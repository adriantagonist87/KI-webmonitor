"""Recipient scope isolation, immutable archives, admin CRUD and durable mail jobs."""
import asyncio,copy,json,os,tempfile,unittest
from datetime import datetime,timedelta,timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import AsyncMock,patch
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
scratch=tempfile.TemporaryDirectory(prefix='.distribution-tests-',dir='/data');os.environ['DATA_DIR']=scratch.name
import store,web,engine,distributions as lists,email_reports,report_batches,report_mail,transfer,tag_registry

class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=scratch.name);self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);store.DATA=web.DATA=engine.DATA=root;store.CONFIG=web.CONFIG=root/'settings.json';store.SECRETS=web.SECRETS=root/'credentials.json';store.init()
        tag_registry.save('Arbeit','#df482a');tag_registry.save('PRIVATE-TAG-CANARY','#2f88d0')
        self.tags=tag_registry.all_tags();self.work=next(t['id'] for t in self.tags if t['name']=='Arbeit');self.private=next(t['id'] for t in self.tags if t['name']!='Arbeit')
        for pid,name,tags in [('public','Öffentliche Änderung',['Arbeit','PRIVATE-TAG-CANARY']),('quiet','Öffentliche Quelle',['Arbeit']),('private','PRIVATE-PROJECT-CANARY',['PRIVATE-TAG-CANARY']),('notice','ADMIN-NOTICE-PROJECT',['Arbeit']),('failed','ADMIN-FAILED-PROJECT',['Arbeit'])]:
            store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,tags) VALUES(?,?,?,?,?,?,?,?,?,?)',(pid,name,'https://'+pid+'.example.org/','Prüfen',pid+'.example.org','0 6 * * *',0,store.now(),store.now(),json.dumps(tags)))
        self.client=TestClient(web.app);self.addCleanup(self.client.close);self.client.get('/');self.csrf=self.client.cookies.get('kiwm_csrf')
    def config(self,**kw):return {'alias':'Arbeit','emails':['one@example.test','two@example.test'],'project_ids':[],'tag_ids':[self.work],'enabled':1,**kw}
    def create(self,**kw):return lists.save(self.config(**kw))
    def model(self):
        start='2026-11-01T00:00:00+00:00';end='2026-11-07T00:00:00+00:00'
        projects=store.rows('SELECT * FROM projects');runs=[{'id':p['id'],'project_id':p['id'],'started':start,'status':'failed' if p['id']=='failed' else 'success','error':'ADMIN-ERROR-CANARY'} for p in projects]
        changes=[]
        for pid,relevance,reason in [('public','relevant','Neue fachliche Veröffentlichung erkannt.'),('private','relevant','PRIVATE-CHANGE-CANARY'),('notice','uncertain','ADMIN-HINT-CANARY')]:
            changes.append({'id':'change-'+pid,'project_id':pid,'kind':'Inhalt geändert','evidence':{'ai_assessment':{'relevance':relevance,'reason':reason}}})
        return email_reports.build_model(projects,runs,changes,self.tags,start,end,base_url='https://monitor.example.org')
    def batch(self,did=None,test=False):
        did=did or self.create();key=('test-' if test else 'week-')+store.uid();rid=store.uid()
        records=report_batches.records(rid,key,store.now(),self.model(),{},test)
        with store.db() as c:report_batches.insert(c,records)
        return rid,store.rows('SELECT * FROM reports WHERE parent_id=? ORDER BY recipient_email',(rid,))
    def child(self):return self.batch()[1][0]
    def test_01_addresses_alias_and_deduplication(self):
        did=self.create(emails='first+report@example.test\nsecond@example.test;first+report@EXAMPLE.TEST')
        d=lists.get(did);self.assertEqual(d['emails'],['first+report@example.test','second@example.test']);self.assertEqual(d['alias'],'Arbeit')
    def test_02_invalid_address_and_header_injection(self):
        for value in ('a@example.test\r\nBcc: attacker@example.test','A <a@example.test>','a@example','a@@example.test','a@example.test,evil@example.test'):
            with self.subTest(value=value),self.assertRaises(ValueError):lists.email(value)
        with self.assertRaises(ValueError):lists.addresses('a@example.test\nBcc: attacker@example.test')
    def test_03_empty_and_foreign_selection_never_means_all(self):
        for kw in ({'tag_ids':[]},{'tag_ids':['missing']},{'project_ids':['missing']}):
            with self.assertRaises(ValueError):self.create(**kw)
        self.assertEqual(lists.all_lists(),[])
    def test_04_unique_alias_and_unknown_edit(self):
        self.create()
        with self.assertRaises(ValueError):self.create(alias='ARBEIT')
        with self.assertRaises(ValueError):lists.save(self.config(),'unknown')
    def test_05_admin_crud_checkbox_form_and_csrf(self):
        response=self.client.post('/distributions/save',data={'alias':'Arbeit','emails':'one@example.test','tag_ids':self.work,'enabled':'on'});self.assertEqual(response.status_code,403)
        response=self.client.post('/distributions/save',data={'csrf':self.csrf,'alias':'Arbeit','emails':'one@example.test','tag_ids':self.work,'enabled':'on'},follow_redirects=False);self.assertEqual(response.status_code,303)
        did=lists.all_lists()[0]['id'];page=BeautifulSoup(self.client.get('/distributions/'+did+'/edit').text,'html.parser')
        self.assertIsNotNone(page.select_one('input[name=tag_ids][checked]'));self.assertEqual(len(page.select('input[name=project_ids]')),5)
        self.assertIn('Verteilerlisten',self.client.get('/').text)
    def test_06_pause_delete_preserve_report_history(self):
        did=self.create();rid,children=self.batch(did)
        lists.toggle(did);self.assertFalse(lists.get(did)['enabled']);lists.remove(did)
        self.assertEqual(lists.all_lists(),[]);self.assertEqual(store.one('SELECT count(*) AS n FROM reports')['n'],3)
        self.assertEqual(self.client.get('/reports/'+children[0]['id']).status_code,200)
    def test_07_tag_selection_does_not_leak_another_tag_on_shared_project(self):
        did=self.create();model,scope=email_reports.for_distribution(self.model(),lists.get(did));bundle=email_reports.render(model)
        self.assertEqual(set(model['projects']),{'public','quiet'});self.assertEqual([g['name'] for g in model['groups']],['Arbeit'])
        for canary in ('PRIVATE-TAG-CANARY','PRIVATE-PROJECT-CANARY','PRIVATE-CHANGE-CANARY','ADMIN-HINT-CANARY','ADMIN-ERROR-CANARY','ADMIN-NOTICE-PROJECT','ADMIN-FAILED-PROJECT','Hinweise','Webmonitor ›'):
            self.assertNotIn(canary,bundle['html']+bundle['text'])

    def test_recipient_links_use_one_readable_page_that_names_the_files(self):
        did=self.create()
        overview='https://public.example.org/veroeffentlichungen'
        article='https://public.example.org/artikel/neue-regel'
        document='https://files.example.org/neue-richtlinie.pdf'
        attachment='https://files.example.org/download?id=77'
        app_link='https://monitor.example.org/artifacts/internal.pdf'
        navigation='https://public.example.org/abonnieren'
        project=store.one('SELECT * FROM projects WHERE id=?',('public',))
        run={'id':'proof','project_id':'public','started':store.now(),'status':'success'}
        changes=[{'project_id':'public','run_id':'proof','url':overview,'kind':'Inhalt geändert',
                  'evidence':{'links_added':[article,document,attachment,app_link,navigation,document],
                              'ai_assessment':{'relevance':'relevant','reason':'Neue Richtlinie veröffentlicht.'}}}]
        snapshots=[{'project_id':'public','run_id':'proof','kind':'page','url':url,'links':json.dumps(links)}
                   for url,links in ((overview,[article,document,attachment,app_link,navigation]),
                                     (article,[document,attachment]))]
        snapshots.append({'project_id':'public','run_id':'proof','kind':'document','url':attachment,'links':'[]'})
        model=email_reports.build_model([project],[run],changes,self.tags,store.now(),store.now(),
                                        base_url='https://monitor.example.org',source_snapshots=snapshots)
        filtered,_=email_reports.for_distribution(model,lists.get(did))
        filtered['projects']['public']['app_url']='https://monitor.example.org/runs/proof'
        bundle=email_reports.render(filtered)
        anchors=BeautifulSoup(bundle['html'],'html.parser').select('a[href]')
        self.assertEqual([(a['href'],a.get_text()) for a in anchors],[(article,'Originalquelle')])
        self.assertIn(article,bundle['text'])
        for url in (overview,document,attachment,navigation):
            self.assertNotIn(url,bundle['html']+bundle['text'])
        self.assertNotIn('monitor.example.org',bundle['html']+bundle['text'])
        admin=BeautifulSoup(email_reports.render(model)['html'],'html.parser')
        admin_rows=admin.select('tr[data-project-id="public"]')
        self.assertEqual(len(admin_rows),2)
        for row in admin_rows:
            self.assertEqual([a['href'] for a in row.select('a[href]')],
                             [article,'https://monitor.example.org/projects/public'])

    def test_old_recipient_links_are_filtered_at_delivery_preview_and_eml(self):
        _,children=self.batch();child=children[0];rid=child['id']
        path=store.DATA/'reports'/(rid+'.html')
        internal='https://old-monitor.example.org/runs/example'
        legacy=path.read_text(encoding='utf-8').replace('</body>',
            '<a href="'+internal+'">Webmonitor ›</a><a href="/reports/admin">Analyse öffnen</a></body>')
        path.write_text(legacy,encoding='utf-8')
        store.execute('UPDATE reports SET content=content||? WHERE id=?',('\nWebmonitor: '+internal,rid))
        sender=AsyncMock(return_value='production')
        self.assertTrue(asyncio.run(report_mail.deliver(rid,sender))['sent'])
        text,_,_,html,_,_=sender.call_args.args
        eml=self.client.get('/reports/'+rid+'/message.eml')
        self.assertEqual(eml.status_code,200)
        parsed=BytesParser(policy=policy.default).parsebytes(eml.content)
        parts=(text,html,self.client.get('/reports/'+rid+'/html').text,
               parsed.get_body('plain').get_content(),parsed.get_body('html').get_content())
        for part in parts:
            self.assertNotIn(internal,part)
            self.assertNotIn('/reports/admin',part)
            self.assertNotIn('Analyse öffnen',part)
        self.assertIn('https://public.example.org/',html)
        self.assertEqual(path.read_text(encoding='utf-8'),legacy)

    def test_removed_document_uses_its_archived_publication_page(self):
        overview='https://public.example.org/'
        article=overview+'artikel/richtlinie'
        document=overview+'richtlinie%2Epdf'
        change={'project_id':'public','run_id':'proof','url':article,'kind':'Links geändert',
                'previous_id':'old-page','current_id':'new-page','evidence':{'links_removed':[document],
                'ai_assessment':{'relevance':'relevant','reason':'Richtlinie entfernt.'}}}
        snapshots=[{'id':sid,'project_id':'public','run_id':run,'kind':'page','url':url,'links':json.dumps(links)}
                   for sid,run,url,links in (('old-page','prior',article,[document]),
                                              ('new-page','proof',article,[]),
                                              ('overview','proof',overview,[article]))]
        project=store.one('SELECT * FROM projects WHERE id=?',('public',))
        model=email_reports.build_model([project],[],[change],self.tags,store.now(),store.now(),
                                        source_snapshots=snapshots)
        bundle=email_reports.render(model)
        self.assertEqual(model['projects']['public']['source_page'],article)
        self.assertNotIn(document,bundle['html']+bundle['text'])
    def test_08_explicit_project_is_in_a_neutral_group(self):
        did=self.create(tag_ids=[],project_ids=['quiet']);model,_=email_reports.for_distribution(self.model(),lists.get(did))
        self.assertEqual([g['name'] for g in model['groups']],['Ausgewählte Projekte']);self.assertEqual(set(model['projects']),{'quiet'})
    def test_09_union_deduplicates_projects(self):
        did=self.create(project_ids=['public','private']);model,_=email_reports.for_distribution(self.model(),lists.get(did))
        self.assertEqual(model['counts']['sources'],3);self.assertEqual(set(model['projects']),{'public','quiet','private'})
    def test_10_global_report_excludes_notice_and_failure_rows(self):
        bundle=email_reports.render(self.model())
        for value in ('ADMIN-HINT-CANARY','ADMIN-NOTICE-PROJECT','ADMIN-FAILED-PROJECT','Hinweise'):
            self.assertNotIn(value,bundle['html']+bundle['text'])
        self.assertIn('Neue fachliche Veröffentlichung',bundle['html'])
        self.assertIn('Keine Änderungen',bundle['text'])
    def test_11_later_failure_keeps_confirmed_change_but_not_problem_note(self):
        m=self.model();m['projects']['public']['problem']='ADMIN-LATER-FAILURE-CANARY'
        did=self.create();filtered,_=email_reports.for_distribution(m,lists.get(did));bundle=email_reports.render(filtered)
        self.assertIn('Neue fachliche Veröffentlichung',bundle['html']);self.assertNotIn('ADMIN-LATER-FAILURE',bundle['html']);self.assertEqual(filtered['projects']['public']['category'],'changed')
    def test_12_notice_only_is_skipped_and_never_claimed_unchanged(self):
        did=self.create(tag_ids=[],project_ids=['notice','failed']);rid,children=self.batch(did)
        self.assertTrue(all(c['mail_status']=='skipped' for c in children));self.assertEqual(len(children),2)
        html=email_reports.saved_html(children[0]);self.assertNotIn('ADMIN-NOTICE',html);self.assertNotIn('Keine Änderungen',html)
    def test_13_recipient_archive_does_not_change_with_list_edits(self):
        did=self.create();_,children=self.batch(did);child=children[0];before=email_reports.saved_html(child)
        lists.save(self.config(alias='Geänderter Alias',tag_ids=[self.private]),did)
        self.assertEqual(email_reports.saved_html(child),before);self.assertEqual(child['distribution_alias'],'Arbeit')
    def test_14_separate_report_per_address(self):
        _,children=self.batch();self.assertEqual([c['recipient_email'] for c in children],['one@example.test','two@example.test'])
        self.assertEqual(email_reports.saved_html(children[0]),email_reports.saved_html(children[1]))
    def test_15_success_is_durable_and_not_sent_twice(self):
        child=self.child();sender=AsyncMock(return_value='production')
        asyncio.run(report_mail.deliver(child['id'],sender));asyncio.run(report_mail.deliver(child['id'],sender))
        self.assertEqual(sender.await_count,1);self.assertEqual(sender.call_args.args[-1],'one@example.test')
        self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(child['id'],))['mail_status'],'sent')
        self.assertEqual([r['status'] for r in store.rows('SELECT status FROM report_mail_events WHERE report_id=? ORDER BY id',(child['id'],))],['pending','sending','sent'])
    def test_16_definite_failure_plans_a_delayed_retry(self):
        child=self.child();before=datetime.now(timezone.utc);sender=AsyncMock(side_effect=ValueError('SECRET-NOT-LOGGED'))
        asyncio.run(report_mail.deliver(child['id'],sender));row=store.one('SELECT * FROM reports WHERE id=?',(child['id'],))
        self.assertEqual(row['mail_status'],'failed');self.assertGreaterEqual(datetime.fromisoformat(row['next_attempt']),before+timedelta(minutes=30))
        asyncio.run(report_mail.deliver(child['id'],sender));self.assertEqual(sender.await_count,1);self.assertNotIn('SECRET-NOT-LOGGED',row['error'])
    def test_17_uncertain_delivery_never_retries_automatically(self):
        child=self.child();sender=AsyncMock(side_effect=TimeoutError())
        asyncio.run(report_mail.deliver(child['id'],sender));asyncio.run(report_mail.deliver(child['id'],sender));self.assertEqual(sender.await_count,1)
        with self.assertRaises(ValueError):report_mail.retry(child['id'])
        report_mail.retry(child['id'],True);self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(child['id'],))['mail_status'],'pending')
    def test_18_concurrent_dispatch_claims_once(self):
        child=self.child()
        async def send(*args):await asyncio.sleep(.02);return 'production'
        sender=AsyncMock(side_effect=send)
        async def run():await asyncio.gather(report_mail.deliver(child['id'],sender),report_mail.deliver(child['id'],sender))
        asyncio.run(run());self.assertEqual(sender.await_count,1)
    def test_19_interrupted_claim_requires_admin_review(self):
        child=self.child();store.execute("UPDATE reports SET mail_status='sending',next_attempt=? WHERE id=?",((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),child['id']))
        report_mail.recover();self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(child['id'],))['mail_status'],'unknown')
    def test_20_changed_scope_cancels_old_pending_mail(self):
        did=self.create();_,children=self.batch(did);lists.save(self.config(tag_ids=[self.private]),did);sender=AsyncMock(return_value='production')
        asyncio.run(report_mail.deliver(children[0]['id'],sender));sender.assert_not_awaited();self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(children[0]['id'],))['mail_status'],'cancelled')
    def test_21_removed_address_and_paused_list_cancel(self):
        did=self.create();_,children=self.batch(did);lists.save(self.config(emails=['two@example.test']),did);sender=AsyncMock(return_value='production')
        asyncio.run(report_mail.deliver(children[0]['id'],sender));sender.assert_not_awaited();lists.toggle(did)
        asyncio.run(report_mail.deliver(children[1]['id'],sender));sender.assert_not_awaited()
    def test_22_tag_membership_changes_fail_closed(self):
        child=self.child();store.execute("UPDATE projects SET tags='[]' WHERE id='public'");sender=AsyncMock(return_value='production')
        asyncio.run(report_mail.deliver(child['id'],sender));sender.assert_not_awaited()
    def test_23_test_report_forces_test_transport(self):
        _,children=self.batch(test=True);sender=AsyncMock(return_value='preview')
        asyncio.run(report_mail.deliver(children[0]['id'],sender));self.assertTrue(sender.call_args.args[2])
    def test_24_report_details_show_parent_recipient_and_events(self):
        rid,children=self.batch();page=self.client.get('/reports/'+children[0]['id']).text
        self.assertIn('one@example.test',page);self.assertIn('/reports/'+rid,page);self.assertIn('Versandprotokoll',page);self.assertIn('Gespeicherte Auswahl',page)
        self.assertEqual(self.client.get('/reports?scope=distribution').status_code,200)
    def test_25_configuration_export_includes_lists(self):
        self.create();path=store.DATA/'config.json';payload=transfer.export_to(path);transfer.validate(payload)
        self.assertEqual(len(payload['distributions']),1);self.assertEqual(payload['version'],8)
    def test_26_archive_roundtrip_keeps_mail_history_without_resending(self):
        rid,children=self.batch();sender=AsyncMock(return_value='production');asyncio.run(report_mail.deliver(children[0]['id'],sender))
        archive=store.DATA/'archive.zip';transfer.export_to(archive,history=True);payload=transfer.read_package(archive)
        dest=store.DATA/'restored';dest.mkdir();store.DATA=web.DATA=engine.DATA=dest;store.CONFIG=web.CONFIG=dest/'settings.json';store.SECRETS=web.SECRETS=dest/'credentials.json';store.init()
        transfer.import_package(archive,payload)
        self.assertEqual(store.one('SELECT count(*) AS n FROM reports')['n'],3);self.assertEqual(store.one('SELECT count(*) AS n FROM report_mail_events')['n'],5)
        self.assertFalse(lists.all_lists()[0]['enabled']);self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(children[1]['id'],))['mail_status'],'imported')
        self.assertIn('Neue fachliche Veröffentlichung',email_reports.saved_html(store.one('SELECT * FROM reports WHERE id=?',(children[0]['id'],))))
    def test_27_import_rejects_recipient_header_injection(self):
        self.create();path=store.DATA/'config.json';payload=transfer.export_to(path)
        payload['distributions'][0]['emails']=json.dumps(['one@example.test\r\nBcc: evil@example.test'])
        with self.assertRaises(transfer.Invalid):transfer.validate(payload)
    def test_28_report_list_has_pagination(self):
        for i in range(52):store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status) VALUES(?,?,?,?,?,?,?)',('r'+str(i),'old-'+str(i),store.now(),'start','end','Archived','preview'))
        html=BeautifulSoup(self.client.get('/reports').text,'html.parser');self.assertEqual(len(html.select('tbody tr')),50)
        self.assertIsNotNone(html.find('a',string='Weiter'));self.assertIn('2 / 2',self.client.get('/reports?page=2').text)
        filtered=BeautifulSoup(self.client.get('/reports?regular=1').text,'html.parser')
        self.assertIn('regular=1',filtered.find('a',string='Weiter')['href'])

    def test_29_failed_recipient_does_not_block_the_other_reports(self):
        import httpx
        rid,children=self.batch();real=httpx.AsyncClient
        def handler(request):
            payload=json.loads(request.content)
            return httpx.Response(502,json={'failure_kind':'definite'}) if payload['recipient']=='one@example.test' else httpx.Response(200,json={'mode':'mailpit'})
        asyncio.run(report_batches.dispatch([rid,*[r['id'] for r in children]],lambda **kw:real(transport=httpx.MockTransport(handler))))
        self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(rid,))['mail_status'],'sent')
        self.assertEqual([store.one('SELECT mail_status FROM reports WHERE id=?',(r['id'],))['mail_status'] for r in children],['failed','sent'])
    def test_30_malformed_success_response_is_not_assumed_undelivered(self):
        import httpx
        child=self.child();real=httpx.AsyncClient
        asyncio.run(report_batches.dispatch([child['id']],lambda **kw:real(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='not-json')))))
        row=store.one('SELECT * FROM reports WHERE id=?',(child['id'],));self.assertEqual(row['mail_status'],'unknown');self.assertIsNone(row['next_attempt'])
    def test_31_new_routes_are_admin_protected(self):
        _,children=self.batch()
        auth=web.admin_auth.AdminAuth(store.DATA,{'KIWM_ADMIN_USERNAME':'test-admin','KIWM_ADMIN_PASSWORD':'FAKE-ADMIN-PASSWORD-12345'})
        with patch.object(web.admin_auth,'current',return_value=auth):
            client=TestClient(web.app)
            for path in ('/distributions','/distributions/new','/reports/'+children[0]['id'],'/reports/'+children[0]['id']+'/html'):
                response=client.get(path,follow_redirects=False);self.assertEqual(response.status_code,303);self.assertTrue(response.headers['location'].startswith('/login'))
            client.close()
    def test_32_legacy_export_version_seven_still_imports(self):
        store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status) VALUES(?,?,?,?,?,?,?)',('legacy','legacy-period',store.now(),'start','end','Archived','preview'))
        path=store.DATA/'legacy.zip';payload=transfer.export_to(path,history=True);payload['version']=7;payload.pop('distributions');payload['history'].pop('report_mail_events')
        for r in payload['history']['reports']:
            for key in lists.REPORT_DEFAULTS:r.pop(key)
        transfer.validate(payload);self.assertEqual(payload['history']['reports'][0]['audience'],'admin')
    def test_33_batch_generation_is_once_per_week_with_one_analysis(self):
        import httpx
        self.create()
        for pid in ('public','quiet'):
            for i in range(2):store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',(pid+str(i),pid,store.now(),store.now(),'success','test'))
        real=httpx.AsyncClient;calls=[]
        def handler(request):calls.append(json.loads(request.content));return httpx.Response(200,json={'mode':'preview'})
        async def identity(model,*args):return model
        compact=AsyncMock(side_effect=identity)
        with patch.object(engine.email_reports,'compact_headlines',compact),patch.object(engine.httpx,'AsyncClient',side_effect=lambda **kw:real(transport=httpx.MockTransport(handler))):
            first=asyncio.run(engine.make_report());second=asyncio.run(engine.make_report())
        self.assertEqual(first,second);self.assertEqual(compact.await_count,1);self.assertEqual(len(calls),3);self.assertEqual(store.one('SELECT count(*) AS n FROM reports')['n'],3)

    def test_34_headline_requests_never_mix_projects(self):
        requests=[]
        async def llm(messages,*args,**kwargs):requests.append(messages);return {'choices':[{'message':{'content':'{"headlines":[]}'}}]}
        asyncio.run(email_reports.compact_headlines(self.model(),llm,'local-model'))
        self.assertEqual(len(requests),2)
        for messages in requests:
            payload=json.loads(messages[1]['content']);self.assertEqual(len(payload),1)
            if payload[0]['id']=='public':self.assertNotIn('PRIVATE-',json.dumps(messages));self.assertNotIn('ADMIN-HINT-',json.dumps(messages))

if __name__=='__main__':unittest.main(verbosity=2)
