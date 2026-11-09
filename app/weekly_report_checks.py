"""Compact weekly mail acceptance: real renderer, MIME, storage and web routes."""
import asyncio,base64,copy,io,json,os,tempfile,unittest
from pathlib import Path
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock,patch
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image
scratch=tempfile.TemporaryDirectory(prefix='.weekly-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store,web,email_reports as reports,mail_message,report_images,project_icons,transfer
START='2026-10-31T12:33:00+00:00';END='2026-11-07T12:33:00+00:00'
ORIGIN='https://monitor.example.org'
NAMES=['Berlin.de – Verwaltungsvorschriften','VDE - Normenbibliothek','DiBt - Baubestimmungen','Berlin.de - Entscheidungshilfen der obersten Bauaufsicht','Berlin.de - Schulbau Publikationen','Berlin.de – Gesetze','Berlin.de – Verordnungen','Berliner Feuerwehr','BIM Berlin - Vertragsbedingungen','elektro.net – Praxisprobleme','IWW Planungsbüro Professionell','Stromnetz Berlin - Mittelspannung','Stromnetz Berlin - Niederspannung','VdS – Brandschutz & Security']

def demo(count=14):
    projects=[{'id':str(i),'name':NAMES[i] if i<14 else 'Quelle '+str(i),'url':'https://source'+str(i)+'.example.org/','tags':['Arbeit']} for i in range(count)]
    runs=[{'id':'run'+p['id'],'project_id':p['id'],'started':START,'status':'success'} for p in projects]
    changes=[]
    for i,reason in enumerate(['Neue Verwaltungsvorschrift zu Werbeanlagen erkannt.','Neue VDE-Norm in der Normenbibliothek erkannt.','Visuelle Abweichung, keine bestätigte fachliche Änderung.','Layoutabweichung ohne bestätigte fachliche Änderung.']):
        pid=str(min(i,2));changes.append({'id':'event'+str(i),'project_id':pid,'kind':'Inhalt geändert' if i<2 else 'Visuelle Abweichung','evidence':{'ai_assessment':{'relevance':'relevant' if i<2 else 'uncertain','reason':reason}}})
    runs.append({'id':'later','project_id':'1','started':END,'status':'failed','error':'Bot-Schutz aktiviert'})
    tags=[{'name':'Arbeit','color':'#df482a'},{'name':'Privat','color':'#2f88d0'}]
    return projects,runs,changes,tags,START,END

def model(data=None):return reports.build_model(*(data or demo()),base_url=ORIGIN)

class WeeklyReport(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(dir=scratch.name);self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name);store.DATA=web.DATA=self.root
        store.CONFIG=web.CONFIG=self.root/'settings.json';store.SECRETS=web.SECRETS=self.root/'credentials.json';store.init()
    def saved(self):
        bundle=reports.render(model());reports.save('weekly',bundle)
        store.execute('INSERT INTO reports(id,period_key,created,start,end,content) VALUES(?,?,?,?,?,?)',('weekly','test-demo',END,START,END,bundle['text']))
        return store.one('SELECT * FROM reports'),bundle
    def test_01_demo_unique_totals_and_two_notice_events(self):
        m=model();self.assertEqual({k:m['counts'][k] for k in ('sources','changed','notices','unchanged')},{'sources':14,'changed':2,'notices':1,'unchanged':11})
        self.assertEqual(m['projects']['2']['notice_count'],2)
    def test_02_tags_current_colors_and_empty_group(self):
        bundle=reports.render(model());soup=BeautifulSoup(bundle['html'],'html.parser')
        self.assertEqual([(e['data-tag-name'],e['bgcolor']) for e in soup.select('[data-tag-name]')],[('Arbeit','#df482a'),('Privat','#2f88d0')])
        self.assertIn('0 Quellen',soup.select('[data-tag-name]')[1].text)
    def test_03_order_and_three_quiet_columns(self):
        html=reports.render(model())['html'];self.assertLess(html.index('Relevante Änderungen'),html.index('Hinweise'));self.assertLess(html.index('Hinweise'),html.index('Unverändert'))
        soup=BeautifulSoup(html,'html.parser');table=soup.select_one('[data-quiet-columns]');self.assertEqual(len(table.find('tr',recursive=False).find_all('td',recursive=False)),3)
        self.assertEqual(len(table.select('[data-project-id]')),11)
    def test_04_later_failed_run_does_not_hide_change(self):
        m=model();p=m['projects']['1'];self.assertEqual(p['category'],'changed');self.assertIn('Bot-Schutz',p['problem'])
        html=reports.render(m)['html'];self.assertIn('Neue VDE-Norm',html);self.assertIn('Prüfproblem: Bot-Schutz',html)
    def test_05_multi_tag_same_source_unique_count(self):
        data=list(demo());data[0][3]['tags'].append('Privat');m=model(data)
        self.assertEqual(m['counts']['sources'],14);self.assertEqual([len(g['projects']) for g in m['groups']],[14,1])
        self.assertEqual(reports.render(m)['html'].count('data-project-id="3"'),2)
    def test_06_private_change_is_independent(self):
        data=list(demo());data[0].append({'id':'private','name':'Private Quelle','url':'https://private.example.org/','tags':['Privat']})
        data[2].append({'id':'private-event','project_id':'private','kind':'Inhalt geändert','evidence':{'ai_assessment':{'relevance':'relevant','reason':'Neue private Veröffentlichung erkannt.'}}})
        m=model(data);self.assertEqual(m['counts']['sources'],15);self.assertEqual(m['projects']['private']['category'],'changed')
        self.assertEqual(len(m['groups'][0]['projects']),14);self.assertEqual(len(m['groups'][1]['projects']),1)
    def test_07_hundred_sources_without_output_limit(self):
        m=model(demo(100));html=reports.render(m)['html'];soup=BeautifulSoup(html,'html.parser')
        self.assertEqual(len(soup.select('[data-project-id]')),100);self.assertEqual(len(soup.select('[data-category="unchanged"]')),97)
    def test_08_zero_changes_and_no_projects(self):
        data=list(demo());data[1]=data[1][:14];data[2]=[];m=model(data)
        self.assertEqual(m['counts']['unchanged'],14);self.assertNotIn('Relevante Änderungen',reports.render(m)['html'])
        data[0]=data[1]=[];m=model(data);bundle=reports.render(m);self.assertEqual(m['counts']['sources'],0);report_images.validate(bundle['images'],bundle['html'])
    def test_09_xss_and_unsafe_urls_are_not_emitted(self):
        data=list(demo());data[0][0].update(name='<img src=x onerror=evil()> & "lang" '+('Titel '*30),url='javascript:evil()')
        data[2][0]['evidence']={'note':'<script>evil()</script>Text <img src="https://evil.example/track">'}
        bundle=reports.render(model(data));soup=BeautifulSoup(bundle['html'],'html.parser')
        self.assertFalse(soup.select('script,style,iframe,[onerror]'));self.assertIn('&lt;img',bundle['html']);self.assertNotIn('javascript:',bundle['html'])
        self.assertTrue(all(i['src'].startswith('cid:') for i in soup.select('img')))
    def test_10_project_links_and_neutral_styles(self):
        m=model();soup=BeautifulSoup(reports.render(m)['html'],'html.parser')
        for p in m['projects'].values():
            a=soup.find('a',href=p['url'])
            self.assertEqual(a.get_text(),p['name'] if p['category']=='unchanged' else 'Originalquelle')
            self.assertIn('text-decoration:none',a['style'])
        app_links=[a['href'] for a in soup.find_all('a') if a.get_text()=='Webmonitor ›']
        self.assertEqual(set(app_links),{ORIGIN+'/projects/'+str(i) for i in range(3)})
    def test_11_no_origin_no_local_app_links_and_secret_query_rejected(self):
        data=demo();m=reports.build_model(*data,base_url='');html=reports.render(m)['html'];self.assertNotIn('Webmonitor ›',html)
        for url in ('https://u:p@example.org','https://example.org?api_key=SECRET','https://example.org?password=SECRET','file:///etc/passwd'):
            self.assertEqual(reports.safe_url(url),'')
    def test_12_cid_parts_deduplicated_and_real_png(self):
        bundle=reports.render(model());images=report_images.validate(bundle['images'],bundle['html']);self.assertEqual(len(images),2)
        for raw in images.values():self.assertEqual(Image.open(io.BytesIO(raw)).format,'PNG')
    def test_13_svg_rasterized_and_corrupt_favicon_falls_back(self):
        svg=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><script>evil()</script><rect fill="#df482a" width="24" height="24"/></svg>'
        record=project_icons.record(svg);bundle=reports.render(model(),{'0':record,'1':{'data':'CORRUPT'}})
        images=report_images.validate(bundle['images'],bundle['html']);self.assertEqual(len(images),3)
        soup=BeautifulSoup(bundle['html'],'html.parser');self.assertNotEqual(soup.select_one('[data-project-id="0"] img')['src'],soup.select_one('[data-project-id="1"] img')['src'])
    def test_14_mime_exact_saved_html_and_inline_content_ids(self):
        bundle=reports.render(model());msg=mail_message.build(bundle['text'],'Test','sender@example.test','test@example.test',bundle['html'],bundle['images'])
        parsed=BytesParser(policy=policy.default).parsebytes(msg.as_bytes());self.assertEqual(parsed.get_content_type(),'multipart/alternative')
        self.assertEqual(parsed.get_body('html').get_content().rstrip(),bundle['html'].rstrip())
        self.assertEqual(parsed.get_body('plain').get_content().rstrip(),bundle['text'].rstrip())
        self.assertEqual({p['Content-ID'].strip('<>') for p in parsed.walk() if p.get_content_maintype()=='image'},{i['cid'] for i in bundle['images']})
    def test_15_invalid_or_missing_image_cannot_be_sent(self):
        bundle=reports.render(model())
        with self.assertRaises(ValueError):mail_message.build('x','x','a','b',bundle['html'],[])
        bad=copy.deepcopy(bundle['images']);bad[0]['data']=base64.b64encode(b'bad').decode()
        with self.assertRaises(ValueError):report_images.validate(bad,bundle['html'])
    def test_16_preview_only_rewrites_cid_and_is_immutable(self):
        r,bundle=self.saved();before=reports.saved_html(r);preview=reports.preview_html(r)
        for item in bundle['images']:preview=preview.replace('/reports/weekly/assets/'+item['cid'],'cid:'+item['cid'])
        self.assertEqual(preview,before);self.assertEqual(reports.saved_html(r),bundle['html'])
        with patch.dict(os.environ,{'KIWM_PUBLIC_URL':'https://changed.example.org'}):self.assertEqual(reports.saved_html(r),before)
    def test_17_isolated_preview_assets_and_eml_routes(self):
        r,bundle=self.saved();client=TestClient(web.app)
        wrapper=client.get('/reports/weekly');self.assertEqual(wrapper.status_code,200);self.assertIn('sandbox="allow-same-origin',wrapper.text);self.assertNotIn(bundle['html'],wrapper.text)
        response=client.get('/reports/weekly/html');self.assertEqual(response.headers['x-frame-options'],'SAMEORIGIN');self.assertIn("frame-ancestors 'self'",response.headers['content-security-policy'])
        self.assertNotIn('ui.css',response.text);self.assertNotIn('<script',response.text)
        image=client.get('/reports/weekly/assets/'+bundle['images'][0]['cid']);self.assertEqual(image.content,base64.b64decode(bundle['images'][0]['data']))
        eml=client.get('/reports/weekly/message.eml');self.assertEqual(eml.status_code,200);self.assertEqual(BytesParser(policy=policy.default).parsebytes(eml.content).get_body('html').get_content().rstrip(),bundle['html'].rstrip())
        self.assertEqual(client.get('/reports/missing/html').status_code,404);self.assertEqual(client.get('/reports/weekly/assets/missing').status_code,404)
        self.assertEqual(client.get('/').headers['x-frame-options'],'DENY')
    def test_18_archive_contains_report_and_inline_assets(self):
        r,bundle=self.saved();archive=self.root/'backup.zip';transfer.export_to(archive,history=True)
        package=transfer.read_package(archive)
        dest=self.root/'restored';dest.mkdir();store.DATA=web.DATA=dest;store.CONFIG=web.CONFIG=dest/'settings.json';store.SECRETS=web.SECRETS=dest/'credentials.json';store.init()
        transfer.import_package(archive,package)
        r=store.one('SELECT * FROM reports WHERE id=?',('weekly',));self.assertEqual(reports.saved_html(r),bundle['html']);self.assertEqual(reports.saved_images(r),bundle['images'])
    def test_19_local_model_shortens_only_confirmed_evidence(self):
        m=model();fn=AsyncMock(return_value={'choices':[{'message':{'content':json.dumps({'headlines':[{'id':'0','text':'Neue Verwaltungsvorschrift veröffentlicht.','sources':['event0']},{'id':'1','text':'ERFUNDENE NACHRICHT','sources':['unknown']}]})}}]})
        result=asyncio.run(reports.compact_headlines(m,fn,'local-model'))
        self.assertEqual(result['projects']['0']['message'],'Neue Verwaltungsvorschrift veröffentlicht.')
        self.assertEqual(result['projects']['1']['message'],m['projects']['1']['message']);self.assertNotEqual(result,m)
        self.assertNotIn('password',json.dumps(fn.call_args.args).lower())
    def test_20_local_model_failure_keeps_report_working(self):
        m=model();fn=AsyncMock(side_effect=TimeoutError())
        result=asyncio.run(reports.compact_headlines(m,fn,'local-model'));self.assertEqual(result['projects'],m['projects']);self.assertEqual(result['headline_status'],'fallback-existing-assessments')
    def test_21_no_flex_grid_javascript_media_or_external_images(self):
        html=reports.render(model())['html']
        for forbidden in ('<script','@media','display:flex','display:grid','RAW_JSON'):self.assertNotIn(forbidden,html)
        self.assertNotIn('src="http',html)
    def test_22_unchanged_and_irrelevant_do_not_trigger_change(self):
        data=list(demo());data[2]=[{'id':'irrelevant','project_id':'4','kind':'Inhalt geändert','evidence':{'ai_assessment':{'relevance':'irrelevant','reason':'IRRELEVANT-CANARY'}}}]
        m=model(data);self.assertEqual(m['projects']['4']['category'],'unchanged');self.assertNotIn('IRRELEVANT-CANARY',reports.render(m)['html'])
    def test_23_content_id_is_literal_ascii_in_smtp_header(self):
        bundle=reports.render(model());msg=mail_message.build(bundle['text'],'Test','a@example.test','b@example.test',bundle['html'],bundle['images'])
        raw=msg.as_bytes(policy=policy.SMTP).decode('utf-8')
        identifiers=[line for line in raw.splitlines() if line.startswith('Content-ID:')]
        self.assertEqual(len(identifiers),len(bundle['images']))
        for line in identifiers:
            self.assertLessEqual(len(line),78);self.assertIn('Content-ID: <kiwm-',line);self.assertNotIn('=?',line)

if __name__=='__main__':unittest.main(verbosity=2)
