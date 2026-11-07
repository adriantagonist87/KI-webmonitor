"""Scope/archive checks plus real Brave and HTTP PDFs; only the model is stubbed."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import quote
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from playwright.async_api import async_playwright

scratch=tempfile.TemporaryDirectory(prefix='.section-capture-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store,engine
from document_inventory import collect_documents,plan_documents,DOCUMENT_LIMIT
from relevance_test_model import review_only_model,assert_final_review

SOURCE='https://sections.test/'
FIXTURE='http://fixture:8000'
RULE=FIXTURE+'/inventory-test/rule.pdf?document=faq0'
COOKIE='kiwm_inventory_stage'


class SectionArchive(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=scratch.name)
  store.DATA=engine.DATA=Path(self.tmp.name);store.CONFIG=store.DATA/'settings.json';store.SECRETS=store.DATA/'credentials.json';store.init()
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('p','Sections',SOURCE,'Read selected sections.','sections.test,fixture','0 6 * * *',0,store.now(),store.now()))
  self.a=engine.section_scope(SOURCE,'panel-a');self.b=engine.section_scope(SOURCE,'panel-b')
 def tearDown(self):self.tmp.cleanup()
 def run_record(self,rid,status='running'):
  store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,'p',store.now(),status,'test'))
 def archive(self,rid,scope,text):
  folder=store.DATA/'artifacts'/rid;folder.mkdir(exist_ok=True)
  path=folder/(store.uid()+'.html.txt')
  raw='<main data-kiwm-scope="'+scope+'"><p>'+text+'</p></main>';path.write_text(raw,encoding='utf-8')
  return engine.archive({'id':'p'},rid,SOURCE,'page',raw.encode(),text,[],html=path.relative_to(store.DATA).as_posix(),scope=scope)
 def test_01_two_real_url_section_snapshots_and_reverse_order_no_changes(self):
  self.run_record('old')
  old_a=self.archive('old',self.a,'A content');old_b=self.archive('old',self.b,'B content')
  store.execute("UPDATE runs SET status='success',finished=? WHERE id='old'",(store.now(),))
  self.run_record('new');self.archive('new',self.b,'B content');self.archive('new',self.a,'A content')
  self.assertEqual(len(store.rows("SELECT id FROM snapshots WHERE run_id='new'")),2)
  self.assertEqual({r['url'] for r in store.rows('SELECT url FROM snapshots')},{SOURCE})
  self.assertFalse(store.rows('SELECT * FROM changes'))
  self.assertEqual(engine.previous_in_scope('p','new',SOURCE,self.a)[0]['id'],old_a)
  self.assertEqual(engine.previous_in_scope('p','new',SOURCE,self.b)[0]['id'],old_b)
 def test_02_recapture_updates_own_snapshot_and_changes_only(self):
  self.run_record('old');self.archive('old',self.a,'A 10');self.archive('old',self.b,'B 10')
  store.execute("UPDATE runs SET status='success' WHERE id='old'")
  self.run_record('new');a=self.archive('new',self.a,'A 11');b=self.archive('new',self.b,'B 11')
  before=store.one('SELECT * FROM snapshots WHERE id=?',(b,));b_changes=store.rows('SELECT * FROM changes WHERE current_id=?',(b,))
  self.assertEqual(self.archive('new',self.a,'A 12'),a)
  self.assertEqual(store.one('SELECT * FROM snapshots WHERE id=?',(b,)),before)
  self.assertEqual(store.rows('SELECT * FROM changes WHERE current_id=?',(b,)),b_changes)
  self.assertEqual(len(store.rows('SELECT * FROM changes WHERE current_id=?',(a,))),1)
 def test_03_previous_section_survives_newer_raw_attempts(self):
  self.run_record('old');a=self.archive('old',self.a,'A');store.execute("UPDATE runs SET status='success' WHERE id='old'")
  self.run_record('raw');self.archive('raw','main-content-v2','Raw');store.execute("UPDATE runs SET status='partial' WHERE id='raw'")
  self.assertEqual(engine.previous_in_scope('p','next',SOURCE,self.a)[0]['id'],a)
 def test_04_discard_transient_raw_snapshot_but_keep_physical_object(self):
  self.run_record('old');self.archive('old','main-content-v2','Old');store.execute("UPDATE runs SET status='success' WHERE id='old'")
  self.run_record('new');raw_id=self.archive('new','main-content-v2','Changed')
  path=store.DATA/store.one('SELECT html FROM snapshots WHERE id=?',(raw_id,))['html']
  self.assertTrue(store.rows('SELECT id FROM changes WHERE current_id=?',(raw_id,)))
  self.assertEqual(engine.discard_unselected_page_snapshots('new',SOURCE),[raw_id])
  self.assertTrue(path.is_file());self.assertFalse(store.one('SELECT id FROM snapshots WHERE id=?',(raw_id,)))
  self.assertFalse(store.rows('SELECT id FROM changes WHERE current_id=?',(raw_id,)))
 def test_05_frozen_inventory_aggregates_52_and_shadows_only_own_raw_url(self):
  def capture(urls):return {'metadata':{'links':[{'url':u,'label':'Document'} for u in urls]}}
  a=[FIXTURE+'/a'+str(n)+'.pdf' for n in range(15)];b=[FIXTURE+'/b'+str(n)+'.pdf' for n in range(37)]
  raw=capture([FIXTURE+'/never-opened.pdf']);frozen={(SOURCE,self.a):capture(a),(SOURCE,self.b):capture(b)}
  active={SOURCE:raw};before=json.dumps(raw)
  found=collect_documents(engine.inventory_captures(active,frozen),lambda u:True,engine.DOC_EXT)
  self.assertEqual(list(found),a+b);self.assertNotIn(FIXTURE+'/never-opened.pdf',found);self.assertEqual(json.dumps(raw),before)
  active['https://other.test/']=capture([FIXTURE+'/ordinary.pdf'])
  self.assertEqual(len(collect_documents(engine.inventory_captures(active,frozen),lambda u:True,engine.DOC_EXT)),53)
 def test_06_one_global_budget_counts_failures_and_duplicate_links(self):
  self.assertEqual(engine.DOCUMENT_LIMIT,DOCUMENT_LIMIT)
  urls=[FIXTURE+'/'+str(n)+'.pdf' for n in range(DOCUMENT_LIMIT+20)]
  todo,overflow=plan_documents(urls+[urls[2]+'#page=1'],{urls[0],urls[1]})
  self.assertEqual(len(todo),DOCUMENT_LIMIT-2);self.assertEqual(len(overflow),20);self.assertNotIn(urls[0],todo)
  todo2,overflow2=plan_documents(urls,set(urls[:DOCUMENT_LIMIT]));self.assertEqual(todo2,[]);self.assertEqual(len(overflow2),20)
 def test_07_collection_guard_remains_strict_and_discard_preserves_index(self):
  self.run_record('run');scope='collection-v1:confirmed';sid=self.archive('run',scope,'Index')
  self.assertEqual(engine.discard_unselected_page_snapshots('run',SOURCE),[])
  with self.assertRaisesRegex(ValueError,'Sammlungsvergleich'):self.archive('run',self.a,'A')
  self.assertEqual(store.one('SELECT text FROM snapshots WHERE id=?',(sid,))['text'],'Index')
 def test_07b_base_scope_is_independent_preserved_and_never_uses_old_raw_as_predecessor(self):
  base='page-base-v1:main'
  self.run_record('raw');self.archive('raw','main-content-v2','Raw prior');store.execute("UPDATE runs SET status='success' WHERE id='raw'")
  self.run_record('old');old=self.archive('old',base,'Direct 10');self.archive('old',self.a,'Panel 10')
  self.assertFalse(store.rows("SELECT id FROM changes WHERE run_id='old'"))
  store.execute("UPDATE runs SET status='success' WHERE id='old'")
  self.run_record('new');direct=self.archive('new',base,'Direct 11');panel=self.archive('new',self.a,'Panel 10')
  raw=self.archive('new','main-content-v2','Transient raw')
  self.assertEqual(engine.discard_unselected_page_snapshots('new',SOURCE),[raw])
  self.assertEqual(engine.previous_in_scope('p','new',SOURCE,base)[0]['id'],old)
  changes=store.rows("SELECT * FROM changes WHERE run_id='new'")
  self.assertEqual(len(changes),1);self.assertEqual(changes[0]['current_id'],direct)
  self.assertIsNotNone(store.one('SELECT id FROM snapshots WHERE id=?',(panel,)))
  captured={SOURCE:{'metadata':{'links':[{'url':FIXTURE+'/wrong.pdf','label':'Raw'}]}}}
  sections={(SOURCE,self.a):{'metadata':{'links':[{'url':FIXTURE+'/panel.pdf','label':'Panel'}]}}}
  bases={SOURCE:{'metadata':{'links':[{'url':FIXTURE+'/direct.pdf','label':'Direct'}]}}}
  actual=collect_documents(engine.inventory_captures(captured,sections,bases),lambda u:True,engine.DOC_EXT)
  self.assertEqual(set(actual),{FIXTURE+'/panel.pdf',FIXTURE+'/direct.pdf'})


@unittest.skipUnless(os.environ.get('BROWSER_CDP_URL'),'Select the isolated QA browser with BROWSER_CDP_URL')
class SectionMonitor(unittest.IsolatedAsyncioTestCase):
 async def test_08_exclusive_sections_real_brave_52_http_pdfs_and_one_stable_url_delta(self):
  tmp=tempfile.TemporaryDirectory(dir=scratch.name)
  store.DATA=engine.DATA=Path(tmp.name);store.CONFIG=store.DATA/'settings.json';store.SECRETS=store.DATA/'credentials.json';store.init()
  store.save_json(store.CONFIG,{k:'synthetic-no-model' for k in ('agent_model','analysis_model','vision_model')})
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('p','Sections',SOURCE,'Read both confirmed panels and all documents.','sections.test,fixture','0 6 * * *',0,store.now(),store.now()))
  project=store.one("SELECT * FROM projects WHERE id='p'")
  def document(group,n):return RULE if group=='FAQ' and n==0 else FIXTURE+'/guideline.pdf?document='+quote(group.casefold(),safe='')+str(n)
  def panel(ident,label,count):
   links=''.join('<p><a href="'+document(label,n)+'">'+label+' document '+str(n)+'</a></p>' for n in range(count))
   return '<details name="exclusive" id="'+ident+'"><summary>'+label+' ('+str(count)+')</summary>'+links+'</details>'
  html='<!doctype html><html><head><meta charset="utf-8"></head><body><main>'+panel('a','FAQ',15)+panel('b','Merkblätter',37)+panel('hidden','Never opened',1)+'</main></body></html>'
  async with async_playwright() as pw:
   browser=await pw.chromium.connect_over_cdp(engine.BROWSER_CDP_URL)
   context=await browser.new_context(viewport={'width':1200,'height':1000},service_workers='block')
   async def serve(route):
    if route.request.method!='GET':return await route.abort()
    return await route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html)
   class ContextProxy:
    @property
    def pages(self):return context.pages
    async def new_page(self):
     page=await context.new_page();await page.route(SOURCE+'**',serve);return page
    def __getattr__(self,name):return getattr(context,name)
   async def close_proxy():pass
   browser_proxy=SimpleNamespace(contexts=[ContextProxy()],close=close_proxy)
   class EnginePlaywright:
    async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser_proxy)))
    async def __aexit__(self,*args):pass
   async def request_counts():
    async with httpx.AsyncClient(proxy='http://proxy:3128',trust_env=False,timeout=15) as client:
     response=await client.get(FIXTURE+'/inventory-test/status');response.raise_for_status();return response.json()['rule']
   before=await request_counts();first_rule=None
   try:
    for stage,order in ((1,['FAQ (15)','Merkblätter (37)']),(2,['Merkblätter (37)','FAQ (15)']),(3,['FAQ (15)','Merkblätter (37)'])):
     await context.add_cookies([{'name':COOKIE,'value':'2' if stage==3 else '1','domain':'fixture','path':'/inventory-test','httpOnly':False,'secure':False,'sameSite':'Lax'}])
     rid='section-stage-'+str(stage);store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,'p',store.now(),'running','test'))
     async def agent(task,initial,model,llm,dispatch,screen,log,**kwargs):
      await dispatch('focus_content',{'selector':'main'})
      sources={}
      for label in order:
       result=await dispatch('expand_section',{'label':label});sources[label]=result['source']
       self.assertEqual(result['source']['url'],SOURCE);self.assertTrue(result['source']['scope'].startswith('section-v1:'))
      self.assertTrue(await context.pages[-1].locator('#a').get_attribute('open') is None or await context.pages[-1].locator('#b').get_attribute('open') is None)
      before_reload=store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='page' ORDER BY id",(rid,))
      # Explicit browser reload: read_sources now deliberately reuses the live
      # page instead of generating an unnecessary provider request.
      await context.pages[-1].reload(wait_until='domcontentloaded')
      reloaded=await dispatch('read_sources',{'urls':[SOURCE]})
      self.assertEqual(len(reloaded['sources']),1);self.assertTrue(reloaded['sources'][0]['section_snapshot_preserved'])
      self.assertIsNone(await context.pages[-1].locator('#a').get_attribute('open'))
      self.assertIsNone(await context.pages[-1].locator('#b').get_attribute('open'))
      self.assertEqual(store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='page' ORDER BY id",(rid,)),before_reload)
      reloaded_inventory=await dispatch('check_documents',{})
      self.assertEqual(reloaded_inventory['found'],52);self.assertEqual(reloaded_inventory['checked'],52)
      self.assertEqual(reloaded_inventory['failed'],[]);self.assertEqual(reloaded_inventory['unverified'],[])
      again=await dispatch('expand_section',{'label':'FAQ (15)'});self.assertEqual(again['source']['scope'],sources['FAQ (15)']['scope'])
      old=await dispatch('read_archive',{'url':SOURCE,'scope':sources['Merkblätter (37)']['scope']})
      self.assertIn('Merkblätter document 36',old['text']);self.assertNotIn('FAQ document',old['text'])
      inventory=await dispatch('check_documents',{})
      self.assertEqual(inventory['found'],52);self.assertEqual(inventory['checked'],52);self.assertEqual(inventory['failed'],[]);self.assertEqual(inventory['unverified'],[])
      return {'complete':True,'checked':['Both sections and 52 documents read.'],'pending':[],'findings':'Synthetic section check.','actions':[]}
     with patch('engine.async_playwright',EnginePlaywright),patch('engine.run_agent',new=agent),patch('engine.create_summary',new=AsyncMock(return_value='Sections checked.')),patch('engine.analyze_visual_changes',new=AsyncMock(return_value=[])),patch('engine.llm',new=AsyncMock(side_effect=review_only_model)) as model:
      await asyncio.wait_for(engine.monitor(project,rid),timeout=240)
     run=store.one('SELECT * FROM runs WHERE id=?',(rid,));self.assertEqual(run['status'],'success',run['error'])
     evidence=json.loads((store.DATA/'artifacts'/rid/'evidence.json').read_text())
     assert_final_review(self,evidence,rid,model,0)
     pages=[r for r in evidence['observations'] if r['kind']=='page'];self.assertEqual(len(pages),2)
     self.assertEqual({r['metadata']['title'] for r in pages},{'FAQ (15)','Merkblätter (37)'})
     self.assertTrue(all(r['url']==SOURCE for r in pages));self.assertTrue(all('Never opened' not in r['text'] for r in pages))
     scope_state=json.loads(store.one("SELECT value FROM state WHERE key='content_scopes:p'")['value']);self.assertEqual(scope_state[SOURCE],'main')
     rule=store.one("SELECT * FROM snapshots WHERE run_id=? AND url=? AND kind='document'",(rid,RULE))
     if stage==1:first_rule=rule
     changes=store.rows('SELECT * FROM changes WHERE run_id=?',(rid,))
     if stage<3:self.assertEqual(changes,[])
     else:
      self.assertEqual(len(changes),1);self.assertEqual(changes[0]['url'],RULE);self.assertEqual(changes[0]['kind'],'Datei geändert')
      self.assertNotEqual(rule['sha256'],first_rule['sha256']);self.assertEqual(rule['text_sha256'],first_rule['text_sha256'])
      self.assertEqual(rule['text'],'');self.assertEqual(first_rule['text'],'')
     self.assertEqual(await request_counts()-before,stage)
   finally:
    await context.close();await browser.close();tmp.cleanup()

 async def test_09_collection_metadata_replaces_initial_raw_capture_and_changes_stay_addressable(self):
  tmp=tempfile.TemporaryDirectory(dir=scratch.name)
  store.DATA=engine.DATA=Path(tmp.name);store.CONFIG=store.DATA/'settings.json';store.SECRETS=store.DATA/'credentials.json';store.init()
  store.save_json(store.CONFIG,{k:'synthetic-no-model' for k in ('agent_model','analysis_model','vision_model')})
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('p','Collection',SOURCE,'Read confirmed collection.','sections.test','0 6 * * *',0,store.now(),store.now()))
  project=store.one("SELECT * FROM projects WHERE id='p'");year=datetime.now(store.TZ).year;phase=1
  def card(slug,month,title):
   return '<article class="record" style="height:140px"><h2><a href="'+SOURCE+'norm/'+slug+'">'+title+'</a></h2><dl><dt>Ausgabedatum:</dt><dd>'+month+'</dd></dl><p>Confirmed visible record.</p></article>'
  def content():
   records=(card('new',str(year)+'-11','New standard') if phase==2 else '')+card('one',str(year)+'-10','First standard')+card('two',str(year)+'-09','Second standard')+card('old',str(year-1)+'-12','Older boundary')
   return '<!doctype html><html><head><meta charset="utf-8"><title>Raw portal title</title></head><body><nav><button type="button" onclick="document.querySelector(\'#list\').hidden=false">Normen</button></nav><main><h1>Raw start page heading</h1><div id="list" hidden style="height:300px;width:800px;overflow-y:auto">'+records+'</div></main></body></html>'
  async with async_playwright() as pw:
   browser=await pw.chromium.connect_over_cdp(engine.BROWSER_CDP_URL)
   context=await browser.new_context(viewport={'width':1200,'height':1000},service_workers='block')
   async def serve(route):
    if route.request.method!='GET':return await route.abort()
    return await route.fulfill(status=200,content_type='text/html; charset=utf-8',body=content())
   class ContextProxy:
    @property
    def pages(self):return context.pages
    async def new_page(self):
     page=await context.new_page();await page.route(SOURCE+'**',serve);return page
    def __getattr__(self,name):return getattr(context,name)
   async def close_proxy():pass
   browser_proxy=SimpleNamespace(contexts=[ContextProxy()],close=close_proxy)
   class EnginePlaywright:
    async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser_proxy)))
    async def __aexit__(self,*args):pass
   try:
    for phase in (1,2):
     rid='collection-stage-'+str(phase);store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,'p',store.now(),'running','test'))
     async def agent(task,initial,model,llm,dispatch,screen,log,**kwargs):
      await dispatch('activate_navigation',{'label':'Normen'})
      scan=await dispatch('scan_collection',{'filter_label':'Normen','item_selector':'.record','title_selector':'h2','date_label':'Ausgabedatum','container_selector':'#list'})
      self.assertNotIn('error',scan);self.assertEqual(scan['observed_count'],2 if phase==1 else 3)
      reread=await dispatch('read_archive',{'url':SOURCE})
      self.assertEqual(reread['title'],'Normen');self.assertTrue(reread['scope'].startswith('collection-v1:'))
      self.assertEqual(len(reread['current_entries']),2 if phase==1 else 3)
      self.assertEqual(reread['baseline'],phase==1)
      self.assertEqual(len(reread['comparison']['changes']),0 if phase==1 else 1)
      if phase==2:self.assertIn(SOURCE+'norm/new',[r['url'] for r in reread['new_entries']])
      protected=await dispatch('focus_content',{'selector':'main'})
      self.assertTrue(protected['collection_snapshot_preserved']);self.assertEqual(protected['title'],'Normen')
      return {'complete':True,'checked':['Confirmed collection read.'],'pending':[],'findings':'Synthetic collection metadata check.','actions':[]}
     with patch('engine.async_playwright',EnginePlaywright),patch('engine.run_agent',new=agent),patch('engine.create_summary',new=AsyncMock(return_value='Collection checked.')),patch('engine.analyze_visual_changes',new=AsyncMock(return_value=[])),patch('engine.llm',new=AsyncMock(side_effect=review_only_model)) as model:
      await asyncio.wait_for(engine.monitor(project,rid),timeout=90)
     run=store.one('SELECT * FROM runs WHERE id=?',(rid,));self.assertEqual(run['status'],'success',run['error'])
     evidence=json.loads((store.DATA/'artifacts'/rid/'evidence.json').read_text());pages=[r for r in evidence['observations'] if r['kind']=='page']
     assert_final_review(self,evidence,rid,model,0 if phase==1 else 1)
     self.assertEqual(len(pages),1);observation=pages[0]
     self.assertEqual(observation['metadata']['title'],'Normen');self.assertEqual(observation['metadata']['baseline'],phase==1)
     links=observation['metadata']['links'];self.assertEqual(len(links),2 if phase==1 else 3)
     self.assertEqual({r['url'] for r in links},{SOURCE+'norm/one',SOURCE+'norm/two'}|({SOURCE+'norm/new'} if phase==2 else set()))
     self.assertEqual(store.one('SELECT url FROM snapshots WHERE id=?',(observation['snapshot_id'],))['url'],SOURCE)
   finally:
    await context.close();await browser.close();tmp.cleanup()


 async def _aria_monitor_scenario(self,phases,preserve_closed):
  tmp=tempfile.TemporaryDirectory(dir=scratch.name)
  store.DATA=engine.DATA=Path(tmp.name);store.CONFIG=store.DATA/'settings.json';store.SECRETS=store.DATA/'credentials.json';store.init()
  store.save_json(store.CONFIG,{k:'synthetic-no-model' for k in ('agent_model','analysis_model','vision_model')})
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('p','ARIA sections',SOURCE,'Read both explicitly selected document panels.','sections.test,fixture','0 6 * * *',0,store.now(),store.now()))
  project=store.one("SELECT * FROM projects WHERE id='p'");phase=1;navigation=0;known_scopes={}
  def content(serial):
   def panel(group):
    label='Prüffrist '+('31' if group=='a' and phase==3 else '30' if group=='a' else '60')+' Tage.'
    links=''.join('<p><a href="'+FIXTURE+'/guideline.pdf?aria='+group+str(number)+'">Document '+group+str(number)+'</a></p>' for number in range(2))
    token=str(serial)+'-'+group
    return '<section id="scope-'+group+'"><h2>Context '+group+'</h2><div class="accordion"><h3><button type="button" id="heading-'+token+'" aria-controls="panel-'+token+'" aria-expanded="false" onclick="togglePanel(this)">Documents</button></h3><div id="panel-'+token+'" aria-labelledby="heading-'+token+'" aria-hidden="true" hidden><p>'+label+'</p>'+links+'</div></div></section>'
   script='''<script>window.headerClicks=0;function togglePanel(button){window.headerClicks++;const opening=button.getAttribute('aria-expanded')!=='true';for(const other of document.querySelectorAll('button[aria-controls]')){const p=document.getElementById(other.getAttribute('aria-controls'));other.setAttribute('aria-expanded','false');p.setAttribute('aria-hidden','true');p.hidden=true;}const p=document.getElementById(button.getAttribute('aria-controls'));button.setAttribute('aria-expanded',String(opening));p.setAttribute('aria-hidden',String(!opening));p.hidden=!opening;}</script>'''
   return '<!doctype html><html><head><meta charset="utf-8"><title>ARIA fixture</title></head><body><main>'+panel('a')+panel('b')+'</main>'+script+'</body></html>'
  async with async_playwright() as pw:
   browser=await pw.chromium.connect_over_cdp(engine.BROWSER_CDP_URL)
   context=await browser.new_context(viewport={'width':1200,'height':1000},service_workers='block')
   async def serve(route):
    nonlocal navigation
    if route.request.method!='GET':return await route.abort()
    navigation+=1
    return await route.fulfill(status=200,content_type='text/html; charset=utf-8',body=content(navigation))
   class ContextProxy:
    @property
    def pages(self):return context.pages
    async def new_page(self):
     page=await context.new_page();await page.route(SOURCE+'**',serve);return page
    def __getattr__(self,name):return getattr(context,name)
   async def close_proxy():pass
   browser_proxy=SimpleNamespace(contexts=[ContextProxy()],close=close_proxy)
   class EnginePlaywright:
    async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser_proxy)))
    async def __aexit__(self,*args):pass
   try:
    for phase in phases:
     rid='aria-stage-'+str(phase);store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,'p',store.now(),'running','test'))
     async def agent(task,initial,model,llm,dispatch,screen,log,**kwargs):
      sources={};selectors={}
      for group in (('b','a') if phase==2 else ('a','b')):
       result=await dispatch('expand_section',{'label':'Documents','container_selector':'#scope-'+group})
       self.assertEqual(result['disclosure_type'],'aria');self.assertEqual(result['source']['url'],SOURCE)
       self.assertTrue(result['source']['scope'].startswith('section-v1:'))
       self.assertEqual(result['source']['section_container_selector'],'#scope-'+group)
       sources[group]=result['source'];selectors[group]=result['selector']
       if group in known_scopes:self.assertEqual(sources[group]['scope'],known_scopes[group])
       known_scopes[group]=sources[group]['scope']
      self.assertNotEqual(sources['a']['scope'],sources['b']['scope'])
      page=context.pages[-1]
      self.assertEqual(await page.locator('button[aria-expanded="true"]').count(),1)
      archived_a=await dispatch('read_archive',{'url':SOURCE,'scope':sources['a']['scope']})
      self.assertIn('Document a1',archived_a['text']);self.assertNotIn('Document b1',archived_a['text'])
      if preserve_closed:
       before=store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='page' ORDER BY id",(rid,))
       await page.reload(wait_until='domcontentloaded')
       reloaded=await dispatch('read_sources',{'urls':[SOURCE]})
       self.assertTrue(reloaded['sources'][0]['section_snapshot_preserved'])
       for selector in selectors.values():self.assertEqual(await page.locator(selector).count(),0)
       self.assertEqual(await page.locator('button[aria-expanded="true"]').count(),0)
       self.assertEqual(await page.evaluate('window.headerClicks'),0)
       raw=await dispatch('focus_content',{'selector':'main'})
       self.assertTrue(raw['section_snapshot_preserved'])
       scrolled=await dispatch('scroll_view',{'direction':'down'})
       self.assertTrue(scrolled['source']['section_snapshot_preserved'])
       self.assertEqual(await page.evaluate('window.headerClicks'),0)
       self.assertEqual(store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='page' ORDER BY id",(rid,)),before)
       archived_b=await dispatch('read_archive',{'url':SOURCE,'scope':sources['b']['scope']})
       self.assertIn('Document b1',archived_b['text'])
       inventory=await dispatch('check_documents',{})
       self.assertEqual(inventory['found'],4);self.assertEqual(inventory['checked'],4)
       self.assertEqual(inventory['failed'],[]);self.assertEqual(inventory['unverified'],[])
       sibling=next(s for s in before if engine.snapshot_scope(s)==sources['b']['scope'])
       reopened=await dispatch('expand_section',{'label':'Documents','container_selector':'#scope-a'})
       self.assertEqual(reopened['source']['scope'],sources['a']['scope'])
       self.assertEqual(store.one('SELECT * FROM snapshots WHERE id=?',(sibling['id'],)),sibling)
      inventory=await dispatch('check_documents',{})
      self.assertEqual(inventory['found'],4);self.assertEqual(inventory['checked'],4)
      self.assertEqual(inventory['failed'],[]);self.assertEqual(inventory['unverified'],[])
      return {'complete':True,'checked':['Both ARIA sections and four documents checked.'],'pending':[],'findings':'Synthetic ARIA integration.','actions':[]}
     with patch('engine.async_playwright',EnginePlaywright),patch('engine.run_agent',new=agent),patch('engine.create_summary',new=AsyncMock(return_value='ARIA sections checked.')),patch('engine.analyze_visual_changes',new=AsyncMock(return_value=[])),patch('engine.llm',new=AsyncMock(side_effect=review_only_model)) as model:
      await asyncio.wait_for(engine.monitor(project,rid),timeout=150)
     run=store.one('SELECT * FROM runs WHERE id=?',(rid,));self.assertEqual(run['status'],'success',run['error'])
     evidence=json.loads((store.DATA/'artifacts'/rid/'evidence.json').read_text())
     assert_final_review(self,evidence,rid,model,0 if phase<3 else 1)
     snapshots=store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='page'",(rid,))
     self.assertEqual(len(snapshots),2);self.assertEqual({engine.snapshot_scope(s) for s in snapshots},set(known_scopes.values()))
     self.assertEqual({s['url'] for s in snapshots},{SOURCE})
     changes=store.rows('SELECT * FROM changes WHERE run_id=?',(rid,))
     if phase<3:self.assertEqual(changes,[])
     else:
      self.assertEqual(len(changes),1)
      changed=store.one('SELECT * FROM snapshots WHERE id=?',(changes[0]['current_id'],))
      self.assertEqual(engine.snapshot_scope(changed),known_scopes['a'])
      self.assertIn('Inhalt geändert',changes[0]['kind'])
      diff=json.loads(changes[0]['evidence'])['text_diff']
      self.assertIn('-Prüffrist 30 Tage.',diff);self.assertIn('+Prüffrist 31 Tage.',diff)
   finally:
    await context.close();await browser.close();tmp.cleanup()

 async def test_10_aria_reload_raw_and_scroll_preserve_closed_sections_without_opening(self):
  await self._aria_monitor_scenario((1,),True)

 async def test_11_aria_dynamic_ids_reverse_order_and_one_digit_delta_keep_other_scope_stable(self):
  await self._aria_monitor_scenario((1,2,3),False)

 async def _mixed_monitor_scenario(self,kind,direct_heading=False):
  tmp=tempfile.TemporaryDirectory(dir=scratch.name)
  store.DATA=engine.DATA=Path(tmp.name);store.CONFIG=store.DATA/'settings.json';store.SECRETS=store.DATA/'credentials.json';store.init()
  store.save_json(store.CONFIG,{k:'synthetic-no-model' for k in ('agent_model','analysis_model','vision_model')})
  store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',('p','Mixed '+kind,SOURCE,'Read direct material and both selected panels.','sections.test,fixture','0 6 * * *',0,store.now(),store.now()))
  project=store.one("SELECT * FROM projects WHERE id='p'");phase=1;serial=0;scopes={};base_scope=None
  def content():
   direct='<p>Direct deadline '+('31' if phase==3 else '30')+' days.</p><p><a href="'+RULE+'">Direct rule</a></p>'
   if direct_heading:direct='<h3><a href="'+RULE+'">Direct deadline '+('31' if phase==3 else '30')+' days.</a></h3>'
   def panel(group):
    body='<p>Panel '+group+' text 10.</p><a href="'+FIXTURE+'/guideline.pdf?mixed='+group+'">Panel document '+group+'</a>'
    opening=phase==2 and group=='a'
    if kind=='native':return '<details name="exclusive" id="'+group+'"'+(' open' if opening else '')+'><summary>Panel '+group+'</summary>'+body+'</details>'
    if kind=='legacy':return '<section id="scope-'+group+'"><div><h3><button type="button" id="duplicate-trigger" aria-controls="missing-panel" aria-expanded="'+str(opening).lower()+'" onclick="toggle(this)">Panel '+group+'</button></h3></div><div'+('' if opening else ' hidden')+'>'+body+'</div></section>'
    token=str(serial)+'-'+group
    return '<section id="scope-'+group+'"><h3><button type="button" id="trigger-'+token+'" aria-controls="panel-'+token+'" aria-expanded="'+str(opening).lower()+'" onclick="toggle(this)">Panel '+group+'</button></h3><div id="panel-'+token+'" aria-labelledby="trigger-'+token+'"'+('' if opening else ' hidden')+'>'+body+'</div></section>'
   script='''<script>function toggle(b){const opening=b.getAttribute('aria-expanded')!=='true';for(const e of document.querySelectorAll('button[aria-controls]')){e.setAttribute('aria-expanded','false');document.getElementById(e.getAttribute('aria-controls')).hidden=true;}b.setAttribute('aria-expanded',String(opening));document.getElementById(b.getAttribute('aria-controls')).hidden=!opening;}</script>'''
   if kind=='legacy':script='''<script>function toggle(b){const opening=b.getAttribute('aria-expanded')!=='true';for(const e of document.querySelectorAll('button[aria-controls]')){e.setAttribute('aria-expanded','false');e.closest('section').children[1].hidden=true;}b.setAttribute('aria-expanded',String(opening));b.closest('section').children[1].hidden=!opening;}</script>'''
   return '<!doctype html><html><head><meta charset="utf-8"><title>Mixed source</title></head><body><main><h1>Mixed content</h1>'+direct+panel('a')+panel('b')+'<details><summary>Unselected</summary><a href="'+FIXTURE+'/unselected.pdf">Unselected hidden file</a></details></main>'+script+'</body></html>'
  async with async_playwright() as pw:
   browser=await pw.chromium.connect_over_cdp(engine.BROWSER_CDP_URL)
   context=await browser.new_context(viewport={'width':1200,'height':1000},service_workers='block')
   async def serve(route):
    nonlocal serial
    serial+=1
    return await route.fulfill(status=200,content_type='text/html; charset=utf-8',body=content())
   class ContextProxy:
    @property
    def pages(self):return context.pages
    async def new_page(self):
     page=await context.new_page();await page.route(SOURCE+'**',serve);return page
    def __getattr__(self,name):return getattr(context,name)
   async def close_proxy():pass
   proxy=SimpleNamespace(contexts=[ContextProxy()],close=close_proxy)
   class EnginePlaywright:
    async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=proxy)))
    async def __aexit__(self,*args):pass
   try:
    for phase in (1,2,3):
     await context.add_cookies([{'name':COOKIE,'value':'2' if phase==3 else '1','domain':'fixture','path':'/inventory-test','httpOnly':False,'secure':False,'sameSite':'Lax'}])
     rid=kind+'-mixed-'+str(phase);store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',(rid,'p',store.now(),'running','test'))
     async def agent(task,initial,model,llm,dispatch,screen,log,**kwargs):
      nonlocal base_scope
      await dispatch('focus_content',{'selector':'main'})
      for group in (('b','a') if phase==2 else ('a','b')):
       result=await dispatch('expand_section',{'label':'Panel '+group})
       if group in scopes:self.assertEqual(scopes[group],result['source']['scope'])
       scopes[group]=result['source']['scope']
      pages=store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='page'",(rid,))
      bases=[s for s in pages if (engine.snapshot_scope(s) or '').startswith('page-base-v1:')]
      self.assertEqual(len(pages),3);self.assertEqual(len(bases),1)
      saved=bases[0];scope=engine.snapshot_scope(saved)
      if base_scope:self.assertEqual(scope,base_scope)
      base_scope=scope
      self.assertIn('Direct deadline',saved['text']);self.assertNotIn('Panel a',saved['text']);self.assertNotIn('Panel b',saved['text'])
      self.assertNotIn('Unselected',saved['text']);self.assertIsNone(saved['screenshot'])
      self.assertEqual(json.loads(saved['links']),[RULE])
      read=await dispatch('read_archive',{'url':SOURCE,'scope':scope})
      self.assertIn('Direct deadline',read['text'])
      inventory=await dispatch('check_documents',{})
      self.assertEqual(inventory['found'],3);self.assertEqual(inventory['checked'],3)
      self.assertEqual(inventory['failed'],[]);self.assertEqual(inventory['unverified'],[])
      return {'complete':True,'checked':['Direct area, both sections and three real PDFs checked.'],'pending':[],'findings':'Mixed source checked.','actions':[]}
     with patch('engine.async_playwright',EnginePlaywright),patch('engine.run_agent',new=agent),patch('engine.create_summary',new=AsyncMock(return_value='Mixed source checked.')),patch('engine.analyze_visual_changes',new=AsyncMock(return_value=[])),patch('engine.llm',new=AsyncMock(side_effect=review_only_model)) as model:
      await asyncio.wait_for(engine.monitor(project,rid),timeout=150)
     run=store.one('SELECT * FROM runs WHERE id=?',(rid,));self.assertEqual(run['status'],'success',run['error'])
     changes=store.rows('SELECT * FROM changes WHERE run_id=?',(rid,))
     if phase<3:self.assertEqual(changes,[])
     else:
      self.assertEqual(len(changes),2)
      self.assertEqual({c['url'] for c in changes},{SOURCE,RULE})
      changed=next(c for c in changes if c['url']==SOURCE)
      self.assertEqual(engine.snapshot_scope(store.one('SELECT * FROM snapshots WHERE id=?',(changed['current_id'],))),base_scope)
      difference=json.loads(changed['evidence'])['text_diff']
      self.assertIn('-Direct deadline 30 days.',difference);self.assertIn('+Direct deadline 31 days.',difference)
     evidence=json.loads((store.DATA/'artifacts'/rid/'evidence.json').read_text())
     assert_final_review(self,evidence,rid,model,0 if phase<3 else 1)
     self.assertEqual(evidence['document_check']['found'],3)
     self.assertEqual(len([o for o in evidence['observations'] if o['kind']=='page']),3)
   finally:
    await context.close();await browser.close();tmp.cleanup()

 async def test_12_native_mixed_direct_region_and_panels_preserve_both_comparisons(self):
  await self._mixed_monitor_scenario('native')

 async def test_13_aria_mixed_dynamic_ids_default_open_and_reverse_order_are_stable(self):
  await self._mixed_monitor_scenario('aria')

 async def test_14_legacy_mixed_dangling_controls_duplicate_ids_and_default_open_are_stable(self):
  await self._mixed_monitor_scenario('legacy')

 async def test_15_direct_document_heading_alone_remains_in_base_inventory(self):
  await self._mixed_monitor_scenario('native',direct_heading=True)


if __name__=='__main__':unittest.main(verbosity=2)
