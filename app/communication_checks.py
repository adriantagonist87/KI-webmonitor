"""Notifications, TLS mail, private configuration and real app/gateway API flow; no external sends."""
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock,AsyncMock
import httpx
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import configuration
import communication_config as config
import communication_transport as transport
import notifications
import telegram_message
import store,web,engine,gateway,transfer,retry_jobs

TOKEN='123456789:'+('SYNTHETIC_'*4)


class Communications(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/data');self.root=Path(self.temp.name)
        self.saved=store.DATA,store.CONFIG,store.SECRETS,web.DATA,web.CONFIG,web.SECRETS,engine.DATA
        store.DATA=web.DATA=engine.DATA=self.root
        store.CONFIG=web.CONFIG=self.root/'settings.json';store.SECRETS=web.SECRETS=self.root/'credentials.json'
        self.env=patch.dict(os.environ,dict.fromkeys((*configuration.ENVIRONMENT_NAMES,*config.ENVIRONMENT_NAMES),''));self.env.start()
        store.init();store.save_json(store.CONFIG,{'telegram_mode':'projects','telegram_chat_id':'12345'})
        store.save_json(store.SECRETS,{'telegram_token':TOKEN})
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,telegram_enabled) VALUES(?,?,?,?,?,?,?,?,?,?)',('project','Testprojekt','https://example.test/','Prüfen','example.test','0 6 * * *',1,store.next_due('0 6 * * *'),store.now(),1))
        self.client=TestClient(web.app);self.client.get('/');self.csrf=self.client.cookies['kiwm_csrf']

    def tearDown(self):
        self.client.close();self.env.stop()
        store.DATA,store.CONFIG,store.SECRETS,web.DATA,web.CONFIG,web.SECRETS,engine.DATA=self.saved
        self.temp.cleanup()

    def post(self,path,**values):return self.client.post(path,data={'csrf':self.csrf,**values},follow_redirects=False)

    def run_record(self,status='success',changed=True,relevance='relevant',baseline=False):
        rid=store.uid();stamp=store.now()
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,error,summary,origin) VALUES(?,?,?,?,?,?,?,?)',(rid,'project',stamp,stamp,status,'Server nicht erreichbar' if status!='success' else None,'## Änderung\nDie Frist ist jetzt 42 Tage.\n<details><summary>Technik</summary>UNWICHTIGE DETAILS</details>','test'))
        if changed:
            store.execute('INSERT INTO changes(id,project_id,run_id,url,kind,detected,previous_id,current_id,evidence,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?)',(store.uid(),'project',rid,'https://example.test/file.pdf','Neues Dokument' if baseline else 'Datei geändert',stamp,None if baseline else 'old','current',json.dumps({'ai_assessment':{'relevance':relevance,'summary':'Die Datei hat einen anderen SHA-256-Hash.'}}),store.uid()))
        return rid

    def test_01_environment_typed_and_no_secret_in_settings(self):
        env={'KIWM_SMTP_ENABLED':'false','KIWM_SMTP_PORT':'465','KIWM_TELEGRAM_MODE':'all','KIWM_TELEGRAM_BOT_TOKEN':TOKEN}
        result=config.resolve({},config.FIELDS,env)
        self.assertIs(result['smtp_enabled'],False);self.assertEqual(result['smtp_port'],465)
        self.assertNotIn(TOKEN,json.dumps(result));self.assertEqual(config.resolve({},config.SECRETS,env)['telegram_token'],TOKEN)

    def test_02_invalid_values_are_private(self):
        for field,value in [('telegram_token','secret-invalid'),('telegram_chat_id','https://invalid'),('smtp_port','0'),('smtp_tls','none'),('smtp_enabled','maybe'),('smtp_host','https://smtp.test'),('smtp_to','x\nBcc: other@test.test')]:
            with self.subTest(field=field),self.assertRaises(configuration.ConfigurationError) as error:config.validate(field,value)
            self.assertNotIn(value,str(error.exception))

    def test_03_secrets_rejected_from_public_settings(self):
        for key in ('telegram_token','KIWM_TELEGRAM_BOT_TOKEN','KIWM_SMTP_PASSWORD'):
            with self.assertRaises(configuration.ConfigurationError):configuration.public_settings({key:TOKEN})

    def test_04_sidebar_pages_separate_and_secret_masked(self):
        for url in ('/telegram','/smtp'):
            response=self.client.get(url);self.assertEqual(response.status_code,200)
            self.assertNotIn(TOKEN,response.text)
        soup=BeautifulSoup(self.client.get('/settings').text,'html.parser')
        self.assertIsNone(soup.select_one('input[name=smtp_host]'))
        self.assertIsNotNone(soup.select_one('a.nav-link[href="/telegram"]'))
        self.assertIsNotNone(soup.select_one('a.nav-link[href="/smtp"]'))
        self.assertIsNotNone(soup.select_one('select[name=telegram_mode]'))

    def test_05_telegram_form_overrides_stack_defaults(self):
        store.save_json(store.CONFIG,{})
        store.save_json(store.SECRETS,{})
        web_token='987654321:'+('WEB_SYNTHETIC_'*3)
        with patch.dict(os.environ,{'KIWM_TELEGRAM_BOT_TOKEN':TOKEN,'KIWM_TELEGRAM_CHAT_ID':'99999','KIWM_TELEGRAM_MODE':'all'}):
            soup=BeautifulSoup(self.client.get('/telegram').text,'html.parser')
            mode=soup.select_one('[name=telegram_mode]')
            self.assertFalse(mode.has_attr('disabled'))
            self.assertEqual(mode.select_one('option[selected]')['value'],'all')
            self.assertEqual(mode['data-config-source'],'environment')
            self.assertFalse(soup.select_one('[name=telegram_token]').has_attr('readonly'))
            self.assertEqual(store.credentials()['telegram_token'],TOKEN)
            self.assertEqual(self.post('/telegram/save',telegram_chat_id='11111',telegram_mode='off',telegram_token=web_token).status_code,303)
            self.assertEqual(store.settings()['telegram_chat_id'],'11111')
            self.assertEqual(store.settings()['telegram_mode'],'off')
            self.assertEqual(store.credentials()['telegram_token'],web_token)
            self.assertNotIn(web_token,self.client.get('/telegram').text)
        self.assertEqual(store.persisted_settings()['telegram_chat_id'],'11111')

    def test_06_ui_token_kept_or_cleared_and_no_env_persisted(self):
        self.assertEqual(self.post('/telegram/save',telegram_chat_id='-12345',telegram_mode='all').status_code,303)
        self.assertEqual(store.credentials()['telegram_token'],TOKEN)
        fallback='111122223:'+('STACK_SYNTHETIC_'*3)
        with patch.dict(os.environ,{'KIWM_TELEGRAM_BOT_TOKEN':fallback}):
            self.post('/telegram/save',telegram_chat_id='12345',telegram_mode='off',clear_secret='on')
            self.assertEqual(store.credentials()['telegram_token'],fallback)
        self.assertNotIn('telegram_token',store.persisted_credentials())

    def test_07_connection_actions_require_csrf(self):
        for url in ('/telegram/test','/smtp/test','/telegram/save','/settings/telegram'):
            self.assertEqual(self.client.post(url,data={'action':'message'}).status_code,403)

    def test_08_global_off_projects_all(self):
        p={'telegram_enabled':0}
        self.assertFalse(notifications.enabled(p,{'telegram_mode':'off'}));self.assertFalse(notifications.enabled(p,{'telegram_mode':'projects'}))
        self.assertTrue(notifications.enabled(p,{'telegram_mode':'all'}));p['telegram_enabled']=1
        self.assertTrue(notifications.enabled(p,{'telegram_mode':'projects'}))
        with patch.dict(os.environ,{'KIWM_TELEGRAM_MODE':'all'}):
            self.post('/settings/telegram',telegram_mode='off')
            self.assertEqual(store.settings()['telegram_mode'],'off')

    def test_09_unchanged_irrelevant_uncertain_and_baseline_are_silent(self):
        for args in ({'changed':False},{'relevance':'irrelevant'},{'relevance':'uncertain'},{'baseline':True}):notifications.queue_run(self.run_record(**args))
        self.assertEqual(store.rows('SELECT * FROM notifications'),[])

    def test_10_relevant_change_queued_once_with_primary_summary(self):
        rid=self.run_record();notifications.queue_run(rid);notifications.queue_run(rid)
        records=store.rows('SELECT * FROM notifications');self.assertEqual(len(records),1)
        self.assertIn('42 Tage',records[0]['message']);self.assertNotIn('UNWICHTIGE DETAILS',records[0]['message'])
        self.assertIn('SHA-256',records[0]['message']);self.assertNotIn(TOKEN,records[0]['message'])
        self.assertIn('file.pdf',records[0]['message'])

    def test_11_failures_and_partial_send_without_changes(self):
        for status in ('failed','partial'):notifications.queue_run(self.run_record(status,False))
        self.assertEqual(len(store.rows('SELECT * FROM notifications')),2)

    def test_12_old_runs_not_backfilled_by_initialization(self):
        self.run_record()
        with store.db() as c:notifications.initialize(c)
        self.assertEqual(store.rows('SELECT * FROM notifications'),[])

    def test_13_pending_cancelled_when_globally_disabled(self):
        notifications.queue_run(self.run_record());store.save_json(store.CONFIG,{'telegram_mode':'off'})
        asyncio.run(notifications.dispatch_one());self.assertEqual(store.one('SELECT status FROM notifications')['status'],'cancelled')

    def test_14_app_gateway_bot_end_to_end(self):
        self.enterContext(patch.dict(os.environ,{'KIWM_TELEGRAM_MODE':'off','KIWM_TELEGRAM_CHAT_ID':'99999',
                                                'KIWM_TELEGRAM_BOT_TOKEN':'111122223:'+('STACK_SYNTHETIC_'*3),
                                                'KIWM_PUBLIC_URL':'','KIWM_LAN_URL':'http://192.168.0.10:8765'}))
        store.execute("UPDATE projects SET name='Testprojekt 🧪' WHERE id='project'")
        rid=self.run_record();notifications.queue_run(rid);calls=[];real_async=httpx.AsyncClient;real_sync=httpx.Client
        def bot(req):calls.append(req);return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
        def sync(*a,**k):return real_sync(transport=httpx.MockTransport(bot))
        def asynchronous(*a,**k):return real_async(transport=httpx.ASGITransport(app=gateway.app))
        with patch.object(httpx,'AsyncClient',side_effect=asynchronous),patch.object(httpx,'Client',side_effect=sync):asyncio.run(notifications.dispatch_one())
        self.assertEqual(store.one('SELECT status FROM notifications')['status'],'sent');self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].url.host,'api.telegram.org');self.assertIn(TOKEN,calls[0].url.path)
        payload=json.loads(calls[0].content)
        self.assertEqual(payload['chat_id'],'12345');self.assertIn('42 Tage',payload['text']);self.assertNotIn('parse_mode',payload)
        text = payload['text'].encode('utf-16-le')
        def label(entity):
            offset = entity['offset'] * 2
            return text[offset:offset + entity['length'] * 2].decode('utf-16-le')
        links = {label(e): e['url'] for e in payload['entities'] if e['type'] == 'text_link'}
        self.assertEqual(links, {'Quelle öffnen':'https://example.test/',
                                 'Auswertung im Webmonitor öffnen':'http://192.168.0.10:8765/runs/'+rid})
        self.assertIn('▸ Änderung', [label(e) for e in payload['entities'] if e['type'] == 'bold'])
        asyncio.run(notifications.dispatch_one());self.assertEqual(len(calls),1)

    def test_15_telegram_rate_limit_preserves_retry_after(self):
        real=httpx.Client
        def factory(*a,**k):return real(transport=httpx.MockTransport(lambda r:httpx.Response(429,json={'ok':False,'parameters':{'retry_after':123}})))
        with patch.object(httpx,'Client',side_effect=factory),self.assertRaises(transport.DeliveryError) as caught:transport.telegram_send('test')
        self.assertEqual(caught.exception.retry_after,124)

    def test_16_telegram_errors_never_echo_upstream_token(self):
        real=httpx.Client
        def factory(*a,**k):return real(transport=httpx.MockTransport(lambda r:httpx.Response(401,json={'ok':False,'description':TOKEN})))
        with patch.object(httpx,'Client',side_effect=factory):result=TestClient(gateway.app).post('/telegram/test',json={'action':'connection'}).json()
        self.assertFalse(result['ok']);self.assertNotIn(TOKEN,json.dumps(result))

    def test_17_ambiguous_delivery_not_retried(self):
        notifications.queue_run(self.run_record());real=httpx.AsyncClient
        def factory(*a,**k):return real(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':False,'uncertain':True,'error':'Unklar'})))
        with patch.object(httpx,'AsyncClient',side_effect=factory):asyncio.run(notifications.dispatch_one())
        self.assertEqual(store.one('SELECT status FROM notifications')['status'],'uncertain')

    def test_18_outbox_survives_restart_and_marks_inflight_uncertain(self):
        notifications.queue_run(self.run_record());store.execute("UPDATE notifications SET status='sending'")
        with store.db() as c:notifications.initialize(c)
        self.assertEqual(store.one('SELECT status FROM notifications')['status'],'uncertain')

    def test_19_message_utf16_limit_with_emoji(self):
        rid=self.run_record();p=store.one('SELECT * FROM projects');run=store.one('SELECT * FROM runs WHERE id=?',(rid,));run['summary']='😀'*10000
        p['name']='😀'*180
        p['url']='https://example.test/quelle?token=SECRET#abschnitt'
        text=notifications.message(p,run,notifications.relevant_changes(rid));self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        linked, entities = telegram_message.with_source_link(text)
        self.assertIn('Quelle öffnen', linked)
        self.assertEqual(entities[0]['url'], 'https://example.test/quelle')
        self.assertNotIn('SECRET', text)
        for invalid in ('javascript:alert(1)', 'https://user@example.test/', 'https://example.test/a\nb'):
            with self.subTest(invalid=invalid):
                self.assertEqual(telegram_message.with_source_link(telegram_message.compose(['Test'], invalid))[1], [])

    def test_19b_catalog_preview_keeps_complete_entries_and_drops_technical_details(self):
        rid = self.run_record()
        project = store.one('SELECT * FROM projects')
        run = store.one('SELECT * FROM runs WHERE id=?', (rid,))
        entries = [f'### [Norm 🧪 {n:02d}](https://example.test/normen/{n}?token=PRIVATE)\n\n'
                   f'{"Ein neuer fachlicher Anwendungsbereich. " * 8}\n'
                   f'*Ausgabe: 2026-{n:02d} · Erschienen: 2026-{n:02d}-01*' for n in range(1, 13)]
        run['summary'] = ('Zwölf neue Normen.\n\n' + '\n\n'.join(entries)
                          + '\n\n<details><summary>Technik</summary>UNWICHTIGE DETAILS</details>')
        text, entities = telegram_message.for_delivery(notifications.message(project, run, notifications.relevant_changes(rid)))
        shown = [line for line in text.splitlines() if line.startswith('▸ Norm')]
        self.assertGreater(len(shown), 1)
        self.assertLess(len(shown), len(entries))
        self.assertEqual(text.count('\nAusgabe:'), len(shown))
        self.assertIn('▸ Norm 🧪 01\nEin neuer', text)
        self.assertIn('\n\nWeitere Details im vollständigen Bericht.', text)
        self.assertNotIn('UNWICHTIGE DETAILS', text)
        self.assertNotIn('PRIVATE', text)
        self.assertLessEqual(telegram_message.units(text), 4096)

    def test_20_telegram_probe_checks_identity_chat_and_explicit_send(self):
        with patch.object(transport,'telegram_call',return_value={'id':1}) as call:
            transport.telegram_probe(False);self.assertEqual([x.args[0] for x in call.call_args_list],['getMe','getChat'])
            call.reset_mock();transport.telegram_probe(True);self.assertEqual(call.call_args_list[-1].args[0],'sendMessage')

    def test_21_smtp_starttls_before_login(self):
        s={'smtp_host':'smtp.example.test','smtp_port':587,'smtp_user':'user','smtp_tls':'starttls'}
        with patch.object(transport.smtplib,'SMTP') as smtp:
            transport.smtp_client(s);names=[c[0] for c in smtp.return_value.method_calls]
            self.assertLess(names.index('starttls'),names.index('login'));smtp.return_value.ehlo.assert_called()

    def test_22_smtp_ssl_uses_verified_context(self):
        with patch.object(transport.smtplib,'SMTP_SSL') as smtp:
            transport.smtp_client({'smtp_host':'smtp.example.test','smtp_port':465,'smtp_tls':'ssl'})
            self.assertTrue(smtp.call_args.kwargs['context'].check_hostname)

    def test_23_smtp_probe_does_not_send_but_testmail_does_when_disabled(self):
        s={'smtp_host':'smtp.example.test','smtp_from':'from@example.test','smtp_to':'to@example.test','smtp_enabled':False}
        with patch.object(transport,'settings',return_value=s),patch.object(transport,'smtp_client') as opened:
            c=opened.return_value.__enter__.return_value;c.noop.return_value=(250,b'ok')
            transport.smtp_probe(False);c.send_message.assert_not_called()
            transport.smtp_probe(True);message=c.send_message.call_args.args[0]
            self.assertEqual(message.get_content_type(),'multipart/alternative');self.assertEqual(message['To'],'to@example.test')

    def test_24_smtp_form_overrides_stack_defaults_and_keeps_password_private(self):
        fields={'smtp_host':'web.example.test','smtp_port':'111','smtp_tls':'ssl','smtp_from':'from@example.test','smtp_to':'to@example.test'}
        with patch.dict(os.environ,{'KIWM_SMTP_HOST':'env.example.test','KIWM_SMTP_PORT':'465','KIWM_SMTP_PASSWORD':'SYNTHETIC-ENV-PASSWORD','KIWM_SMTP_ENABLED':'false'}):
            initial=self.client.get('/smtp').text
            soup=BeautifulSoup(initial,'html.parser')
            checkbox=soup.select_one('[name=smtp_enabled]')
            self.assertFalse(checkbox.has_attr('disabled'))
            self.assertFalse(checkbox.has_attr('checked'))
            self.assertEqual(checkbox['data-config-source'],'environment')
            self.assertNotIn('SYNTHETIC-ENV-PASSWORD',initial)
            self.assertEqual(self.post('/settings/smtp',**fields,smtp_enabled='on',smtp_password='synthetic-web-password').status_code,303)
            self.assertEqual(store.settings()['smtp_host'],'web.example.test')
            self.assertEqual(store.settings()['smtp_port'],111)
            self.assertIs(store.settings()['smtp_enabled'],True)
            self.assertEqual(store.credentials()['smtp_password'],'synthetic-web-password')
            self.assertNotIn('synthetic-web-password',self.client.get('/smtp').text)
        with patch.dict(os.environ,{'KIWM_SMTP_ENABLED':'true'}):
            self.assertEqual(self.post('/settings/smtp',**fields).status_code,303)
            self.assertIs(store.settings()['smtp_enabled'],False)
        self.assertEqual(store.persisted_credentials()['smtp_password'],'synthetic-web-password')

    def test_25_test_buttons_store_sanitized_status(self):
        real=httpx.AsyncClient
        def factory(*a,**k):return real(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True,'message':'Erfolgreich'})))
        with patch.object(httpx,'AsyncClient',side_effect=factory):self.assertEqual(self.post('/smtp/test',action='message').status_code,303)
        self.assertIn('Erfolgreich',self.client.get('/smtp').text)

    def test_26_new_export_roundtrip_keeps_project_flag_and_secret_opt_in(self):
        path=self.root/'export.json';transfer.export_to(path)
        payload=transfer.read_package(path);self.assertEqual(payload['projects'][0]['telegram_enabled'],1);self.assertIsNone(payload['credentials']);self.assertNotIn(TOKEN,path.read_text())
        transfer.import_package(path,payload);self.assertEqual(store.settings()['telegram_mode'],'off')

    def test_27_legacy_v6_import_defaults_telegram_off(self):
        path=self.root/'export.json';transfer.export_to(path);payload=json.loads(path.read_text());payload['version']=6
        for p in payload['projects']:p.pop('telegram_enabled')
        transfer.validate(payload);transfer.import_package(path,payload)
        self.assertEqual(store.one('SELECT telegram_enabled FROM projects')['telegram_enabled'],0)

    def test_28_project_form_saves_checkbox(self):
        self.assertEqual(self.post('/projects/save',id='project',name='Testprojekt',url='https://example.test/',prompt='Prüfen',domains='example.test',cron='0 6 * * *',telegram_present='1',telegram_enabled='on').status_code,303)
        self.assertEqual(store.one('SELECT telegram_enabled FROM projects')['telegram_enabled'],1)
        self.post('/projects/save',id='project',name='Testprojekt',url='https://example.test/',prompt='Prüfen',domains='example.test',cron='0 6 * * *',telegram_present='1')
        self.assertEqual(store.one('SELECT telegram_enabled FROM projects')['telegram_enabled'],0)

    def test_29_completion_hook_queues_after_primary_analysis(self):
        rid=store.enqueue('project');p=store.one('SELECT * FROM projects')
        async def finish(project,run):retry_jobs.finish_run(run,'failed',error='Technischer Fehler')
        with patch.object(engine.project_icons,'refresh',new=AsyncMock()),patch.object(engine,'monitor',side_effect=finish),patch.object(engine.site_access,'get_secret',return_value=None),patch.object(engine.site_access,'public_config',return_value=None):asyncio.run(engine.run_job(p,rid))
        self.assertEqual(store.one('SELECT run_id FROM notifications')['run_id'],rid)

    def test_30_bad_smtp_env_does_not_break_dashboard_or_inference_settings(self):
        with patch.dict(os.environ,{'KIWM_SMTP_PORT':'invalid','KIWM_TELEGRAM_BOT_TOKEN':'invalid'}):
            self.assertEqual(self.client.get('/').status_code,200)
            self.assertIn('smtp_port',store.settings()['_communication_errors'])
            self.assertEqual(store.credentials()['telegram_token'],TOKEN)
            store.save_json(store.SECRETS,{})
            self.assertIn('telegram_token',store.credentials()['_communication_errors'])
            self.assertNotIn('invalid',self.client.get('/telegram').text)

    def test_31_rate_limited_outbox_uses_future_due_and_stops_after_three_attempts(self):
        rid=self.run_record();notifications.queue_run(rid);real=httpx.AsyncClient
        def factory(*a,**k):return real(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':False,'error':'Begrenzt','retry_after':121})))
        with patch.object(httpx,'AsyncClient',side_effect=factory):
            asyncio.run(notifications.dispatch_one());row=store.one('SELECT * FROM notifications')
            self.assertEqual(row['status'],'queued');self.assertGreater(row['due_at'],store.now())
            store.execute("UPDATE notifications SET due_at='2000-01-01',attempts=2")
            asyncio.run(notifications.dispatch_one())
        self.assertEqual(store.one('SELECT status FROM notifications')['status'],'failed')
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?',(rid,))['status'],'success')

    def test_32_gateway_missing_credentials_cannot_claim_success(self):
        store.save_json(store.SECRETS,{})
        result=TestClient(gateway.app).post('/telegram/send',json={'text':'test'}).json()
        self.assertFalse(result['ok']);self.assertIn('fehlt',result['error'])

    def test_33_export_credentials_are_explicit_and_validated(self):
        path=self.root/'private.json';transfer.export_to(path,include_secrets=True)
        payload=transfer.read_package(path);self.assertEqual(payload['credentials']['telegram_token'],TOKEN)
        payload['credentials']['telegram_token']='invalid'
        with self.assertRaises(transfer.Invalid):transfer.validate(payload)

    def test_34_smtp_password_keeps_significant_spaces(self):
        self.assertEqual(config.validate('smtp_password',' significant spaces '),' significant spaces ')

    def test_35_gateway_connect_failure_queues_retry_without_changing_run(self):
        rid=self.run_record();notifications.queue_run(rid);real=httpx.AsyncClient
        def fail(req):raise httpx.ConnectError('synthetic',request=req)
        def factory(*a,**k):return real(transport=httpx.MockTransport(fail))
        with patch.object(httpx,'AsyncClient',side_effect=factory):asyncio.run(notifications.dispatch_one())
        self.assertEqual(store.one('SELECT status FROM notifications')['status'],'queued')
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?',(rid,))['status'],'success')

    def test_36_smtp_environment_password_preserves_spaces(self):
        self.assertEqual(config.resolve({},config.SECRETS,{'KIWM_SMTP_PASSWORD':' significant spaces '})['smtp_password'],' significant spaces ')

    def test_37_invalid_smtp_override_never_uses_fallback_port_for_weekly_mail(self):
        with patch.object(gateway,'settings',return_value={'smtp_enabled':True,'_communication_errors':{'smtp_port':'Ungültiger Port'}}),patch.object(gateway.smtplib,'SMTP') as smtp:
            with self.assertRaises(ValueError):gateway.send_mail('text','subject')
            smtp.assert_not_called()


if __name__=='__main__':unittest.main()
