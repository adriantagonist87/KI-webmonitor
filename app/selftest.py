"""Integration tests in an isolated database; no production data or model calls."""
import os
import tempfile
test_directory = tempfile.TemporaryDirectory(prefix='.kiwm-selftest-', dir='/data')
os.environ['DATA_DIR'] = test_directory.name
import json
import unittest
from datetime import datetime
from fixture import pdf
from store import *
from engine import archive, allowed, document_suffix, HTMLDocumentURL

class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init()
        cls.p = {'id': 'test-project'}
        execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)', ('test-project', 'Test', 'https://example.org', 'Test', 'example.org', '0 6 * * *', now(), now()))

    def run_row(self, rid):
        execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)', (rid, 'test-project', now(), 'success', 'test'))

    def test_01_pdf_bytes_and_metadata_only_changes_both_alert_without_text(self):
        from change_review import DOCUMENT_HASH_POLICY, review_document_hash_change
        self.run_row('one')
        a = pdf('Deadline 30 days', metadata='A')
        first = archive(self.p, 'one', 'https://example.org/test.pdf', 'document', a, '', suffix='.pdf')
        self.assertFalse(rows('SELECT * FROM changes'))
        self.run_row('two')
        b = pdf('Deadline 30 days', metadata='B')
        archive(self.p, 'two', 'https://example.org/test.pdf', 'document', b, '', suffix='.pdf')
        technical = one("SELECT * FROM changes WHERE run_id='two'")
        self.assertEqual(technical['kind'], 'Datei geändert')
        self.assertNotIn('text_diff', json.loads(technical['evidence']))
        self.run_row('three')
        c = pdf('Deadline 45 days', metadata='B')
        archive(self.p, 'three', 'https://example.org/test.pdf', 'document', c, '', suffix='.pdf')
        changed = one("SELECT * FROM changes WHERE run_id='three'")
        self.assertEqual(changed['kind'],'Datei geändert')
        for change in (technical,changed):
            detail=json.loads(change['evidence'])
            self.assertNotIn('text_diff',detail)
            self.assertEqual(detail['ai_assessment']['policy'],DOCUMENT_HASH_POLICY)
            self.assertEqual(detail['ai_assessment']['method'],'deterministic')
            self.assertEqual(detail['ai_assessment']['relevance'],'relevant')
            pair=[one('SELECT * FROM snapshots WHERE id=?',(change[key],)) for key in ('previous_id','current_id')]
            for snapshot in pair:
                self.assertEqual(snapshot['text'],'')
                self.assertEqual(digest((DATA/snapshot['file']).read_bytes()),snapshot['sha256'])
            self.assertNotEqual(pair[0]['sha256'],pair[1]['sha256'])
            verified=review_document_hash_change(change['id'],change['run_id'])
            self.assertTrue(verified['originals_verified']);self.assertFalse(verified['updated'])
        for rid,raw in (('one',a),('two',b),('three',c)):
            snapshot=one('SELECT * FROM snapshots WHERE run_id=?',(rid,))
            self.assertEqual((DATA/snapshot['file']).read_bytes(),raw)
        self.run_row('hash-repeat')
        repeated=archive(self.p,'hash-repeat','https://example.org/test.pdf','document',c,'',suffix='.pdf')
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='hash-repeat'"))
        self.assertEqual(one('SELECT sha256 FROM snapshots WHERE id=?',(repeated,))['sha256'],digest(c))
        self.assertEqual(one('PRAGMA integrity_check')['integrity_check'], 'ok')

    def test_02_links_and_unchanged_content(self):
        self.run_row('four')
        archive(self.p, 'four', 'https://example.org/page', 'page', b'page', 'Page', ['https://example.org/a'])
        self.run_row('five')
        archive(self.p, 'five', 'https://example.org/page', 'page', b'page', 'Page', ['https://example.org/b'])
        r = one("SELECT * FROM changes WHERE run_id='five'")
        e = json.loads(r['evidence'])
        self.assertEqual(e['links_added'], ['https://example.org/b'])
        self.assertEqual(e['links_removed'], ['https://example.org/a'])
        self.run_row('six')
        archive(self.p, 'six', 'https://example.org/page', 'page', b'page', 'Page', ['https://example.org/b'])
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='six'"))

    def test_03_navigation_policy(self):
        self.assertTrue(allowed('https://example.org/publications', ['example.org']))
        for url in ['https://example.org/account/delete', 'https://other.org', 'javascript:alert(1)', 'file:///etc/passwd', 'https://user:password@example.org', 'https://example.org/logout', 'https://example.org/?password=secret', 'https://example.org/?api_key=secret']:
            self.assertFalse(allowed(url, ['example.org']), url)

    def test_04_berlin_schedule(self):
        after = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
        due = datetime.fromisoformat(next_due('0 8 * * 1', after)).astimezone(TZ)
        self.assertEqual((due.weekday(), due.hour, due.minute), (0, 8, 0))
        self.assertGreater(due, after)

    def test_05_repeated_real_transition_is_not_suppressed(self):
        for index, text in enumerate(('A', 'B', 'A', 'B')):
            rid = 'recurrence-' + str(index)
            self.run_row(rid)
            archive(self.p, rid, 'https://example.org/recurrence', 'page', text.encode(), text)
        changed = rows("SELECT * FROM changes WHERE url='https://example.org/recurrence' AND kind='Inhalt geändert'")
        self.assertEqual(len(changed), 3)

    def test_06_report_markup_is_inert(self):
        from markdown_it import MarkdownIt
        text = MarkdownIt('commonmark', {'html': False}).render('<script>alert(1)</script>\n\n[x](javascript:alert(1))')
        self.assertNotIn('<script>', text)
        self.assertNotIn('href="javascript:', text)

    def test_07_tiny_changes_and_review_cutoff(self):
        import attention
        pid = 'test-small-changes'
        execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)', (pid,'Test','https://example.org','Test','example.org','0 6 * * *',now(),now()))
        texts = ['Stand: 18.09.2026\nWert: 10', 'Stand: 19.09.2026\nWert: 10', 'Stand: 19.09.2026\nWert: 11', 'Stand: 19.09.2026\nWert: 11']
        for i, text in enumerate(texts):
            rid = 'small-' + str(i)
            execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)', (rid,pid,now(),f'2026-11-04T14:00:0{i}+00:00','success','test'))
            archive({'id':pid},rid,'https://example.org/small','page',text.encode(),text)
        changes = attention.pending(pid)
        self.assertEqual(len(changes),2)
        self.assertTrue(all(c['priority']=='attention' and c['number_changed'] for c in changes))
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='small-3'"))
        attention.acknowledge(pid,'2026-11-04T14:00:01+00:00',now())
        self.assertEqual([c['run_id'] for c in attention.pending(pid)],['small-2'])
        execute("UPDATE runs SET status='running',finished=NULL WHERE id='small-2'")
        self.assertFalse(attention.pending(pid))
        attention.acknowledge(pid,'2026-11-04T14:00:03+00:00',now())
        execute("UPDATE runs SET status='partial',finished='2026-11-04T14:00:04+00:00' WHERE id='small-2'")
        self.assertEqual(len(attention.pending(pid)),1)
        attention.acknowledge(pid,'2026-11-04T14:00:04+00:00',now())
        self.assertFalse(attention.pending(pid))
        self.assertEqual(len(rows('SELECT id FROM changes WHERE project_id=?',(pid,))),2)

    def test_08_list_insertion_not_duplicate_old_documents(self):
        pid='test-list-order'
        execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)', (pid,'Test','https://example.org','Test','example.org','0 6 * * *',now(),now()))
        for i, text in enumerate(('Alt A\nAlt B','Neu C\nAlt A\nAlt B')):
            rid='list-'+str(i)
            execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,pid,now(),'success','test'))
            archive({'id':pid},rid,'https://example.org/list','page',text.encode(),text,['https://example.org/a.pdf','https://example.org/b.pdf']+(['https://example.org/c.pdf'] if i else []))
        changes=rows('SELECT * FROM changes WHERE project_id=?',(pid,))
        self.assertEqual(len(changes),1)
        e=json.loads(changes[0]['evidence'])
        self.assertEqual(e['links_added'],['https://example.org/c.pdf'])
        self.assertFalse(e['links_removed'])
        self.assertNotIn('-Alt A',e['text_diff'])

    def test_09_document_url_can_serve_html(self):
        for media in ('text/html; charset=utf-8','application/octet-stream'):
            with self.assertRaises(HTMLDocumentURL):
                document_suffix('https://example.org/news.docx',media,b'<!DOCTYPE html><html><h1>Article</h1></html>')
        self.assertEqual(document_suffix('https://example.org/download','application/pdf',b'%PDF-1.4'),'.pdf')

    def test_10_highlighted_diff_cannot_inject_html(self):
        from web import diff_markup
        result=str(diff_markup('--- vorher\n+++ nachher\n-Stand 18.09.2026\n+<script>19.09.2026</script>'))
        self.assertNotIn('<script>',result)
        self.assertIn('&lt;script&gt;',result)
        self.assertIn('diff-added',result)

    def test_11_content_selection_is_site_neutral(self):
        from page_content import editorial_content
        html='<div id="subject"><h1>Index</h1><article><h2><a href="/a">A</a></h2><p>Text</p></article><div class="advertisement">Ad</div></div><aside>History</aside>'
        a=editorial_content(html,'https://one.example/list','#subject h1, #subject article')
        b=editorial_content(html,'https://two.example/list','#subject h1, #subject article')
        self.assertEqual(a['text'],b['text'])
        self.assertNotIn('Ad',a['text']);self.assertNotIn('History',a['text'])
        self.assertEqual(a['metadata']['links'][0]['label'],'A')
        self.assertEqual(a['scope'],'css:#subject h1, #subject article')

    def test_12_selected_article_intro_and_date_change(self):
        from page_content import editorial_content
        raw='<article id="content"><h1>Title</h1><p>Deadline 18.11.2026, 10 Euro.</p></article>'
        a=editorial_content(raw,'https://example.org/article','#content')
        b=editorial_content(raw.replace('18.11.2026','19.11.2026'),'https://example.org/article','#content')
        self.assertEqual(a['metadata']['paragraphs'][0],'Deadline 18.11.2026, 10 Euro.')
        self.assertNotEqual(a['text'],b['text'])
        with self.assertRaises(ValueError):editorial_content(raw,'https://example.org','#missing')

    def test_13_prior_html_rebased_to_model_selected_scope(self):
        from page_content import editorial_content
        pid='test-rebase';url='https://example.org/list'
        execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)',(pid,'Test',url,'Test','example.org','0 6 * * *',now(),now()))
        raw='<main><h1>Title</h1><p id="content">Same</p><div class="advertisement">Old ad</div></main>'
        saved='artifacts/rebase.html.txt';(DATA/saved).parent.mkdir(exist_ok=True);(DATA/saved).write_text(raw)
        for rid in ('rebase-before','rebase-after'):execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,pid,now(),'success','test'))
        archive({'id':pid},'rebase-before',url,'page',raw.encode(),'Title Same Old ad',html=saved)
        result=editorial_content(raw.replace('Old ad','New ad'),url,'#content')
        archive({'id':pid},'rebase-after',url,'page',result['html'].encode(),result['text'],result['links'],scope=result['scope'])
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='rebase-after'"))

    def test_14_generic_main_and_baseline(self):
        from page_content import editorial_content
        result=editorial_content('<header>Menu</header><main><h1>Important</h1><p>04.11.2026</p><aside>Ad</aside></main><footer>History</footer>','https://example.org')
        self.assertIn('04.11.2026',result['text']);self.assertNotIn('Ad',result['text']);self.assertNotIn('History',result['text'])
        self.run_row('initial-baseline')
        archive(self.p,'initial-baseline','https://example.org/first','page',b'First','First',baseline=True)
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='initial-baseline'"))

    def test_15_model_report_disclosures_and_active_html(self):
        from report_render import render_report
        text=str(render_report('<details open onclick="bad()"><summary>More</summary><p>Content</p></details><script>bad()</script><iframe src="http://evil"></iframe><img src="http://evil"><a href="javascript:bad()">Bad</a>'))
        self.assertIn('<details>',text);self.assertIn('<summary>More</summary>',text)
        for blocked in ('<script','<iframe','<img','onclick','javascript:',' open'):
            self.assertNotIn(blocked,text)
        self.assertIn('<table>',str(render_report('| A | B |\n|---|---|\n| 1 | 2 |')))

    def test_16_generic_agent_uses_model_selected_sources(self):
        import asyncio
        from agent_driver import run_agent
        calls=[]; requests=[]
        async def model(messages,selected,**kwargs):
            requests.append(messages.copy())
            self.assertIn('tools',kwargs)
            if not calls:
                return {'choices':[{'message':{'tool_calls':[{'id':'a','type':'function','function':{'name':'read_sources','arguments':json.dumps({'urls':['https://example.org/one','https://example.org/two']})}}]}}]}
            if len(requests)==2:
                return {'choices':[{'message':{'tool_calls':[{'id':'b','type':'function','function':{'name':'finish','arguments':json.dumps({'complete':True,'pending':['None'],'checked':['two'],'findings':'Done'})}}]}}]}
            return {'choices':[{'message':{'tool_calls':[{'id':'c','type':'function','function':{'name':'finish','arguments':json.dumps({'complete':True,'pending':[],'checked':['two'],'findings':'Done'})}}]}}]}
        async def dispatch(name,args):calls.extend(args['urls']);return {'read':calls}
        async def screen():return {'type':'text','text':'Real viewport placeholder'}
        result=asyncio.run(run_agent('Inspect sources',{},'local',model,dispatch,screen,lambda x:None))
        self.assertEqual(calls,['https://example.org/one','https://example.org/two'])
        self.assertTrue(result['complete']);self.assertEqual(result['pending'],[]);self.assertEqual(len(result['actions']),3)
        self.assertTrue(any(m.get('role')=='tool' and 'error' in m.get('content','') for m in requests[2]))

    def test_17_scope_migration_is_not_a_website_change(self):
        from page_content import editorial_content
        self.run_row('scope-old');self.run_row('scope-new')
        saved='artifacts/old-scope.html.txt';(DATA/saved).write_text('<div data-kiwm-scope="older-scope"><p>Old limited text</p></div>')
        archive(self.p,'scope-old','https://example.org/scope','page',b'Old','Old limited text',html=saved)
        current=editorial_content('<main><p>Wider new capture</p></main>','https://example.org/scope')
        archive(self.p,'scope-new','https://example.org/scope','page',current['html'].encode(),current['text'],scope=current['scope'])
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='scope-new'"))

    def test_18_first_read_is_not_a_proven_source_change(self):
        self.run_row('first-new-source')
        archive(self.p,'first-new-source','https://example.org/previously-unvisited','page',b'Existing','Existing old source')
        self.assertFalse(rows("SELECT * FROM changes WHERE run_id='first-new-source'"))
        from engine import summary_evidence
        e=summary_evidence({'changes':[{'kind':'Neue Seite','url':'https://example.org/old','evidence':'{}'}],'observations':[]})
        self.assertFalse(e['changes']);self.assertEqual(len(e['first_captures']),1)

    def test_19_truncated_model_report_is_retried_and_never_accepted(self):
        import asyncio
        from unittest.mock import AsyncMock,patch
        from engine import create_summary
        cut={'choices':[{'finish_reason':'length','message':{'content':'Broken report'}}]}
        full={'choices':[{'finish_reason':'stop','message':{'content':'Complete short report'}}]}
        with patch('engine.llm',new=AsyncMock(side_effect=[cut,full])):
            self.assertEqual(asyncio.run(create_summary({'changes':[],'observations':[]},'local')),'Complete short report')
        with patch('engine.llm',new=AsyncMock(side_effect=[cut,cut])):
            with self.assertRaises(RuntimeError):asyncio.run(create_summary({'changes':[],'observations':[]},'local'))

    def test_20_relevance_review_keeps_proof_and_requires_quote(self):
        from change_review import assess_change
        import attention
        for rid,text in [('review-old','Webinar A'),('review-new','Webinar B')]:
            self.run_row(rid)
            archive(self.p,rid,'https://example.org/relevance','page',text.encode(),text)
        execute("UPDATE runs SET finished=? WHERE id='review-new'",(now(),))
        row=one("SELECT * FROM changes WHERE run_id='review-new'")
        with self.assertRaises(ValueError):assess_change('another-run',row['id'],'irrelevant','Clearly only an advertising box','-Webinar A')
        with self.assertRaises(ValueError):assess_change('review-new',row['id'],'irrelevant','Clearly only an advertising box','invented text')
        assess_change('review-new',row['id'],'irrelevant','Clearly only a rotating advertising box','-Webinar A\n+Webinar B')
        updated=one('SELECT * FROM changes WHERE id=?',(row['id'],))
        self.assertEqual(json.loads(updated['evidence'])['text_diff'],json.loads(row['evidence'])['text_diff'])
        self.assertNotIn(row['id'],[c['id'] for c in attention.pending(self.p['id'])])

    def test_21_http_report_uses_inert_real_disclosures(self):
        from fastapi.testclient import TestClient
        from bs4 import BeautifulSoup
        from web import app
        self.run_row('report-render-http')
        execute('UPDATE runs SET summary=? WHERE id=?',('<details open><summary>Source details</summary><p>Proof</p></details><script>bad()</script>','report-render-http'))
        response=TestClient(app).get('/runs/report-render-http')
        self.assertEqual(response.status_code,200)
        report=BeautifulSoup(response.text,'html.parser').select_one('.report')
        self.assertEqual(report.select_one('details summary').get_text(),'Source details')
        self.assertNotIn('open',report.details.attrs)
        self.assertIsNone(report.find('script'))
        self.assertNotIn('&lt;details',str(report))

    def test_22_visual_changes_compare_archived_pair_only(self):
        import asyncio
        from PIL import Image
        from unittest.mock import AsyncMock,patch
        from engine import analyze_visual_changes
        for rid,color in [('visual-old','red'),('visual-new','blue')]:
            self.run_row(rid)
            path='artifacts/'+rid+'.png';Image.new('RGB',(40,40),color).save(DATA/path)
            archive(self.p,rid,'https://example.org/canvas','page',b'same','Same text',screenshot=path)
        reply={'choices':[{'message':{'content':'Visible color changed'}}]}
        with patch('engine.llm',new=AsyncMock(return_value=reply)) as model:
            result=asyncio.run(analyze_visual_changes(self.p['id'],'visual-new','local-vision'))
            self.assertTrue(result[0]['comparison_available'])
            parts=model.call_args.args[0][0]['content']
            self.assertEqual(sum(p['type']=='image_url' for p in parts),2)
        with patch('engine.llm',new=AsyncMock()) as model:
            self.assertEqual(asyncio.run(analyze_visual_changes(self.p['id'],'visual-old','local-vision')),[])
            model.assert_not_called()

if __name__ == '__main__':
    unittest.main(verbosity=2)
