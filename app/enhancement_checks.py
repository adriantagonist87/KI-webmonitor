"""Isolated icon, mail-format and relative-time acceptance tests."""
import asyncio
import base64
import copy
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from PIL import Image
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import httpx

scratch=tempfile.TemporaryDirectory(prefix='.icons-mail-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store,web,engine,project_icons as icons,email_reports as mails,transfer,gateway
SVG=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#148b9f" d="M2 2h20v20H2z"/></svg>'


class Enhancements(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=scratch.name);self.base=Path(self.tmp.name)
  self.select('source');self.seed()
  self.client=TestClient(web.app);self.client.get('/')
  self.token=self.client.cookies['kiwm_csrf']
 def tearDown(self):self.client.close();self.tmp.cleanup()
 def select(self,name):
  root=self.base/name;root.mkdir(exist_ok=True)
  store.DATA=web.DATA=engine.DATA=root;store.CONFIG=web.CONFIG=root/'settings.json';store.SECRETS=web.SECRETS=root/'credentials.json';store.init()
 def seed(self):
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('alpha','A-Test','https://example.org','Prüfe Änderungen','example.org','0 6 * * *',0,store.now(),store.now()))
 def form(self,**extra):
  return {'csrf':self.token,'id':'alpha','name':'A-Test','url':'https://example.org','prompt':'Prüfe Änderungen','domains':'example.org','cron':'0 6 * * *',**extra}
 def test_01_svg_sanitization_and_idempotence(self):
  raw=b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>evil()</script><foreignObject>bad</foreignObject><use href="https://evil.test/a"/><path style="fill:url(https://evil.test/a)" d="M0 0h2"/></svg>'
  icon=icons.record(raw);self.assertEqual(icons.validate(icon),icon)
  clean=base64.b64decode(icon['data']);self.assertNotIn(b'evil',clean);self.assertNotIn(b'onload',clean);self.assertNotIn(b'foreignObject',clean)
 def test_02_dtd_invalid_and_large_rejected(self):
  for raw in (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',b'abc',b'0'*(icons.MAX_ICON+1)):
   with self.assertRaises(ValueError):icons.record(raw)
 def test_03_ico_rasterized_without_metadata(self):
  out=io.BytesIO();Image.new('RGBA',(32,32),(0,128,180,255)).save(out,format='ICO')
  icon=icons.record(out.getvalue());self.assertEqual(icon['media'],'image/png');icons.validate(icon)
 def test_04_ssrf_urls_rejected(self):
  for url in ('file:///etc/passwd','http://127.0.0.1/','http://192.168.50.8/','http://169.254.169.254/','https://localhost/','https://foo.local/','https://foo.lan/','https://u:p@example.org/','https://example.org:9000/'):
   with self.subTest(url=url),self.assertRaises(ValueError):icons.safe_url(url)
 def test_05_upload_csrf_and_extension_limits(self):
  self.assertEqual(self.client.post('/icons/upload',content=SVG,headers={'x-icon-name':'x.svg'}).status_code,403)
  headers={'x-csrf-token':self.token,'x-icon-name':'icon.svg','content-type':'application/octet-stream'}
  response=self.client.post('/icons/upload',content=SVG,headers=headers);self.assertEqual(response.status_code,200)
  self.assertEqual(response.json()['mode'],'manual')
  self.assertEqual(self.client.post('/icons/upload',content=b'x'*(icons.MAX_ICON+1),headers=headers).status_code,413)
  headers['x-icon-name']='bad.html';self.assertEqual(self.client.post('/icons/upload',content=SVG,headers=headers).status_code,400)
 def test_06_save_serve_and_delete_icon(self):
  icon=icons.record(SVG)
  self.assertEqual(self.client.post('/projects/save',data=self.form(icon_payload=json.dumps(icon))).status_code,200)
  response=self.client.get('/projects/alpha/icon');self.assertEqual(response.status_code,200);self.assertIn('image/svg+xml',response.headers['content-type'])
  self.assertIn("default-src 'none'; sandbox",response.headers['content-security-policy'])
  self.assertEqual(response.content,base64.b64decode(icon['data']))
  self.assertIn('/projects/alpha/icon?v=',self.client.get('/').text)
  self.client.post('/projects/alpha/delete',data={'csrf':self.token,'confirm':'on'})
  self.assertIsNone(icons.get('alpha'));self.assertEqual(self.client.get('/projects/alpha/icon').status_code,404)
 def test_07_manual_retained_auto_updates_and_failure_preserves(self):
  manual=icons.record(SVG);automatic=icons.record(SVG.replace(b'148b9f',b'8b5cf6'),'auto','https://example.org/favicon.svg')
  icons.save('alpha',manual)
  with patch.object(icons,'discover',AsyncMock(return_value=automatic)) as fetch:
   asyncio.run(icons.refresh({'id':'alpha','url':'https://example.org'}));fetch.assert_not_awaited();self.assertEqual(icons.get('alpha'),manual)
  icons.save('alpha',{**manual,'mode':'auto'})
  with patch.object(icons,'discover',AsyncMock(return_value=automatic)):asyncio.run(icons.refresh({'id':'alpha','url':'https://example.org'}))
  self.assertEqual(icons.get('alpha'),automatic)
  with patch.object(icons,'discover',AsyncMock(side_effect=ValueError('not found'))):asyncio.run(icons.refresh({'id':'alpha','url':'https://example.org'}))
  self.assertEqual(icons.get('alpha'),automatic)
 def test_08_icon_configuration_and_archive_roundtrip(self):
  icon=icons.record(SVG);icons.save('alpha',icon)
  for history in (False,True):
   self.select('source');path=self.base/('archive.zip' if history else 'config.json');transfer.export_to(path,history=history);p=transfer.read_package(path)
   self.assertEqual(p['version'],transfer.VERSION if history else transfer.CONFIG_VERSION)
   self.assertEqual(p['project_icons']['alpha'],icon)
   self.select('dest'+str(history));transfer.import_package(path,p);self.assertEqual(icons.get('alpha'),icon)
 def test_09_tampered_icon_import_rejected(self):
  icons.save('alpha',icons.record(SVG));path=self.base/'config.json';p=transfer.export_to(path)
  p['project_icons']['alpha']['sha256']='0'*64
  with self.assertRaises(transfer.Invalid):transfer.validate(p)
 def test_10_age_uses_largest_unit_and_success_timestamp(self):
  now=datetime.now(timezone.utc)
  for delta,want in ((timedelta(minutes=5),'vor 5 Minuten'),(timedelta(hours=3,minutes=8),'vor 3 Stunden'),(timedelta(days=2,hours=7),'vor 2 Tagen')):
   self.assertEqual(web.relative_age((now-delta).isoformat()),want)
  old=(now-timedelta(hours=3)).isoformat();store.execute('UPDATE projects SET last_success=?',(old,))
  store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',('failed','alpha',store.now(),'failed','test'))
  doc=BeautifulSoup(self.client.get('/').text,'html.parser');self.assertEqual(doc.select_one('[data-relative-time]')['data-relative-time'],old)
 def mail_data(self):
  start='2026-11-01T00:00:00+00:00';end='2026-11-05T08:00:00+00:00'
  projects=[{'id':'quiet','name':'A-Unverändert','url':'https://quiet.example','tags':'["Arbeit"]'}, {'id':'change','name':'Z-Änderung','url':'https://change.example','tags':'["Arbeit","Privat"]'},{'id':'bad','name':'Fehler','url':'https://bad.example','tags':'["Arbeit"]'},{'id':'new','name':'Ungeprüft','url':'https://new.example','tags':'[]'}]
  runs=[{'project_id':p['id'],'started':start,'finished':end,'status':'failed' if p['id']=='bad' else 'success','error':'Timeout','summary':'### Neuigkeit\nDie Frist wurde geändert.'} for p in projects if p['id']!='new']
  changes=[{'project_id':'change','kind':'Inhalt geändert','url':'https://change.example/law','evidence':json.dumps({'text_diff':'--- old\n+++ new\n-Frist 10 Tage\n+Frist 11 Tage','ai_assessment':{'relevance':'relevant','reason':'Eine Ziffer der Frist wurde korrigiert.'}})}]
  return projects,runs,changes,[{'name':'Arbeit','color':'#1967d2'},{'name':'Privat','color':'#8b5cf6'}],start,end
 def test_11_tag_grouping_changed_first_quiet_last(self):
  model=mails.build_model(*self.mail_data());text,html=mails.compose(*self.mail_data());soup=BeautifulSoup(html,'html.parser')
  self.assertEqual([h['data-tag-name'] for h in soup.select('[data-tag-name]')],['Arbeit','Privat','Arbeit'])
  self.assertEqual([row['data-change-project-id'] for row in soup.select('[data-change-project-id]')],['change'])
  self.assertLess(html.index('Z-Änderung'),html.index('A-Unverändert'))
  self.assertEqual(model['projects']['quiet']['category'],'unchanged')
  self.assertEqual(model['projects']['change']['category'],'changed')
  self.assertIn('Eine Ziffer der Frist wurde korrigiert.',html);self.assertNotIn('--- old',html)
  self.assertNotIn('Frist 10 Tage',html);self.assertNotIn('Frist 11 Tage',html)

 def test_12_failure_and_unchecked_are_never_claimed_unchanged(self):
  model=mails.build_model(*self.mail_data());text,html=mails.compose(*self.mail_data())
  self.assertEqual(model['projects']['bad']['category'],'notice')
  self.assertEqual(model['projects']['new']['category'],'notice')
  soup=BeautifulSoup(html,'html.parser')
  self.assertEqual([row['data-project-id'] for row in soup.select('[data-category=unchanged]')],['quiet'])
  for value in ('Fehler','Ungeprüft','fehlgeschlagen','Im Berichtszeitraum nicht geprüft'):
   self.assertNotIn(value,text+html)

 def test_13_mail_injection_escaped_and_no_tracking_images(self):
  data=list(self.mail_data());data[0][1]['name']='<img src=x onerror=alert(1)>';data[2][0]['evidence']=json.dumps({'note':'<script>evil()</script>**Wichtig** ![track](https://evil.test/a)'})
  _,html=mails.compose(*data);soup=BeautifulSoup(html,'html.parser')
  self.assertFalse(soup.select('script,iframe,style,[onerror]'));self.assertIn('&lt;img',html);self.assertIn('Wichtig',html)
  self.assertTrue(all(i['src'].startswith('cid:') for i in soup.select('img')))

 def test_14_empty_period_is_honest(self):
  data=list(self.mail_data());data[1]=[];data[2]=[];text,html=mails.compose(*data)
  self.assertIn('Für diesen Zeitraum liegen keine Quellen im Bericht vor.',html)
  self.assertIn('0 überwachte Quellen',text)
  self.assertNotIn('data-change-table',html)
 def test_15_multipart_alternative_and_readable_plaintext(self):
  bundle=mails.render(mails.build_model(*self.mail_data()));smtp=MagicMock()
  with patch.dict(os.environ,{'KIWM_TEST_SMTP_HOST':'mailpit'}),patch.object(gateway.smtplib,'SMTP',return_value=smtp),patch.object(gateway,'settings',return_value={}):gateway.send_mail(bundle['text'],'Test',True,bundle['html'],bundle['images'])
  message=smtp.__enter__.return_value.send_message.call_args.args[0]
  parsed=BytesParser(policy=policy.default).parsebytes(message.as_bytes())
  self.assertEqual(parsed.get_content_type(),'multipart/alternative');self.assertEqual(parsed.get_body('html').get_content_type(),'text/html')
  self.assertIn('ARBEIT',parsed.get_body('plain').get_content());self.assertNotIn('###',parsed.get_body('plain').get_content())
  self.assertEqual(parsed.get_body('html').get_content().rstrip(),bundle['html'].rstrip())
  self.assertTrue(any(p.get_content_type()=='multipart/related' for p in parsed.walk()))

 def test_16_real_discovery_code_handles_links_redirects_no_cookies(self):
  calls=[];real=httpx.AsyncClient
  def handler(request):
   calls.append(request);self.assertNotIn('cookie',request.headers);self.assertNotIn('authorization',request.headers)
   if request.url.path=='/':return httpx.Response(200,text='<link rel="icon" href="/forward">',headers={'set-cookie':'anonymous=ignored; Path=/'})
   if request.url.path=='/forward':return httpx.Response(302,headers={'location':'/icon.svg'})
   return httpx.Response(200,content=SVG,headers={'content-type':'image/svg+xml'})
  with patch.object(icons.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),timeout=5,event_hooks=kw.get('event_hooks',{}))):
   icon=asyncio.run(icons.discover('https://example.org/'))
  self.assertEqual(icon['mode'],'auto');self.assertEqual(len(calls),3);self.assertTrue(icon['url'].endswith('/icon.svg'))
 def test_17_redirect_to_lan_blocked(self):
  real=httpx.AsyncClient
  def handler(request):return httpx.Response(302,headers={'location':'http://192.168.50.8/'})
  with patch.object(icons.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler),timeout=5)),self.assertRaises(ValueError):asyncio.run(icons.discover('https://example.org/'))
 def test_18_manual_clear_schedules_safe_refresh(self):
  icons.save('alpha',icons.record(SVG))
  with patch.object(icons,'refresh',AsyncMock()) as refresh:
   self.client.post('/projects/save',data=self.form(icon_action='clear'))
   refresh.assert_awaited_once()
  self.assertIsNone(icons.get('alpha'))

 def test_19_successful_retry_clears_current_error_count(self):
  data=list(self.mail_data())
  data[1].append({'project_id':'quiet','started':'2026-10-31T22:00:00+00:00','finished':'2026-10-31T22:01:00+00:00','status':'failed','error':'Earlier failure'})
  model=mails.build_model(*data);text,html=mails.compose(*data)
  self.assertEqual(model['counts']['failed'],1)
  self.assertEqual(model['projects']['quiet']['category'],'unchanged');self.assertNotIn('Earlier failure',text)

 def test_20_collection_mail_renders_linked_entries_instead_of_raw_json_diff(self):
  data=list(self.mail_data())
  evidence={'text_diff':'RAW_JSON_DIFF_CANARY','collection':{'added':[{'title':'DIN VDE 0100-999:2026-10','url':'https://change.example/new'}],'updated':[{'before':{'date':'2026-09'},'after':{'date':'2026-10'}}]}}
  data[2][0]['evidence']=json.dumps(evidence)
  text,html=mails.compose(*data);soup=BeautifulSoup(html,'html.parser')
  self.assertIn('1 neue und 1 aktualisierte Einträge erkannt.',html)
  self.assertNotIn('RAW_JSON_DIFF_CANARY',text+html);self.assertNotIn('2026-09',text+html)
  self.assertIsNotNone(soup.find('a',href='https://change.example'))
  self.assertEqual(len(soup.select('[data-project-id="change"][data-category="changed"]')),2)

 def test_21_mail_uses_shared_priority_for_technical_versions_and_uncertain_hints(self):
  data=list(self.mail_data())
  def change(kind,relevance,reason):return {'project_id':'change','kind':kind,'url':'https://change.example/a','evidence':json.dumps({'ai_assessment':{'relevance':relevance,'reason':reason}})}
  data[2]=[change('Datei technisch geändert','relevant','Neue Originaldateifassung relevant.'),change('Datei technisch geändert','uncertain','Fachliche Bedeutung unsicher.'),change('Datei technisch geändert','irrelevant','IRRELEVANT_MAIL_CANARY'),change('Visuelle Abweichung','relevant','Visueller Hinweis ohne Textänderung.')]
  original=copy.deepcopy(data[2]);model=mails.build_model(*data);text,html=mails.compose(*data)
  self.assertEqual(data[2],original);self.assertEqual(model['projects']['change']['change_count'],1)
  self.assertEqual(model['projects']['change']['notice_count'],2)
  self.assertEqual(model['projects']['change']['category'],'changed')
  self.assertNotIn('IRRELEVANT_MAIL_CANARY',text+html)
  self.assertEqual(len(model['projects']['change']['evidence']),1)

 def test_22_cancelled_unstarted_retries_do_not_replace_real_results_or_count_as_checks(self):
  data=list(self.mail_data())
  for pid in ('quiet','bad','new'):data[1].append({'project_id':pid,'started':'2026-11-04T12:00:00+00:00','finished':'2026-11-04T12:00:01+00:00','status':'cancelled','error':'CANCELLED_NOT_A_CHECK','summary':'CANCELLED_NOT_A_CHECK','origin':'retry'})
  saved=copy.deepcopy(data[1]);model=mails.build_model(*data);text,html=mails.compose(*data)
  self.assertEqual(data[1],saved)
  self.assertEqual({k:model['counts'][k] for k in ('checked','successful','failed')},
                   {'checked':3,'successful':2,'failed':1})
  self.assertIn('2 überwachte Quellen',text)
  self.assertEqual(model['projects']['quiet']['category'],'unchanged')
  self.assertEqual(model['projects']['bad']['category'],'notice');self.assertEqual(model['projects']['new']['category'],'notice')
  self.assertNotIn('CANCELLED_NOT_A_CHECK',text+html)

 def test_23_source_coverage_counts_read_urls_not_differences_or_dom_nodes(self):
  observations=[{'url':'https://sources.test/','kind':'page'},
                {'url':'https://sources.test/detail','kind':'page'},
                {'url':'https://sources.test/detail','kind':'page'},
                {'url':'https://sources.test/rule.pdf','kind':'document'}]
  coverage=engine.source_coverage(observations,'https://sources.test/')
  self.assertEqual(coverage['distinct_pages_read'],2)
  self.assertEqual(coverage['other_pages_read'],1)
  self.assertTrue(coverage['start_page_read'])
  self.assertEqual(coverage['distinct_documents_archived'],1)
  self.assertEqual(coverage['page_urls'],['https://sources.test/','https://sources.test/detail'])

 def test_24_period_change_does_not_append_later_unchanged_summary(self):
  data=list(self.mail_data());reason='Die unveränderte Ausgabe wurde in die überwachte Kategorie verschoben.'
  data[1].append({'id':'later','project_id':'change','started':'2026-11-04T12:00:00+00:00','status':'success','error':None,'summary':'LATEST_UNCHANGED_SUMMARY'})
  evidence={'ai_assessment':{'relevance':'relevant','reason':reason}}
  data[2][0]['evidence']=json.dumps(evidence);original=copy.deepcopy(data)
  text,html=mails.compose(*data);self.assertEqual(data,original)
  self.assertIn(reason,text+html);self.assertNotIn('LATEST_UNCHANGED_SUMMARY',text+html)
  for relevance,category in (('irrelevant','unchanged'),('uncertain','notice')):
   evidence['ai_assessment']['relevance']=relevance;data[2][0]['evidence']=json.dumps(evidence)
   model=mails.build_model(*data);self.assertEqual(model['projects']['change']['category'],category)
   self.assertNotIn('LATEST_UNCHANGED_SUMMARY',mails.render(model)['html'])

if __name__=='__main__':unittest.main(verbosity=2)
