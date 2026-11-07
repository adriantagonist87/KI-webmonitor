"""Isolated document discovery, shared budgets and accepted-state comparisons."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

scratch = tempfile.TemporaryDirectory(prefix='.document-inventory-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import store
import engine
from fixture import pdf
from document_inventory import collect_documents, document_identity, plan_documents, DOCUMENT_LIMIT

EXTENSIONS = ('.pdf', '.docx', '.txt', '.csv', '.xml')
BASE = 'https://example.org/'


def allowed(url):
    return urlsplit(url).hostname == 'example.org'


class InventoryTests(unittest.TestCase):
    def test_01_current_links_are_defragmented_filtered_and_ordered(self):
        captured = {'source': {'metadata': {'links': [
            {'url': BASE + 'second.PDF#page=2', 'label': ' Norm  Stand 2026 '},
            {'url': BASE + 'first.docx?download=1', 'label': 'Anlage'},
            {'url': BASE + 'second.PDF#page=9', 'label': 'Duplicate'},
            {'url': 'https://elsewhere.example/foreign.pdf', 'label': 'Foreign'},
            {'url': 'javascript:bad.pdf', 'label': 'Bad'},
            {'url': BASE + 'article.html', 'label': 'HTML'},
            {'url': 'https://user:password@example.org/private.pdf', 'label': 'Credentials'},
            {'url': BASE + 'bad port.pdf', 'label': 'Whitespace'},
        ]}}}
        result = collect_documents(captured, allowed, EXTENSIONS)
        self.assertEqual(list(result), [BASE + 'second.PDF', BASE + 'first.docx?download=1'])
        self.assertEqual(result[BASE + 'second.PDF'], 'Norm Stand 2026')

    def test_02_fallback_strings_preserve_query_but_empty_scope_is_authoritative(self):
        captured = {'fallback': {'links': [BASE + 'legacy.pdf?version=2#page=1']},
                    'empty': {'metadata': {'links': []}, 'links': [BASE + 'outside.pdf']}}
        result = collect_documents(captured, allowed, EXTENSIONS)
        self.assertEqual(result, {BASE + 'legacy.pdf?version=2': BASE + 'legacy.pdf?version=2'})

    def test_03_final_scope_excludes_header_navigation_and_superseded_capture(self):
        captured = {BASE: {'metadata': {'links': [{'url': BASE + 'header.pdf', 'label': 'Header'}]}}}
        # The agent's final selected source replaces its earlier broad capture.
        captured[BASE] = {'metadata': {'links': [{'url': BASE + 'annex.pdf', 'label': 'Anlage'}]},
                          'navigation_links': [{'url': BASE + 'header.pdf', 'label': 'Header'}],
                          'links': [BASE + 'header.pdf', BASE + 'annex.pdf']}
        self.assertEqual(collect_documents(captured, allowed, EXTENSIONS), {BASE + 'annex.pdf': 'Anlage'})

    def test_04_new_scope_documents_are_added_without_reordering_or_mutation(self):
        captured = {'a': {'metadata': {'links': [{'url': BASE + 'a.pdf', 'label': ''}]}},
                    'b': {'metadata': {'links': [{'url': BASE + 'a.pdf#x', 'label': 'Richtlinie'},
                                               {'url': BASE + 'b.pdf', 'label': 'Anlage B'}]}}}
        before = json.dumps(captured)
        result = collect_documents(captured, allowed, EXTENSIONS)
        self.assertEqual(result, {BASE + 'a.pdf': 'Richtlinie', BASE + 'b.pdf': 'Anlage B'})
        self.assertEqual(json.dumps(captured), before)

    def test_05_limit_two_returns_two_explicit_overflow_urls(self):
        candidates = {BASE + key + '.pdf': key for key in ('z', 'a', 'c', 'b')}
        todo, overflow = plan_documents(candidates, set(), limit=2)
        self.assertEqual(todo, [BASE + 'z.pdf', BASE + 'a.pdf'])
        self.assertEqual(overflow, [BASE + 'c.pdf', BASE + 'b.pdf'])

    def test_06_successes_and_errors_share_budget_and_are_never_retried(self):
        # Mapping values deliberately differ; success and failure both count.
        attempted = {BASE + 'ok.pdf': 'success', BASE + 'failed.pdf': 'error'}
        candidates = {BASE + name + '.pdf': name for name in ('failed', 'new', 'ok')}
        self.assertEqual(plan_documents(candidates, attempted, limit=2), ([], [BASE + 'new.pdf']))

    def test_07_later_discovery_uses_remaining_global_budget(self):
        attempted = {BASE + 'first.pdf'}
        candidates = {BASE + name + '.pdf': name for name in ('first', 'later', 'overflow')}
        todo, overflow = plan_documents(candidates, attempted, limit=2)
        self.assertEqual(todo, [BASE + 'later.pdf'])
        self.assertEqual(overflow, [BASE + 'overflow.pdf'])
        attempted.update(todo)
        self.assertEqual(plan_documents(candidates, attempted, limit=2), ([], [BASE + 'overflow.pdf']))

    def test_08_default_limit_and_fragment_duplicates_are_explicit(self):
        candidates = {BASE + str(i) + '.pdf': str(i) for i in range(DOCUMENT_LIMIT + 2)}
        todo, overflow = plan_documents(candidates, set())
        self.assertEqual(len(todo), DOCUMENT_LIMIT)
        self.assertEqual(overflow, [BASE + str(i) + '.pdf'
                                    for i in range(DOCUMENT_LIMIT, DOCUMENT_LIMIT + 2)])
        self.assertEqual(plan_documents({BASE + 'old.pdf#x': 'Old', BASE + 'new.pdf': 'New'},
                                        {BASE + 'old.pdf#y'}, limit=1), ([], [BASE + 'new.pdf']))

    def test_18_default_budget_remains_shared_across_late_discovery_and_errors(self):
        self.assertEqual(DOCUMENT_LIMIT, 128)
        attempted = {BASE + str(i) + '.pdf': ('error' if i % 2 else 'success')
                     for i in range(DOCUMENT_LIMIT - 1)}
        candidates = [BASE + str(i) + '.pdf' for i in range(DOCUMENT_LIMIT + 2)]
        candidates.insert(0, BASE + '0.pdf#duplicate')
        todo, overflow = plan_documents(candidates, attempted)
        self.assertEqual(todo, [BASE + str(DOCUMENT_LIMIT - 1) + '.pdf'])
        self.assertEqual(overflow, [BASE + str(i) + '.pdf'
                                    for i in range(DOCUMENT_LIMIT, DOCUMENT_LIMIT + 2)])
        attempted[todo[0]] = 'error'
        self.assertEqual(plan_documents(candidates, attempted), ([], overflow))

    def test_16_two_current_groups_fit_one_global_bounded_budget(self):
        first = {BASE + f'faq-{i}.pdf': f'FAQ {i}' for i in range(15)}
        second = {BASE + f'note-{i}.pdf': f'Merkblatt {i}' for i in range(37)}
        todo, overflow = plan_documents({**first, **second}, set())
        self.assertEqual(len(todo), 52)
        self.assertEqual(overflow, [])
        # A smaller explicitly chosen budget still reports every missing item.
        pending, overflow = plan_documents({**first, **second}, set(first), limit=50)
        self.assertEqual(len(pending), 35)
        self.assertEqual(len(overflow), 2)

    def test_17_generic_download_links_use_only_their_unambiguous_card_heading(self):
        html = '''<main><h1>Dokumente</h1><ul>
          <li><div><h3>Steigleitung</h3><p>Letzte Änderung: 01.11.2026</p></div>
              <div><p><a href="/faq.pdf">Download</a></p></div></li>
          <li><h3>Feuerwehrplan</h3><a href="/plan.pdf">Download</a></li>
          <li><h3>Zwei Anlagen</h3><a href="/a.pdf">Download</a><a href="/b.pdf">Download</a></li>
          <li><h3>Überschrift</h3><a href="/own.pdf">Eigener aussagekräftiger Titel</a></li>
          </ul><a href="/article.html">Download</a></main>'''
        result = engine.editorial_content(html, BASE)
        labels = {item['url']: item['label'] for item in result['metadata']['links']}
        self.assertEqual(labels[BASE+'faq.pdf'], 'Steigleitung')
        self.assertEqual(labels[BASE+'plan.pdf'], 'Feuerwehrplan')
        self.assertEqual(labels[BASE+'a.pdf'], 'Download')
        self.assertEqual(labels[BASE+'b.pdf'], 'Download')
        self.assertEqual(labels[BASE+'own.pdf'], 'Eigener aussagekräftiger Titel')
        self.assertEqual(labels[BASE+'article.html'], 'Download')

    def test_12_cache_identity_preserves_semantic_query_values(self):
        original = (BASE + 'rule.pdf?v=2026-01&version=2&ref=a%2Fb&blank=&tag=one&tag=two'
                    '&ts=123456789&ts=1700000000&ts=17000000000&ts=170000000000&ts=1700000000000'
                    '&ts=12345678&ts=17000000000000&ts=version2&ts=&TS=1700000000#page=2')
        expected = (BASE + 'rule.pdf?v=2026-01&version=2&ref=a%2Fb&blank=&tag=one&tag=two'
                    '&ts=12345678&ts=17000000000000&ts=version2&ts=&TS=1700000000')
        self.assertEqual(document_identity(original), expected)
        self.assertNotEqual(document_identity(BASE + 'rule.pdf?v=1&ts=1700000000'),
                            document_identity(BASE + 'rule.pdf?v=2&ts=1700000000'))
        self.assertEqual(document_identity(BASE + 'rule.pdf#page=1'), BASE + 'rule.pdf')

    def test_19_actual_download_attributes_and_mime_hints_survive_capture_without_changing_plain_links(self):
        html='''<main><h1>Dateien</h1>
          <section><h3>Technische Richtlinie</h3><a href="/download?id=1" download="report.pdf" type="Application/PDF">Download</a></section>
          <a href="/download?id=2" download>Originaldatei</a>
          <a href="/download?id=3" type="application/vnd.ms-excel">Berechnung</a>
          <a href="/article" type="text/html">Artikel</a><a href="/plain">Normaler Link</a>
          <a href="/invalid-name" download="'''+('x'*250)+'''">Anlage</a></main>'''
        source=engine.editorial_content(html,BASE)
        links={item['url']:item for item in source['metadata']['links']}
        self.assertEqual(links[BASE+'download?id=1']['download'],'report.pdf')
        self.assertEqual(links[BASE+'download?id=1']['type'],'application/pdf')
        self.assertEqual(links[BASE+'download?id=1']['label'],'Technische Richtlinie')
        self.assertIs(links[BASE+'download?id=2']['download'],True)
        self.assertEqual(links[BASE+'download?id=3']['type'],'application/vnd.ms-excel')
        self.assertEqual(set(links[BASE+'plain']),{'url','label','context'})
        self.assertEqual(set(links[BASE+'article']),{'url','label','context'})
        self.assertIs(links[BASE+'invalid-name']['download'],True)
        self.assertEqual(set(collect_documents({'page':source},allowed,EXTENSIONS)),
            {BASE+'download?id=1',BASE+'download?id=2',BASE+'download?id=3',BASE+'invalid-name'})

    def test_20_declared_extensionless_files_obey_url_safety_and_do_not_use_label_keywords(self):
        source={'metadata':{'links':[
            {'url':BASE+'download?id=1#part','label':'Report','download':True},
            {'url':BASE+'download?id=2','label':'Original','download':'report.doc'},
            {'url':BASE+'download?id=3','label':'Sheet','type':'application/vnd.ms-excel'},
            {'url':BASE+'article','label':'PDF Download Dokument'},
            {'url':'https://elsewhere.example/file','label':'Foreign','download':True},
            {'url':'javascript:alert(1)','label':'Bad','download':True},
            {'url':'https://user:password@example.org/file','label':'Private','type':'application/pdf'},
        ]}}
        result=collect_documents({'page':source},allowed,EXTENSIONS)
        self.assertEqual(result,{BASE+'download?id=1':'Report',BASE+'download?id=2':'Original',
                                 BASE+'download?id=3':'Sheet'})

    def test_21_known_downloads_are_counted_only_while_linked_in_current_selected_scopes(self):
        current=BASE+'endpoint?id=1';excluded=BASE+'endpoint?id=2';travel=BASE+'endpoint?id=3'
        captured={'page':{'metadata':{'links':[{'url':current+'#download','label':'Richtlinie'},
                                              {'url':BASE+'ordinary-page','label':'Normal'}]},
                          'navigation_links':[{'url':excluded,'label':'Header'}],
                          'links':[current,excluded]}}
        before=json.dumps(captured)
        result=collect_documents(captured,allowed,EXTENSIONS,known_documents={current:'',excluded:'',travel:''})
        self.assertEqual(result,{current:'Richtlinie'})
        self.assertEqual(json.dumps(captured),before)
        self.assertEqual(collect_documents(captured,allowed,EXTENSIONS),{})

    def test_22_empty_scope_still_excludes_known_downloads_and_fallback_strings_remain_supported(self):
        target=BASE+'endpoint?id=1'
        captured={'page':{'metadata':{'links':[]},'links':[target]}}
        self.assertEqual(collect_documents(captured,allowed,EXTENSIONS,known_documents=[target]),{})
        self.assertEqual(collect_documents({'legacy':{'links':[target]}},allowed,EXTENSIONS,known_documents=target),
                         {target:target})

    def test_23_invalid_hints_and_inline_images_are_ignored_but_explicit_image_downloads_are_files(self):
        rejected=[{'url':BASE+'page-a','download':False},{'url':BASE+'page-b','download':[]},
                  {'url':BASE+'page-c','download':'x'*201},{'url':BASE+'page-d','download':'bad\nname'},
                  {'url':BASE+'page-e','type':'text/html'},{'url':BASE+'image.png','type':'image/png'},
                  {'url':BASE+'page-f','type':'application/pdf\r\ninjected'},
                  {'url':BASE+'page-g','type':'x'*161}]
        source={'metadata':{'links':rejected+[{'url':BASE+'explicit-image.png','download':True}]}}
        self.assertEqual(collect_documents({'page':source},allowed,EXTENSIONS),
                         {BASE+'explicit-image.png':BASE+'explicit-image.png'})

    def test_24_new_selected_scope_does_not_keep_previously_declared_downloads(self):
        target=BASE+'endpoint?id=old'
        captures={'page':{'metadata':{'links':[{'url':target,'download':True}]}}}
        self.assertEqual(collect_documents(captures,allowed,EXTENSIONS),{target:target})
        captures['page']={'metadata':{'links':[{'url':BASE+'endpoint?id=current','type':'application/pdf'}]},
                          'navigation_links':[{'url':target,'download':True}]}
        self.assertEqual(collect_documents(captures,allowed,EXTENSIONS,known_documents=[target]),
                         {BASE+'endpoint?id=current':BASE+'endpoint?id=current'})


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        store.DATA = engine.DATA = Path(self.directory.name)
        store.CONFIG = store.DATA / 'settings.json'
        store.SECRETS = store.DATA / 'credentials.json'
        store.init()
        self.project = {'id': 'inventory-project'}
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (self.project['id'], 'Normen', BASE, 'Dateien und Versionsdaten prüfen', 'example.org',
                       '0 6 * * *', 0, store.next_due('0 6 * * *'), store.now()))

    def tearDown(self):
        self.directory.cleanup()

    def run_row(self, rid, status='success'):
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid, self.project['id'], store.now(), store.now(), status, 'test'))

    def document(self, rid, text):
        raw = pdf(text)
        sid = engine.archive(self.project, rid, BASE + 'norm.pdf', 'document', raw,
                             '', suffix='.pdf')
        return sid, raw

    def assert_hash_comparison(self, change, previous_id, current_id):
        previous = store.one('SELECT * FROM snapshots WHERE id=?', (previous_id,))
        current = store.one('SELECT * FROM snapshots WHERE id=?', (current_id,))
        detail = json.loads(change['evidence'])
        self.assertEqual(change['kind'], 'Datei geändert')
        self.assertEqual(detail['comparison_mode'], 'sha256')
        self.assertFalse(detail['content_analysis_performed'])
        self.assertNotIn('text_diff', detail)
        proof = detail['document_comparison']
        self.assertEqual(proof['previous_snapshot_id'], previous_id)
        self.assertEqual(proof['current_snapshot_id'], current_id)
        self.assertEqual(proof['previous_sha256'], previous['sha256'])
        self.assertEqual(proof['current_sha256'], current['sha256'])
        self.assertNotEqual(proof['previous_sha256'], proof['current_sha256'])
        self.assertTrue(proof['originals_verified'])
        for snapshot in (previous, current):
            self.assertEqual(snapshot['text'], '')
            self.assertEqual(snapshot['text_sha256'], store.digest(''))

    def scoped_page(self, rid, title, links):
        scope='css:section.container h1, section.container a.white'
        source='<div data-kiwm-scope="'+scope+'"><h1>'+title+'</h1>'
        source+=''.join('<a class="white" href="'+url+'">'+label+'</a>' for url,label in links)+'</div>'
        text='\n'.join([title]+[label for url,label in links])
        path=Path(rid+'.html.txt')
        (store.DATA/path).write_text(source)
        sid=engine.archive(self.project,rid,BASE,'page',source.encode(),text,
                           [url for url,label in links],html=str(path),scope=scope)
        return sid,source,text,scope

    def test_09_same_url_new_version_has_bound_hash_difference_and_both_original_files(self):
        self.run_row('version-one')
        first, original = self.document('version-one', 'Norm version 2026-01. Deadline 10 days.')
        self.run_row('version-two')
        second, updated = self.document('version-two', 'Norm version 2026-02. Deadline 11 days.')
        change = store.one('SELECT * FROM changes WHERE run_id=?', ('version-two',))
        self.assertEqual(change['previous_id'], first)
        self.assertEqual(change['current_id'], second)
        self.assert_hash_comparison(change, first, second)
        self.assertNotEqual(hashlib.sha256(original).hexdigest(), hashlib.sha256(updated).hexdigest())
        for row in store.rows("SELECT file,sha256 FROM snapshots WHERE kind='document'"):
            path = store.DATA / row['file']
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['sha256'])

    def test_10_failed_mutated_snapshot_cannot_hide_next_successful_change(self):
        self.run_row('accepted-before')
        accepted, _ = self.document('accepted-before', 'Version 1. Deadline 10 days.')
        self.run_row('failed-middle', status='failed')
        rejected, _ = self.document('failed-middle', 'Version 2. Deadline 11 days.')
        self.run_row('accepted-after')
        current, _ = self.document('accepted-after', 'Version 2. Deadline 11 days.')
        change = store.one('SELECT * FROM changes WHERE run_id=?', ('accepted-after',))
        self.assertIsNotNone(change, 'Failed snapshots must not consume the last accepted baseline.')
        self.assertEqual(change['previous_id'], accepted)
        self.assertNotEqual(change['previous_id'], rejected)
        self.assertEqual(change['current_id'], current)
        self.assert_hash_comparison(change, accepted, current)

    def test_11_new_annex_is_link_proven_removed_file_is_not_queued(self):
        old_url, new_url = BASE + 'old.pdf', BASE + 'annex.docx'
        self.run_row('index-before')
        engine.archive(self.project, 'index-before', BASE, 'page', b'old', 'Version 1', [old_url])
        # A historical file remains in storage but is not an automatic candidate.
        engine.archive(self.project, 'index-before', old_url, 'document', pdf('Old annex'), 'Old annex', suffix='.pdf')
        self.run_row('index-after')
        engine.archive(self.project, 'index-after', BASE, 'page', b'new', 'Version 2', [new_url])
        evidence = json.loads(store.one('SELECT evidence FROM changes WHERE run_id=?', ('index-after',))['evidence'])
        self.assertEqual(evidence['links_added'], [new_url])
        self.assertEqual(evidence['links_removed'], [old_url])
        current = {BASE: {'metadata': {'links': [{'url': new_url, 'label': 'Neue Anlage'}]}}}
        candidates = collect_documents(current, allowed, EXTENSIONS)
        self.assertEqual(plan_documents(candidates, set()), ([new_url], []))
        self.assertNotIn(old_url, candidates)
        self.assertTrue((store.DATA / store.one('SELECT file FROM snapshots WHERE url=?', (old_url,))['file']).is_file())

    def test_13_cache_query_change_uses_latest_accepted_document_without_rewriting_urls(self):
        old_url = BASE + 'rule.pdf?v=2026&ts=1700000000'
        next_url = BASE + 'rule.pdf?v=2026&ts=1700000001'
        changed_url = BASE + 'rule.pdf?v=2026&ts=1700000002'
        raw = pdf('Deadline 30 days.')
        self.run_row('cache-first')
        first = engine.archive(self.project, 'cache-first', old_url, 'document', raw,
                               '', suffix='.pdf')
        self.run_row('cache-second')
        second = engine.archive(self.project, 'cache-second', next_url, 'document', raw,
                                '', suffix='.pdf')
        self.assertIsNone(store.one('SELECT * FROM changes WHERE run_id=?', ('cache-second',)))
        rows = store.rows("SELECT id,url,sha256 FROM snapshots WHERE kind='document' ORDER BY captured")
        self.assertEqual([row['url'] for row in rows], [old_url, next_url])
        self.assertEqual(rows[0]['sha256'], rows[1]['sha256'])
        self.run_row('cache-third')
        updated = pdf('Deadline 31 days.')
        third = engine.archive(self.project, 'cache-third', changed_url, 'document', updated,
                               '', suffix='.pdf')
        change = store.one('SELECT * FROM changes WHERE run_id=?', ('cache-third',))
        self.assertIsNotNone(change)
        self.assertEqual(change['previous_id'], second)
        self.assertNotEqual(change['previous_id'], first)
        self.assertEqual(change['current_id'], third)
        self.assert_hash_comparison(change, second, third)
        self.assertEqual(store.one('SELECT url FROM snapshots WHERE id=?', (third,))['url'], changed_url)

    def test_14_matching_scope_uses_canonical_archive_without_missing_selector_ancestors(self):
        import site_privacy
        title='Norm 30 Tage. Kunde 987654321; PLZ: 54321.'
        links=[(BASE+'old.pdf','Bestehend')]
        self.run_row('scope-first')
        token=site_privacy.bind(None)
        try:
            first,source,text,scope=self.scoped_page('scope-first',title,links)
        finally:
            site_privacy.reset(token)
        # This is the real regression: original selector ancestry is absent.
        with self.assertRaises(ValueError):
            engine.editorial_content(source,BASE,scope[4:])
        self.run_row('scope-same')
        token=site_privacy.bind({'abonummer':'987654321','postcode':'54321'})
        try:
            previous,changed=engine.previous_in_scope(self.project['id'],'scope-same',BASE,scope)
            self.assertIsNotNone(previous,'A matching trusted archive marker must preserve the baseline.')
            self.assertEqual(previous['id'],first)
            self.assertFalse(changed)
            self.assertEqual(previous['text'],site_privacy.redact_text(text))
            self.assertNotIn('987654321',previous['text'])
            self.assertNotIn('PLZ: 54321',previous['text'])
            self.assertEqual(previous['text_sha256'],store.digest(previous['text']))
            self.assertEqual(json.loads(previous['links']),[links[0][0]])
            self.scoped_page('scope-same',title,links)
            self.assertEqual(store.rows('SELECT * FROM changes WHERE run_id=?',('scope-same',)),[])
            mismatch,changed=engine.previous_in_scope(self.project['id'],'scope-same',BASE,'css:article')
            self.assertIsNone(mismatch)
            self.assertTrue(changed)
        finally:
            site_privacy.reset(token)
        # Privacy normalization is comparison-local, not a historical rewrite.
        self.assertIn('987654321',store.one('SELECT text FROM snapshots WHERE id=?',(first,))['text'])

    def test_15_matching_scope_detects_single_digit_change_and_new_card_link(self):
        old_url,new_url=BASE+'old.pdf',BASE+'new.pdf'
        self.run_row('scope-before')
        first,source,text,scope=self.scoped_page('scope-before','Norm 30 Tage.',[(old_url,'Bestehend')])
        self.run_row('scope-after')
        previous,changed=engine.previous_in_scope(self.project['id'],'scope-after',BASE,scope)
        self.assertIsNotNone(previous)
        self.assertEqual(previous['id'],first)
        self.assertFalse(changed)
        current,_,_,_=self.scoped_page('scope-after','Norm 31 Tage.',[(old_url,'Bestehend'),(new_url,'Neu')])
        change=store.one('SELECT * FROM changes WHERE run_id=?',('scope-after',))
        self.assertIsNotNone(change,'Actual content changes must not disappear into a new baseline.')
        self.assertEqual(change['previous_id'],first)
        self.assertEqual(change['current_id'],current)
        self.assertIn('Inhalt geändert',change['kind'])
        self.assertIn('Links geändert',change['kind'])
        evidence=json.loads(change['evidence'])
        self.assertIn('-Norm 30 Tage.',evidence['text_diff'])
        self.assertIn('+Norm 31 Tage.',evidence['text_diff'])
        self.assertEqual(evidence['links_added'],[new_url])
        self.assertEqual(evidence['links_removed'],[])


if __name__ == '__main__':
    unittest.main(verbosity=2)
