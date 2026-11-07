"""Isolated portable-export acceptance tests. Never writes production settings."""
import os,tempfile,json,sqlite3,unittest,zipfile,io,copy
from pathlib import Path
from unittest.mock import patch

scratch=tempfile.TemporaryDirectory(prefix='.transfer-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store,transfer,web,engine,configuration
from fixture import pdf
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

class TransferTests(unittest.TestCase):
 def setUp(self):
  self.directory=tempfile.TemporaryDirectory(dir=scratch.name)
  self.base=Path(self.directory.name)
  self.select('source')
  clean=patch.dict(os.environ,dict.fromkeys(configuration.ENVIRONMENT_NAMES,''))
  clean.start();self.addCleanup(clean.stop)
  self.seed()
 def tearDown(self):self.directory.cleanup()
 def select(self,name):
  folder=self.base/name;folder.mkdir(exist_ok=True)
  store.DATA=web.DATA=engine.DATA=folder
  store.CONFIG=web.CONFIG=folder/'settings.json'
  store.SECRETS=web.SECRETS=folder/'credentials.json'
  store.init()
  self.root=folder
 def seed(self):
  store.save_json(store.CONFIG,{'omlx_url':'http://192.0.2.2:8000/v1','agent_model':'Local Vision','analysis_model':'Local Vision','vision_model':'Local Vision','smtp_enabled':True,'smtp_host':'smtp.example.org','smtp_port':587})
  store.save_json(store.SECRETS,{'omlx_key':'FAKE-TEST-KEY','smtp_password':'FAKE-TEST-PASSWORD'})
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,color,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('alpha','Büro & Recht','https://example.org','Beachte jede Zahl und jedes Datum.','example.org','0 6 * * *',1,store.next_due('0 6 * * *'),store.now(),'#0f766e',json.dumps(['Arbeit','Richtlinien'])))
  for rid,days in [('old',30),('new',45)]:
   store.execute('INSERT INTO runs(id,project_id,started,finished,status,summary,origin) VALUES(?,?,?,?,?,?,?)',(rid,'alpha',store.now(),store.now(),'success','## Ergebnis\nFrist prüfen.','test'))
   engine.archive({'id':'alpha'},rid,'https://example.org/rules.pdf','document',pdf('Deadline '+str(days)+' days'),'Deadline '+str(days)+' days',suffix='.pdf')
   store.event(rid,'PDF geöffnet')
  self.change=store.one('SELECT * FROM changes')['id']
  store.execute('INSERT INTO change_reviews VALUES(?,?)',(self.change,store.now()))
  store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status) VALUES(?,?,?,?,?,?,?)',('report','week-test',store.now(),store.now(),store.now(),'Bericht mit Friständerung','failed'))
  store.execute('INSERT INTO report_changes VALUES(?,?)',(self.change,'report'))
  store.execute('INSERT INTO state VALUES(?,?)',('content_scopes:alpha',json.dumps({'https://example.org':'css:main'})))
  (self.root/'artifacts'/'new'/'evidence.json').write_text('{"read":true}')
 def package(self,history=True,secrets=False):
  path=self.base/('archive.zip' if history else 'config.json')
  transfer.export_to(path,history,secrets)
  return path,transfer.read_package(path)
 def test_01_json_config_roundtrip_and_credentials_choice(self):
  path,p=self.package(False)
  self.assertIsNone(p['credentials']);self.assertNotIn(b'FAKE-TEST',path.read_bytes())
  self.assertEqual(p['projects'][0]['color'],'#0f766e');self.assertEqual(p['projects'][0]['prompt'],'Beachte jede Zahl und jedes Datum.')
  self.select('target');store.save_json(store.SECRETS,{'omlx_key':'KEEP'})
  r=transfer.import_package(path,p)
  self.assertEqual(store.one('SELECT enabled FROM projects')['enabled'],0)
  self.assertEqual(json.loads(store.one('SELECT tags FROM projects')['tags']),['Arbeit','Richtlinien'])
  self.assertEqual(store.rows('SELECT * FROM runs'),[])
  self.assertFalse(store.settings()['smtp_enabled']);self.assertEqual(store.credentials()['omlx_key'],'KEEP')
  self.assertTrue((self.root/r['recovery']/'monitor.db').exists())
 def test_02_zip_history_files_and_idempotence(self):
  path,p=self.package(True,True);self.select('target')
  transfer.import_package(path,p,apply_secrets=True,activate=True)
  for table in transfer.TABLES:self.assertEqual(len(store.rows('SELECT * FROM '+table)),len(p['history'][table]))
  for rel,entry in p['files'].items():self.assertEqual(transfer.file_hash(self.root/rel),entry['sha256'])
  self.assertEqual(store.credentials()['omlx_key'],'FAKE-TEST-KEY')
  self.assertEqual(store.one('SELECT enabled FROM projects')['enabled'],1)
  self.assertEqual(store.one('SELECT mail_status FROM reports')['mail_status'],'imported')
  again=transfer.import_package(path,p,activate=True)
  self.assertTrue(all(v==0 for v in again['added'].values()));self.assertEqual(again['files'],0)
  self.assertEqual(store.one("SELECT value FROM state WHERE key='content_scopes:alpha'")['value'],p['content_scopes'][0]['value'])
  client=TestClient(web.app)
  sid=store.one('SELECT id FROM snapshots')['id'];self.assertEqual(client.get('/documents/'+sid).status_code,200)
  file=p['history']['snapshots'][0]['file'];self.assertEqual(client.get('/artifacts/'+file.removeprefix('artifacts/')).status_code,200)
  self.assertEqual(client.get('/projects/alpha').status_code,200)
 def test_03_evidence_conflict_is_rejected_without_mutation(self):
  path,p=self.package();before=store.one('SELECT name FROM projects')['name']
  p['projects'][0]['name']='SHOULD NOT CHANGE';p['history']['changes'][0]['evidence']='{}'
  with self.assertRaises(transfer.Invalid):transfer.import_package(path,p)
  self.assertEqual(store.one('SELECT name FROM projects')['name'],before)
 def test_04_reject_archive_path_links_checksum_and_extra_files(self):
  path,p=self.package();raw=path.read_bytes()
  with zipfile.ZipFile(io.BytesIO(raw)) as z:entries={i.filename:z.read(i) for i in z.infolist()}
  for kind in ('traversal','symlink','checksum','extra','version'):
   bad=self.base/(kind+'.zip');items=dict(entries);payload=copy.deepcopy(p)
   if kind=='traversal':payload['files']['artifacts/../../secrets/stolen']={'size':0,'sha256':'0'*64}
   if kind=='checksum':
    rel=next(iter(payload['files']));items['files/'+rel]=b'corrupted'
   if kind=='extra':items['files/artifacts/unlisted.txt']=b'extra'
   if kind=='version':payload['version']=999
   items['webmonitor.json']=transfer.dump(payload)
   with zipfile.ZipFile(bad,'w') as z:
    for name,data in items.items():
     if kind=='symlink' and name.startswith('files/'):
      info=zipfile.ZipInfo(name);info.external_attr=(0o120777<<16);z.writestr(info,data)
     else:z.writestr(name,data)
   with self.assertRaises(transfer.Invalid,msg=kind):transfer.read_package(bad)
 def test_05_transaction_rolls_back_files_config_and_database(self):
  path,p=self.package(True,True);self.select('target')
  store.save_json(store.CONFIG,{'agent_model':'KEEP'})
  original=store.save_json
  def failing(target,value):
   if target==store.SECRETS:raise OSError('simulated full filesystem')
   return original(target,value)
  with patch('store.save_json',side_effect=failing):
   with self.assertRaises(OSError):transfer.import_package(path,p,apply_secrets=True)
  self.assertEqual(store.rows('SELECT * FROM projects'),[])
  self.assertEqual(store.settings(),{'agent_model':'KEEP'})
  self.assertFalse(any((self.root/rel).exists() for rel in p['files']))
 def client(self):
  c=TestClient(web.app);c.get('/transfer');return c,c.cookies['kiwm_csrf']
 def test_06_http_preview_then_import_and_session_binding(self):
  path,p=self.package(False);self.select('target');c,token=self.client()
  response=c.post('/transfer/preview',data={'csrf':token},files={'file':('config.json',path.read_bytes(),'application/json')})
  self.assertEqual(response.status_code,200,response.text);self.assertIn('Importvorschau',response.text)
  self.assertEqual(store.rows('SELECT * FROM projects'),[])
  import_token=BeautifulSoup(response.text,'html.parser').select_one('[name=token]')['value']
  other,csrf=self.client();bad=other.post('/transfer/import',data={'csrf':csrf,'token':import_token,'confirm':'on'})
  self.assertEqual(bad.status_code,400);self.assertEqual(store.rows('SELECT * FROM projects'),[])
  response=c.post('/transfer/import',data={'csrf':token,'token':import_token,'confirm':'on','apply_settings':'on'})
  self.assertEqual(response.status_code,200,response.text);self.assertIn('Import abgeschlossen',response.text)
  self.assertEqual(store.one('SELECT enabled FROM projects')['enabled'],0)
  self.assertEqual(c.post('/transfer/import',data={'csrf':token,'token':import_token,'confirm':'on'}).status_code,400)
 def test_07_http_csrf_origin_confirmation_and_download(self):
  path,p=self.package(False);c,token=self.client();raw=path.read_bytes()
  self.assertEqual(c.post('/transfer/preview',data={'csrf':'bad'},files={'file':('x.json',raw)}).status_code,403)
  self.assertEqual(c.post('/transfer/preview',data={'csrf':token},files={'file':('x.json',raw)},headers={'Origin':'http://evil.invalid'}).status_code,403)
  self.assertEqual(c.post('/transfer/import',data={'csrf':token}).status_code,400)
  response=c.post('/transfer/export',data={'csrf':token,'kind':'configuration'})
  self.assertEqual(response.status_code,200);self.assertIn('attachment',response.headers['content-disposition'])
  self.assertIsNone(response.json()['credentials'])
  response=c.post('/transfer/export',data={'csrf':token,'kind':'archive','include_secrets':'on'})
  self.assertEqual(response.status_code,200);self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.content)))
 def test_08_malformed_input_and_busy_import_do_not_modify_data(self):
  path,p=self.package(False);c,token=self.client()
  for value in (b'not json',b'{}',transfer.dump({**p,'version':transfer.VERSION+1}),transfer.dump({**p,'projects':[{'id':'bad'}]})):
   response=c.post('/transfer/preview',data={'csrf':token},files={'file':('broken.json',value)})
   self.assertEqual(response.status_code,400,response.text)
  store.execute("INSERT INTO runs(id,project_id,started,status,origin) VALUES('queued','alpha',?,'queued','test')",(store.now(),))
  with self.assertRaises(transfer.Invalid):transfer.import_package(path,p)
  self.assertEqual(c.post('/transfer/export',data={'csrf':token,'kind':'configuration'}).status_code,409)
 def test_09_unrelated_projects_preserved_and_original_backup_readable(self):
  path,p=self.package(False)
  store.execute("UPDATE projects SET name='Locally edited' WHERE id='alpha'")
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('other','Private','https://example.org','Keep','example.org','0 6 * * *',0,store.next_due('0 6 * * *'),store.now()))
  result=transfer.import_package(path,p,apply_settings=False,activate=True)
  self.assertEqual(len(store.rows('SELECT id FROM projects')),2)
  self.assertEqual(store.one("SELECT name FROM projects WHERE id='other'")['name'],'Private')
  with sqlite3.connect(self.root/result['recovery']/'monitor.db') as c:self.assertEqual(c.execute("SELECT name FROM projects WHERE id='alpha'").fetchone()[0],'Locally edited')
 def site_auth(self,enabled=True):
  store.execute("UPDATE projects SET url='https://www.elektro.net/praxisprobleme/',domains='www.elektro.net' WHERE id='alpha'")
  value={'provider':'elektro_subscription','origin':'https://www.elektro.net','enabled':enabled}
  store.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('site_auth:alpha',json.dumps(value)))
  return value
 def read_site_auth(self,pid='alpha'):
  row=store.one('SELECT value FROM state WHERE key=?',('site_auth:'+pid,))
  return json.loads(row['value']) if row else None
 def test_10_legacy_v1_preserves_existing_site_auth(self):
  self.site_auth();path,p=self.package(False)
  p['version']=1;p.pop('site_auth');p.pop('tag_registry')
  for item in p['projects']:
   item.pop('use_global_schedule');item.pop('schedule_config');item.pop('telegram_enabled')
  path.write_bytes(transfer.dump(p))
  legacy=transfer.read_package(path)
  self.select('legacy-target');self.seed();expected=self.site_auth(True)
  vault=store.SECRETS.with_name('site-logins.json');vault.write_text('{"local-vault":"KEEP-SITE-ACCESS"}')
  before=vault.read_bytes()
  transfer.import_package(path,legacy,activate=False)
  self.assertEqual(self.read_site_auth(),expected)
  self.assertEqual(vault.read_bytes(),before)
  self.assertNotIn('site_auth',legacy)
 def test_11_v2_public_auth_roundtrip_in_json_and_zip(self):
  expected=self.site_auth();packages=[self.package(False),self.package(True)]
  for index,(path,p) in enumerate(packages):
   self.assertEqual(p['version'],transfer.VERSION)
   self.assertEqual(p['site_auth'],{'alpha':expected})
   self.select('auth-target-'+str(index))
   self.assertEqual(transfer.preview(p)['site_auth'],1)
   transfer.import_package(path,p,activate=False)
   self.assertEqual(self.read_site_auth(),{**expected,'enabled':False})
   self.assertFalse(store.SECRETS.with_name('site-logins.json').exists())
 def test_12_website_credentials_never_exported_with_any_option(self):
  self.site_auth()
  canary='SUBSCRIBER-PRIVATE-DO-NOT-EXPORT';postal='98764'
  private={'alpha':{'provider':'elektro_subscription','origin':'https://www.elektro.net','abonummer':canary,'plz':postal}}
  store.SECRETS.with_name('site-logins.json').write_text(json.dumps(private))
  # Explicitly filter the old API/SMTP file too, even if an unexpected future
  # credential field is accidentally placed there.
  store.save_json(store.SECRETS,{**store.credentials(),'site_logins':private})
  for history in (False,True):
   for secrets in (False,True):
    path,p=self.package(history,secrets)
    if history:
     with zipfile.ZipFile(path) as z:
      self.assertFalse(any('site-logins' in name for name in z.namelist()))
      raw=b'\n'.join(z.read(name) for name in z.namelist())
    else:raw=path.read_bytes()
    self.assertNotIn(canary.encode(),raw);self.assertNotIn(postal.encode(),raw)
    self.assertNotIn('abonummer',json.dumps(p));self.assertNotIn('site_logins',json.dumps(p))
    self.assertEqual(set(p['credentials'] or {}),{'omlx_key','smtp_password'} if secrets else set())
 def test_13_v2_activation_requires_flag_and_retains_disabled_state(self):
  expected=self.site_auth();path,p=self.package(False)
  self.select('active-target')
  transfer.import_package(path,p,activate=True)
  self.assertEqual(self.read_site_auth(),expected)
  transfer.import_package(path,p,activate=False)
  self.assertFalse(self.read_site_auth()['enabled'])
  p['site_auth']['alpha']['enabled']=False
  transfer.import_package(path,p,activate=True)
  self.assertFalse(self.read_site_auth()['enabled'])
 def test_14_v2_empty_map_only_clears_imported_public_profiles(self):
  self.site_auth();path,p=self.package(False)
  vault=store.SECRETS.with_name('site-logins.json');vault.write_text('{"alpha":"LOCAL-ONLY"}')
  original_vault=vault.read_bytes()
  other={'provider':'elektro_subscription','origin':'https://www.elektro.net','enabled':True}
  store.execute('INSERT INTO state VALUES(?,?)',('site_auth:other',json.dumps(other)))
  p['site_auth']={}
  transfer.import_package(path,p,apply_secrets=True,activate=True)
  self.assertIsNone(self.read_site_auth())
  self.assertEqual(self.read_site_auth('other'),other)
  self.assertEqual(vault.read_bytes(),original_vault)
  self.site_auth();p.pop('site_auth')
  transfer.import_package(path,p)
  self.assertEqual(p['site_auth'],{})
  self.assertIsNone(self.read_site_auth())
  self.assertEqual(vault.read_bytes(),original_vault)
 def test_15_reject_unbound_origins_providers_and_private_auth_fields(self):
  self.site_auth();path,p=self.package(False)
  invalid=[]
  for field,value in (('origin','https://evil.invalid'),('origin','http://www.elektro.net'),('origin','https://www.elektro.net.evil.invalid'),('origin','https://www.elektro.net/'),('provider','generic-form'),('enabled',1),('selector','input'),('abonummer','PRIVATE')):
   payload=copy.deepcopy(p);payload['site_auth']['alpha'][field]=value;invalid.append(payload)
  for url in ('https://evil.invalid/praxisprobleme/','http://www.elektro.net/praxisprobleme/','https://www.elektro.net:8443/praxisprobleme/'):
   payload=copy.deepcopy(p);payload['projects'][0]['url']=url;payload['projects'][0]['domains']=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url).hostname;invalid.append(payload)
  payload=copy.deepcopy(p);payload['site_auth']['unrelated']=payload['site_auth'].pop('alpha');invalid.append(payload)
  payload=copy.deepcopy(p);payload['version']=1;invalid.append(payload)
  for payload in invalid:
   with self.assertRaises(transfer.Invalid):transfer.validate(payload)
  self.assertTrue(self.read_site_auth()['enabled'])
 def test_16_auth_metadata_rolls_back_and_vault_is_untouched(self):
  self.site_auth();path,p=self.package(True,True);self.select('auth-rollback')
  expected={'provider':'elektro_subscription','origin':'https://www.elektro.net','enabled':False}
  store.execute('INSERT INTO state VALUES(?,?)',('site_auth:alpha',json.dumps(expected)))
  vault=store.SECRETS.with_name('site-logins.json');vault.write_text('{"alpha":"KEEP-VAULT-EXACT"}')
  before=vault.read_bytes();original=store.save_json
  def fail_secrets(target,value):
   if target==store.SECRETS:raise OSError('simulated secret-write failure')
   return original(target,value)
  with patch('store.save_json',side_effect=fail_secrets):
   with self.assertRaises(OSError):transfer.import_package(path,p,apply_secrets=True,activate=True)
  self.assertEqual(self.read_site_auth(),expected)
  self.assertEqual(vault.read_bytes(),before)
  self.assertEqual(store.rows('SELECT id FROM projects'),[])
 def test_17_legacy_v1_zip_restores_history_without_changing_auth(self):
  self.site_auth();path,p=self.package(True)
  with zipfile.ZipFile(path) as z:entries={name:z.read(name) for name in z.namelist()}
  p['version']=1;p.pop('site_auth');p.pop('tag_registry');p['history'].pop('run_retries')
  for item in p['projects']:
   item.pop('use_global_schedule');item.pop('schedule_config');item.pop('telegram_enabled')
  entries['webmonitor.json']=transfer.dump(p)
  legacy_path=self.base/'legacy-v1.zip'
  with zipfile.ZipFile(legacy_path,'w',zipfile.ZIP_DEFLATED) as z:
   for name,data in entries.items():z.writestr(name,data)
  legacy=transfer.read_package(legacy_path)
  self.select('legacy-archive-target')
  expected={'provider':'elektro_subscription','origin':'https://www.elektro.net','enabled':True}
  store.execute('INSERT INTO state VALUES(?,?)',('site_auth:alpha',json.dumps(expected)))
  transfer.import_package(legacy_path,legacy)
  self.assertEqual(self.read_site_auth(),expected)
  for table in transfer.LEGACY_TABLES:self.assertEqual(len(store.rows('SELECT * FROM '+table)),len(p['history'][table]))
  self.assertEqual(store.rows('SELECT * FROM run_retries'),[])

 def test_18_v3_global_schedule_and_unused_tag_colors_roundtrip(self):
  import tag_registry,scheduling
  selected={**scheduling.DEFAULT,'mode':'weekly','time':'19:40','weekday':3}
  s=store.settings();s['global_schedule']=selected;store.save_json(store.CONFIG,s)
  store.execute('UPDATE projects SET use_global_schedule=1,schedule_config=?',(json.dumps({**scheduling.DEFAULT,'mode':'interval','interval':5}),))
  unused=tag_registry.save('Noch unbenutzt','#cc44aa')
  path,p=self.package(False);self.assertEqual(p['version'],transfer.VERSION)
  self.assertTrue(any(t['id']==unused for t in p['tag_registry']))
  self.select('v3-target');transfer.import_package(path,p,activate=True)
  self.assertEqual(store.settings()['global_schedule'],selected)
  row=store.one("SELECT * FROM projects WHERE id='alpha'")
  self.assertEqual(row['use_global_schedule'],1)
  self.assertEqual(json.loads(row['schedule_config'])['interval'],5)
  self.assertEqual(store.one('SELECT color FROM tags WHERE id=?',(unused,))['color'],'#cc44aa')
  self.assertEqual(scheduling.project_schedule(row),selected)

 def test_19_real_legacy_v2_shape_keeps_individual_schedule(self):
  self.site_auth();path,p=self.package(False)
  p['version']=2;p.pop('tag_registry')
  for item in p['projects']:
   item.pop('use_global_schedule');item.pop('schedule_config');item.pop('telegram_enabled')
  path.write_bytes(transfer.dump(p));old=transfer.read_package(path)
  self.select('v2-target');transfer.import_package(path,old)
  row=store.one("SELECT * FROM projects WHERE id='alpha'")
  self.assertEqual(row['use_global_schedule'],0);self.assertEqual(row['schedule_config'],'{}')
  self.assertEqual(row['cron'],'0 6 * * *')
  self.assertEqual({r['name'] for r in store.rows('SELECT * FROM tags')},{'Arbeit','Richtlinien'})
  self.assertFalse(self.read_site_auth()['enabled'])

 def test_20_v3_rejects_bad_schedule_and_registry_before_writing(self):
  path,p=self.package(False)
  bad=[]
  q=copy.deepcopy(p);q['projects'][0]['use_global_schedule']=True;bad.append(q)
  q=copy.deepcopy(p);q['projects'][0]['schedule_config']='{"mode":"interval","interval":9999}';bad.append(q)
  q=copy.deepcopy(p);q['settings']['global_schedule']={'mode':'daily','time':'25:00'};bad.append(q)
  q=copy.deepcopy(p);q['tag_registry'][0]['color']='red;background:url(https://evil)';bad.append(q)
  q=copy.deepcopy(p);q['tag_registry']=[];bad.append(q)
  before=store.one("SELECT * FROM projects WHERE id='alpha'")
  for q in bad:
   with self.assertRaises(transfer.Invalid):transfer.validate(q)
  self.assertEqual(store.one("SELECT * FROM projects WHERE id='alpha'"),before)

 def test_21_generic_public_access_roundtrip_never_exports_site_secret(self):
  profile={'provider':'generic_form','origin':'https://example.org','enabled':True,
   'login_url':'https://example.org/login','submit_url':'https://example.org/login',
   'form_selector':'#login','username_selector':'input[name="username"]','password_selector':'input[name="password"]',
   'submit_selector':'button[type="submit"]','success_selector':'#signed-in','consent_selector':'','close_selector':''}
  store.execute('INSERT INTO state VALUES(?,?)',('site_auth:alpha',json.dumps(profile)))
  vault=store.SECRETS.with_name('site-logins.json');vault.write_text('GENERIC_PRIVATE_SECRET_CANARY')
  path,p=self.package(False)
  self.assertEqual(p['site_auth']['alpha'],profile);self.assertNotIn(b'GENERIC_PRIVATE_SECRET_CANARY',path.read_bytes())
  self.select('generic-target');target_vault=store.SECRETS.with_name('site-logins.json');target_vault.write_text('KEEP-LOCAL-VAULT')
  transfer.import_package(path,p,activate=False)
  self.assertEqual(self.read_site_auth(),{**profile,'enabled':False})
  self.assertEqual(target_vault.read_text(),'KEEP-LOCAL-VAULT')
  for field,value in [('origin','https://evil.example'),('submit_url','https://evil.example/login'),('password','secret-in-public-config')]:
   q=copy.deepcopy(p);q['site_auth']['alpha'][field]=value
   with self.assertRaises(transfer.Invalid):transfer.validate(q)

 def test_22_exports_use_persisted_values_even_with_secrets_and_environment(self):
  saved=store.persisted_settings();private=store.persisted_credentials()
  overrides={'KIWM_OMLX_URL':'http://env-export.test:9000/v1',
             'KIWM_OMLX_API_KEY':'SYNTHETIC-ENV-EXPORT-NEVER-SAVED',
             'KIWM_MODEL':'Synthetic Environment Model'}
  with patch.dict(os.environ,overrides):
   self.assertNotEqual(store.settings()['omlx_url'],saved['omlx_url'])
   self.assertNotEqual(store.credentials()['omlx_key'],private['omlx_key'])
   for history in (False,True):
    for secrets in (False,True):
     with self.subTest(history=history,secrets=secrets):
      path,payload=self.package(history,secrets)
      self.assertEqual(payload['settings'],saved)
      self.assertEqual(payload['credentials'],private if secrets else None)
      exported=transfer.dump(payload)
      for value in overrides.values():self.assertNotIn(value.encode(),exported)

 def test_23_import_merge_never_materializes_unimported_environment_values(self):
  path,payload=self.package(False,True)
  for field in ('omlx_url','agent_model'):payload['settings'].pop(field)
  payload['credentials'].pop('omlx_key')
  path.write_bytes(transfer.dump(payload));payload=transfer.read_package(path)
  self.select('environment-target')
  saved={'omlx_url':'http://keep-saved.test:8000/v1','agent_model':'Keep Saved Agent'}
  private={'omlx_key':'SYNTHETIC-KEEP-SAVED-TOKEN'}
  store.save_json(store.CONFIG,saved);store.save_json(store.SECRETS,private)
  overrides={'KIWM_OMLX_URL':'http://env-import.test:9000/v1',
             'KIWM_OMLX_API_KEY':'SYNTHETIC-ENV-IMPORT-NEVER-SAVED',
             'KIWM_MODEL':'Synthetic Import Environment Model'}
  with patch.dict(os.environ,overrides):
   transfer.import_package(path,payload,apply_secrets=True)
   self.assertEqual(store.settings()['omlx_url'],overrides['KIWM_OMLX_URL'])
   self.assertEqual(store.credentials()['omlx_key'],overrides['KIWM_OMLX_API_KEY'])
  self.assertEqual(store.persisted_settings()['omlx_url'],saved['omlx_url'])
  self.assertEqual(store.persisted_settings()['agent_model'],saved['agent_model'])
  self.assertEqual(store.persisted_credentials()['omlx_key'],private['omlx_key'])
  raw=store.CONFIG.read_bytes()+store.SECRETS.read_bytes()
  for value in overrides.values():self.assertNotIn(value.encode(),raw)

 def test_24_import_validates_model_url_and_token_without_environment_masking(self):
  path,payload=self.package(False,True)
  before=(store.CONFIG.read_bytes(),store.SECRETS.read_bytes())
  for section,key,value in (('settings','agent_model','M'*513),
                            ('settings','omlx_url','http://model.test:99999/v1'),
                            ('credentials','omlx_key','SYNTHETIC INVALID TOKEN')):
   with self.subTest(field=key):
    candidate=copy.deepcopy(payload);candidate[section][key]=value
    with patch.dict(os.environ,{'KIWM_MODEL':'Valid Environment Model',
                               'KIWM_OMLX_API_KEY':'SYNTHETIC-VALID-ENV-TOKEN'}):
     with self.assertRaises(transfer.Invalid) as error:transfer.import_package(path,candidate)
    self.assertNotIn(value,str(error.exception))
    self.assertEqual((store.CONFIG.read_bytes(),store.SECRETS.read_bytes()),before)

if __name__=='__main__':unittest.main(verbosity=2)
