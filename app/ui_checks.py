"""Compact UI and additive metadata migration checks in a separate database."""
import os,tempfile,sqlite3,json,unittest
from datetime import timedelta
from pathlib import Path
scratch=tempfile.TemporaryDirectory(prefix='.compact-ui-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
with sqlite3.connect(Path(scratch.name)/'monitor.db') as c:
 c.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,prompt TEXT NOT NULL,domains TEXT NOT NULL,cron TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,next_due TEXT NOT NULL,last_success TEXT,created TEXT NOT NULL)")
 c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)',('legacy','Legacy Alias','https://example.org','Original task','example.org','0 6 * * *',0,'2027-01-01T00:00:00+00:00',None,'2026-01-01'))
from store import *
from project_meta import *
import tag_registry
init()
from web import app
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

class UI(unittest.TestCase):
 def setUp(self):
  for name in ('Arbeit','Privat'):
   if not any(t['name']==name for t in tag_registry.all_tags()):tag_registry.save(name,'#1967d2')
  self.client=TestClient(app)
  self.client.get('/')
  self.csrf=self.client.cookies.get('kiwm_csrf')
 def form(self,**extra):
  data={'csrf':self.csrf,'id':'legacy','name':'Legacy Alias','url':'https://example.org','prompt':'Original task','domains':'example.org','cron':'0 6 * * *'}
  data.update(extra);return data
 def test_01_existing_data_and_idempotent_migration(self):
  row=one("SELECT * FROM projects WHERE id='legacy'")
  self.assertEqual(row['name'],'Legacy Alias');self.assertEqual(row['prompt'],'Original task')
  self.assertEqual(row['color'],DEFAULT_COLOR);self.assertEqual(json.loads(row['tags']),[])
  execute("UPDATE projects SET color=?,tags=? WHERE id='legacy'",('#7c3aed',json.dumps(['Arbeit','Privat'])))
  init();row=one("SELECT * FROM projects WHERE id='legacy'")
  self.assertEqual(row['color'],'#7c3aed');self.assertEqual(json.loads(row['tags']),['Arbeit','Privat'])
 def test_02_color_tags_save_and_legacy_request(self):
  tag_registry.save('Projekte Berlin','#7c3aed')
  response=self.client.post('/projects/save',data=self.form(color='#14B8A6',tags=' Arbeit, Privat, arbeit ;  Projekte  Berlin '))
  self.assertEqual(response.status_code,200)
  row=one("SELECT * FROM projects WHERE id='legacy'")
  self.assertEqual(row['color'],'#14b8a6');self.assertEqual(json.loads(row['tags']),['Arbeit','Privat','Projekte Berlin'])
  self.client.post('/projects/save',data=self.form())
  row=one("SELECT * FROM projects WHERE id='legacy'")
  self.assertEqual(row['color'],'#14b8a6');self.assertEqual(len(json.loads(row['tags'])),3)
  page=BeautifulSoup(self.client.get('/projects/legacy/edit').text,'html.parser')
  self.assertEqual(page.select_one('input[type=color]')['value'],'#14b8a6')
  self.assertIsNone(page.select_one('[name=tags]'))
  self.assertEqual(len(page.select('[name=tag_ids]:checked')),3)
 def test_03_injection_limits_and_csrf(self):
  self.assertEqual(self.client.post('/projects/save',data=self.form(color='red;background:url(https://evil)')).status_code,400)
  self.assertEqual(self.client.post('/projects/save',data=self.form(tags='x'*41)).status_code,400)
  self.assertEqual(self.client.post('/projects/save',data=self.form(tags=','.join(str(n) for n in range(13)))).status_code,400)
  self.assertEqual(self.client.post('/projects/save',data=self.form(csrf='bad',color='#ffffff')).status_code,403)
  tag='<img src=x onerror=alert(1)>'
  tag_registry.save(tag,'#7c3aed')
  self.assertEqual(self.client.post('/projects/save',data=self.form(color='#7c3aed',tags=tag)).status_code,200)
  home=BeautifulSoup(self.client.get('/').text,'html.parser')
  self.assertIsNone(home.select_one('.project-card img'))
  self.assertEqual(home.select_one('.tag').get_text(),tag)
  self.assertEqual(json.loads(home.select_one('[data-project]')['data-tags']),[tag.casefold()])
 def test_04_compact_cards_filter_metadata_and_color(self):
  self.client.post('/projects/save',data=self.form(color='#0891b2',tags='Arbeit, Privat',enabled='on'))
  execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',('ui-old','legacy',now(),now(),'success','test'))
  execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',('ui-new','legacy',now(),now(),'success','test'))
  from engine import archive
  archive({'id':'legacy'},'ui-old','https://example.org','page',b'10','Deadline 10')
  archive({'id':'legacy'},'ui-new','https://example.org','page',b'11','Deadline 11')
  doc=BeautifulSoup(self.client.get('/').text,'html.parser');card=doc.select_one('[data-project]')
  self.assertEqual(card['style'],'--project-color:#0891b2')
  self.assertEqual(card.h2.get_text(),'Legacy Alias');self.assertEqual(card['data-attention'],'1')
  self.assertEqual(len(doc.select('.filter-bar [data-filter]')),6);self.assertEqual(len(doc.select('.filter-bar .icon')),6)
  self.assertEqual(doc.select_one('[data-filter="attention"].has-changes b').get_text(),'1')
  self.assertIsNone(doc.select_one('.metrics'));self.assertIsNone(doc.select_one('.alert-red'))
  self.assertIn('1',card.select_one('.change-pill').get_text())
  self.assertEqual([o['value'] for o in doc.select('[data-tag-filter] option')],['']+[t['key'] for t in tag_registry.all_tags()])
  execute("UPDATE runs SET status='failed',started='2099-01-01' WHERE id='ui-new'")
  doc=BeautifulSoup(self.client.get('/').text,'html.parser');card=doc.select_one('[data-project]')
  self.assertEqual(card['style'],'--project-color:#0891b2');self.assertIsNotNone(card.select_one('.badge.failed'))

 def history_project(self,pid):
  execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
             VALUES(?,?,?,?,?,?,?,?,?)''',
          (pid,'Historie '+pid,'https://example.org','Pruefe fachliche Inhalte.',
           'example.org','0 6 * * *',0,next_due('0 6 * * *'),now()))
  return pid

 def history_run(self,pid,rid,status='success',finished=True,index=0):
  stamp=(datetime.now(timezone.utc)-timedelta(minutes=30)+timedelta(seconds=index)).isoformat()
  execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
          (rid,pid,stamp,stamp if finished else None,status,'history-ui-test'))

 def history_change(self,pid,rid,kind,previous='previous',relevance=None):
  evidence={} if relevance is None else {'ai_assessment':{'relevance':relevance}}
  change_id=uid()
  execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
          (change_id,pid,rid,'https://example.org',kind,now(),previous,uid(),
           json.dumps(evidence),digest(change_id)))
  return change_id

 def history_links(self,pid):
  response=self.client.get('/projects/'+pid)
  self.assertEqual(response.status_code,200)
  page=BeautifulSoup(response.text,'html.parser')
  history=next(block for block in page.select('details.card')
               if block.find('summary',recursive=False) is not None
               and block.find('summary',recursive=False).get_text(strip=True).startswith('Verlauf ('))
  self.assertTrue(history.has_attr('open'))
  self.assertEqual([th.get_text(strip=True) for th in history.select('th')],
                   ['Prüfung · Europe/Berlin','Status','Änderungen'])
  links={link['href'].removeprefix('/runs/'):link
         for link in history.select('a[href^="/runs/"]')}
  for link in links.values():
   self.assertTrue(link.get('aria-label','').startswith(link.get_text(strip=True)+' · '))
  return links

 def test_dashboard_prioritizes_running_checks_and_exposes_confirmed_change_dates(self):
  recent=self.history_project('sort-recent')
  older=self.history_project('sort-older')
  error=self.history_project('sort-error')
  pending=self.history_project('sort-pending')
  self.addCleanup(execute,"DELETE FROM projects WHERE id LIKE 'sort-%'")
  self.addCleanup(execute,"DELETE FROM runs WHERE project_id LIKE 'sort-%'")
  self.addCleanup(execute,"DELETE FROM changes WHERE project_id LIKE 'sort-%'")
  self.history_run(recent,'sort-recent-ok',index=20)
  self.history_run(older,'sort-older-ok',index=10)
  self.history_run(older,'sort-older-running','running',False,index=30)
  self.history_run(error,'sort-error-fail','failed')
  self.history_run(pending,'sort-pending-cancelled','cancelled',index=40)
  self.history_run(pending,'sort-pending-queued','queued',False,index=50)
  confirmed=self.history_change(older,'sort-older-ok','Inhalt geändert',relevance='relevant')
  detected=one('SELECT detected FROM changes WHERE id=?',(confirmed,))['detected']
  execute('INSERT INTO change_reviews VALUES(?,?)',(confirmed,now()))
  self.history_change(older,'sort-older-ok','Inhalt geändert',relevance='uncertain')
  self.history_change(older,'sort-older-ok','Neues Dokument',previous=None)
  self.history_change(recent,'sort-recent-ok','Inhalt geändert',relevance='irrelevant')
  page=BeautifulSoup(self.client.get('/').text,'html.parser')
  cards=page.select('[data-project-id^="sort-"]')
  self.assertEqual([card['data-project-id'] for card in cards],[older,error,recent,pending])
  self.assertEqual([card['data-running'] for card in cards],['1','0','0','0'])
  self.assertEqual(cards[0]['data-last-change'],detected)
  self.assertEqual(cards[3]['data-last-checked'],'')
  data={p['id']:p for p in self.client.get('/api/project-status').json()['projects']}
  self.assertEqual(data[older]['last_checked'],cards[0]['data-last-checked'])
  self.assertEqual(data[older]['last_change'],detected)
  self.assertIsNone(data[recent]['last_change'])
  self.assertIsNone(data[pending]['last_checked'])

 def test_05_history_results_remain_after_changes_are_acknowledged(self):
  pid=self.history_project('history-reviewed')
  from engine import archive
  for index,(rid,text) in enumerate([('history-baseline','Frist 30 Tage'),
                                     ('history-unchanged','Frist 30 Tage'),
                                     ('history-changed','Frist 45 Tage')]):
   self.history_run(pid,rid,index=index)
   archive({'id':pid},rid,'https://example.org','page',text.encode(),text)
  links=self.history_links(pid)
  self.assertEqual(links['history-unchanged'].get_text(strip=True),'Keine Änderung erkannt')
  self.assertEqual(links['history-changed'].get_text(strip=True),'Änderungen erkannt')
  import attention
  self.assertEqual(attention.summary(pid)['attention_count'],1)
  attention.acknowledge(pid,now(),now())
  self.assertEqual(attention.summary(pid)['attention_count'],0)
  self.assertEqual(self.history_links(pid)['history-changed'].get_text(strip=True),'Änderungen erkannt')

 def test_06_history_ignores_baseline_technical_and_irrelevant_changes(self):
  pid=self.history_project('history-filtered')
  scenarios=[('initial','Neue Seite',None,None),
             ('technical','Datei technisch geändert','previous',None),
             ('visual','Visuelle Abweichung','previous',None),
             ('irrelevant','Inhalt geändert','previous','irrelevant'),
             ('relevant','Inhalt geändert','previous','uncertain')]
  for index,(name,kind,previous,relevance) in enumerate(scenarios):
   rid='history-filter-'+name
   self.history_run(pid,rid,index=index)
   self.history_change(pid,rid,kind,previous,relevance)
  links=self.history_links(pid)
  for name in ('initial','technical','visual','irrelevant'):
   self.assertEqual(links['history-filter-'+name].get_text(strip=True),'Keine Änderung erkannt')
  self.assertEqual(links['history-filter-relevant'].get_text(strip=True),'Änderung noch unklar')

 def test_07_history_never_claims_no_change_for_unfinished_or_failed_checks(self):
  pid=self.history_project('history-incomplete')
  scenarios=[('failed','failed',True,True,'Prüfung fehlgeschlagen'),
             ('partial','partial',True,False,'Prüfung unvollständig'),
             ('running','running',False,False,'Prüfung läuft'),
             ('queued','queued',False,False,'Prüfung ausstehend'),
             ('partial-changed','partial',True,True,'Änderungen erkannt')]
  for index,(name,status,finished,changed,label) in enumerate(scenarios):
   rid='history-state-'+name
   self.history_run(pid,rid,status,finished,index)
   if changed:self.history_change(pid,rid,'Inhalt geändert')
  links=self.history_links(pid)
  for name,status,finished,changed,label in scenarios:
   link=links['history-state-'+name]
   self.assertEqual(link.get_text(strip=True),label)
   self.assertNotEqual(link.get_text(strip=True),'Keine Änderung erkannt')

 def test_08_binary_file_change_directly_reaches_dashboard_and_history(self):
  import attention
  from engine import archive
  pid=self.history_project('history-file-version')
  url='https://example.org/guideline.pdf'
  for index,(rid,raw) in enumerate([('file-version-before',b'%PDF-original-one'),('file-version-after',b'%PDF-original-two')]):
   self.history_run(pid,rid,index=index)
   archive({'id':pid},rid,url,'document',raw,'',suffix='.pdf')
  change=one('SELECT * FROM changes WHERE run_id=?',('file-version-after',))
  self.assertEqual(change['kind'],'Datei geändert')
  before=one('SELECT * FROM snapshots WHERE id=?',(change['previous_id'],))
  after=one('SELECT * FROM snapshots WHERE id=?',(change['current_id'],))
  self.assertNotEqual(before['sha256'],after['sha256'])
  self.assertEqual(before['text_sha256'],after['text_sha256'])
  self.assertEqual(before['text_sha256'],digest(''))
  self.assertEqual((before['text'],after['text']),('',''))
  pending=attention.summary(pid)
  self.assertEqual((pending['attention_count'],pending['notice_count']),(1,0))
  home=BeautifulSoup(self.client.get('/').text,'html.parser')
  card=home.select_one('[data-project-id="'+pid+'"]')
  self.assertEqual(card['data-attention'],'1')
  chip=card.select_one('.change-pill')
  self.assertEqual(chip.get_text(strip=True),'1 Änderung erkannt')
  self.assertEqual(chip['href'],f'/projects/{pid}#changes')
  self.assertEqual(self.history_links(pid)['file-version-after'].get_text(strip=True),'Änderungen erkannt')
  attention.acknowledge(pid,pending['review_through'],now())
  self.assertEqual(attention.summary(pid)['attention_count'],0)
  card=BeautifulSoup(self.client.get('/').text,'html.parser').select_one('[data-project-id="'+pid+'"]')
  self.assertIsNone(card.select_one('.change-pill'))
  self.assertEqual(self.history_links(pid)['file-version-after'].get_text(strip=True),'Änderungen erkannt')

 def test_09_technical_uncertainty_and_visual_relevance_do_not_escalate(self):
  import attention
  pid=self.history_project('history-notice-priority')
  scenarios=[('unreviewed','Datei technisch geändert',None),
             ('uncertain','Datei technisch geändert','uncertain'),
             ('irrelevant','Datei technisch geändert','irrelevant'),
             ('visual','Visuelle Abweichung','relevant'),
             ('unknown','Unbekannter Hinweis','relevant')]
  for index,(name,kind,relevance) in enumerate(scenarios):
   rid='notice-priority-'+name
   self.history_run(pid,rid,index=index)
   self.history_change(pid,rid,kind,relevance=relevance)
  state=attention.summary(pid)
  self.assertEqual((state['attention_count'],state['notice_count']),(0,4))
  self.assertNotIn('notice-priority-irrelevant',[c['run_id'] for c in state['pending_changes']])
  card=BeautifulSoup(self.client.get('/').text,'html.parser').select_one('[data-project-id="'+pid+'"]')
  self.assertEqual(card['data-attention'],'0')
  for run_id,link in self.history_links(pid).items():
   self.assertEqual(link.get_text(strip=True),'Änderung noch unklar' if run_id.endswith('-uncertain') else 'Keine Änderung erkannt')
  self.assertEqual(attention.priority('Datei technisch geändert'),'notice')
  self.assertEqual(attention.priority('Datei technisch geändert',{'ai_assessment':{'relevance':'relevant '}}),'notice')
  self.assertEqual(attention.priority('Inhalt geändert'),'attention')

 def test_10_uncertain_text_change_is_a_hint_not_a_confirmed_change(self):
  import attention
  pid=self.history_project('uncertain-subject-change')
  rid='uncertain-subject-run'
  self.history_run(pid,rid)
  self.history_change(pid,rid,'Inhalt geändert; Links geändert','previous','uncertain')
  state=attention.summary(pid)
  self.assertEqual((state['attention_count'],state['notice_count']),(0,1))
  card=BeautifulSoup(self.client.get('/').text,'html.parser').select_one('[data-project-id="'+pid+'"]')
  self.assertEqual(card['data-attention'],'0')
  self.assertEqual(self.history_links(pid)[rid].get_text(strip=True),'Änderung noch unklar')
  self.assertEqual(attention.priority('Inhalt geändert',{'ai_assessment':{'relevance':'relevant'}}),'attention')

 def reasoned_change(self,pid,rid,reason,kind='Inhalt geändert',relevance='relevant',previous='previous'):
  cid=self.history_change(pid,rid,kind,previous,relevance)
  evidence={'ai_assessment':{'relevance':relevance,'reason':reason}}
  if 'Inhalt geändert' in kind:evidence['text_diff']='--- vorher\n+++ aktuell\n-Kategorie A\n+Kategorie B'
  execute('UPDATE changes SET evidence=? WHERE id=?',(json.dumps(evidence),cid))
  return cid

 def test_11_old_open_finding_precedes_latest_unchanged_report_and_is_not_duplicated(self):
  pid=self.history_project('prominent-old-finding')
  old,new='prominent-old-run','prominent-new-run'
  self.history_run(pid,old,index=0);self.history_run(pid,new,index=1)
  reason='Die bestehende Richtlinie wurde von Kategorie A in Kategorie B verschoben.'
  cid=self.reasoned_change(pid,old,reason)
  notice='Die Bedeutung des technischen Dateiunterschieds ist noch unklar.'
  self.reasoned_change(pid,new,notice,'Datei technisch geändert','uncertain')
  execute('UPDATE runs SET summary=? WHERE id=?',('Keine neue Änderung in der letzten Prüfung.',new))
  page=BeautifulSoup(self.client.get('/projects/'+pid).text,'html.parser')
  block=page.select_one('[data-relevant-findings]')
  self.assertIsNotNone(block);self.assertEqual(block.name,'section')
  self.assertTrue(block.h2.get_text(strip=True).startswith('Offene Änderungen'))
  finding=block.select_one('[data-finding-id="'+cid+'"]')
  self.assertIn(reason,finding.get_text())
  self.assertIsNone(finding.find_parent('details'))
  self.assertEqual(finding.select_one('time')['datetime'],one('SELECT finished FROM runs WHERE id=?',(old,))['finished'])
  self.assertIsNotNone(finding.select_one('a[href="/runs/'+old+'"]'))
  self.assertIsNotNone(finding.select_one('a[href="https://example.org"]'))
  latest=page.select_one('[data-latest-summary]')
  self.assertEqual(latest.h2.get_text(strip=True),'Letzte Prüfung')
  self.assertIn('Keine neue Änderung',latest.get_text())
  self.assertEqual(page.select('[data-relevant-findings],[data-latest-summary]'),[block,latest])
  details=page.select_one('[data-change-notices]')
  self.assertFalse(details.has_attr('open'))
  self.assertIn(notice,details.get_text());self.assertNotIn(reason,details.get_text())
  self.assertEqual(len(page.select('form[action="/projects/'+pid+'/review"]')),1)

 def test_12_notice_only_and_irrelevant_findings_are_not_promoted(self):
  pid=self.history_project('prominent-notices-only');rid='prominent-notice-run'
  self.history_run(pid,rid)
  notice='Ein technischer Unterschied benötigt noch eine fachliche Einordnung.'
  self.reasoned_change(pid,rid,notice,'Datei technisch geändert','uncertain')
  self.reasoned_change(pid,rid,'IRRELEVANT-PROMINENCE-CANARY','Inhalt geändert','irrelevant')
  self.reasoned_change(pid,rid,'BASELINE-PROMINENCE-CANARY','Neue Seite','relevant',None)
  for route in ('/projects/'+pid,'/runs/'+rid):
   page=BeautifulSoup(self.client.get(route).text,'html.parser')
   self.assertIsNone(page.select_one('[data-relevant-findings]'))
  page=BeautifulSoup(self.client.get('/projects/'+pid).text,'html.parser')
  details=page.select_one('[data-change-notices]')
  self.assertIsNotNone(details);self.assertFalse(details.has_attr('open'))
  self.assertIn(notice,details.get_text())
  self.assertNotIn('IRRELEVANT-PROMINENCE-CANARY',page.get_text())
  self.assertNotIn('BASELINE-PROMINENCE-CANARY',page.get_text())

 def test_13_run_findings_remain_visible_after_acknowledgement_and_diff_stays_closed(self):
  import attention
  pid=self.history_project('prominent-run-history');rid='prominent-archived-run'
  self.history_run(pid,rid)
  reason='Der fachliche Eintrag wurde einer anderen Kategorie zugeordnet.'
  cid=self.reasoned_change(pid,rid,reason)
  self.reasoned_change(pid,rid,'Ungeklärter technischer Nebenbefund.','Datei technisch geändert','uncertain')
  attention.acknowledge(pid,attention.summary(pid)['review_through'],now())
  project_page=BeautifulSoup(self.client.get('/projects/'+pid).text,'html.parser')
  self.assertIsNone(project_page.select_one('[data-relevant-findings]'))
  page=BeautifulSoup(self.client.get('/runs/'+rid).text,'html.parser')
  block=page.select_one('[data-relevant-findings]')
  self.assertTrue(block.h2.get_text(strip=True).startswith('Änderungen dieser Prüfung'))
  self.assertEqual([r['data-finding-id'] for r in block.select('[data-finding-id]')],[cid])
  self.assertIn(reason,block.get_text());self.assertNotIn('Nebenbefund',block.get_text())
  for text in ('Technische Unterschiede','Technischer Text- und Linkvergleich'):
   matching=[s for s in page.find_all('summary') if s.get_text(strip=True).startswith(text)]
   self.assertTrue(matching)
   self.assertTrue(all(not s.parent.has_attr('open') for s in matching))

 def test_14_model_reason_is_plain_escaped_text_and_unsafe_source_is_not_linked(self):
  pid=self.history_project('prominent-escaped');rid='prominent-escaped-run'
  self.history_run(pid,rid)
  reason='<img src=x onerror="alert(1)"><script>alert(2)</script> Kategorie A → B & weitere Angaben.'
  cid=self.reasoned_change(pid,rid,reason)
  execute('UPDATE changes SET url=? WHERE id=?',('javascript:alert(3)',cid))
  for route in ('/projects/'+pid,'/runs/'+rid):
   page=BeautifulSoup(self.client.get(route).text,'html.parser')
   block=page.select_one('[data-relevant-findings]')
   self.assertIn(reason,block.get_text())
   self.assertEqual(block.select('img,script,[onerror],a[href^="javascript:"]'),[])
   self.assertIsNotNone(block.select_one('a[href="/runs/'+rid+'"]'))

 def test_15_more_than_fifty_relevant_findings_are_all_visible(self):
  pid=self.history_project('prominent-complete-list');rid='prominent-many-run'
  self.history_run(pid,rid)
  identifiers=[self.reasoned_change(pid,rid,'Belegte fachliche Änderung Nummer '+str(index)+'.') for index in range(51)]
  for route in ('/projects/'+pid,'/runs/'+rid):
   page=BeautifulSoup(self.client.get(route).text,'html.parser')
   block=page.select_one('[data-relevant-findings]')
   self.assertEqual({r['data-finding-id'] for r in block.select('[data-finding-id]')},set(identifiers))
   self.assertIn('Änderung Nummer 50.',block.get_text())
   self.assertTrue(all(r.find_parent('details') is None for r in block.select('[data-finding-id]')))

 def test_16_visible_acknowledgement_cutoff_keeps_a_later_finding_open(self):
  import attention
  pid=self.history_project('prominent-review-cutoff');old,new='prominent-cutoff-old','prominent-cutoff-new'
  self.history_run(pid,old,index=0)
  old_id=self.reasoned_change(pid,old,'Eine frühere fachliche Änderung ist noch nicht bestätigt.')
  page=BeautifulSoup(self.client.get('/projects/'+pid).text,'html.parser')
  form=page.select_one('[data-relevant-findings] form[action="/projects/'+pid+'/review"]')
  through=form.select_one('input[name=through]')['value']
  self.history_run(pid,new,index=1)
  new_id=self.reasoned_change(pid,new,'Eine später abgeschlossene Prüfung enthält eine weitere fachliche Änderung.')
  result=self.client.post('/projects/'+pid+'/review',data={'csrf':self.csrf,'through':through})
  self.assertEqual(result.status_code,200)
  self.assertIsNotNone(one('SELECT * FROM change_reviews WHERE change_id=?',(old_id,)))
  self.assertIsNone(one('SELECT * FROM change_reviews WHERE change_id=?',(new_id,)))
  self.assertEqual([r['id'] for r in attention.pending(pid)],[new_id])
  page=BeautifulSoup(result.text,'html.parser')
  self.assertEqual([r['data-finding-id'] for r in page.select('[data-relevant-findings] [data-finding-id]')],[new_id])

if __name__=='__main__':unittest.main(verbosity=2)
