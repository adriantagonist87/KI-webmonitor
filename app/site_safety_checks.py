"""Offline provider-protection and gentle-pacing regressions; no provider traffic."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import store
import site_safety as safety
import retry_jobs

GERMAN='Unsere Systeme haben ungewöhnlichen Datenverkehr aus Ihrem Computernetzwerk festgestellt. Bitte klicken Sie hier, wenn Sie kein Roboter sind.'
ENGLISH='Our systems have detected unusual traffic. Please click here to verify that you are not a bot.'


class SiteSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='.safety-',dir='/data')
        self.old=store.DATA,store.CONFIG,store.SECRETS
        store.DATA=Path(self.temp.name);store.CONFIG=store.DATA/'settings.json';store.SECRETS=store.DATA/'credentials.json'
        store.init()
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      ('p1','Provider','https://example.test/start','Read public content','example.test','0 6 * * *',1,'2027-01-01T05:00:00+00:00',store.now()))
        self.token=safety._CURRENT.set(None)

    def tearDown(self):
        safety.reset(self.token)
        store.DATA,store.CONFIG,store.SECRETS=self.old
        self.temp.cleanup()

    def run_record(self,error=None):
        rid=store.enqueue('p1','scheduled');self.assertTrue(retry_jobs.begin_attempt(rid))
        if error:
            retry_jobs.finish_run(rid,'failed',error=str(error),summary=str(error),agent_result=safety.result(error))
        return rid

    async def test_01_myra_security_message_on_original_url(self):
        self.assertEqual(safety.classify('https://example.test/start',title='Security',text=GERMAN),'security_check')
        self.assertEqual(safety.classify('https://example.test/start',title='Security Check',text=ENGLISH),'security_check')

    async def test_02_dedicated_challenge_urls_and_visible_captcha(self):
        self.assertEqual(safety.classify('https://example.test/myracloud-captcha/?q=x'),'captcha')
        self.assertEqual(safety.classify('https://example.test/start',visible_captcha=True),'captcha')

    async def test_03_regular_content_and_captcha_articles_are_not_challenges(self):
        self.assertIsNone(safety.classify('https://example.test/news',title='Neue Norm',text='In diesem Artikel geht es um CAPTCHA.'))
        self.assertIsNone(safety.classify('https://example.test/news',title='Security',text=GERMAN,article=True))
        self.assertIsNone(safety.classify('https://example.test/challenge/results',title='Wettbewerb'))

    async def test_04_rate_limit_and_retry_after_are_respected(self):
        now=datetime(2026,11,6,tzinfo=timezone.utc)
        self.assertEqual(safety.retry_after('120',now=now),(now+timedelta(seconds=120)).isoformat())
        self.assertEqual(safety.retry_after('Fri, 06 Nov 2026 01:00:00 GMT',now=now),(now+timedelta(hours=1)).isoformat())
        self.assertEqual(safety.retry_after('invalid',now=now),(now+timedelta(hours=1)).isoformat())
        with self.assertRaises(safety.ProtectionDetected) as caught:
            safety.raise_http('https://example.test/file.pdf',429,{'retry-after':'3600'})
        self.assertEqual(caught.exception.kind,'rate_limit');self.assertTrue(caught.exception.not_before)

    async def test_05_protection_response_html_and_maintenance_retry_after(self):
        with self.assertRaises(safety.ProtectionDetected):
            safety.raise_http('https://example.test/start',403,{},'<h1>Security</h1><p>'+GERMAN+'</p>')
        with self.assertRaises(safety.ProtectionDetected):
            safety.raise_http('https://example.test/start',503,{'retry-after':'600'})
        safety.raise_http('https://example.test/start',403,{},'<h1>Forbidden</h1>')

    async def test_06_protection_suppresses_only_the_extra_retry(self):
        before=store.one('SELECT enabled,next_due FROM projects WHERE id=?',('p1',))
        rid=self.run_record(safety.ProtectionDetected())
        self.assertIsNone(retry_jobs.project_retry('p1'))
        self.assertEqual(store.rows('SELECT * FROM run_retries'),[])
        self.assertEqual(store.one('SELECT enabled,next_due FROM projects WHERE id=?',('p1',)),before)
        self.assertIsNotNone(safety.protected_run(store.one('SELECT * FROM runs WHERE id=?',(rid,))))

    async def test_07_next_regular_cycle_is_not_blocked(self):
        self.run_record(safety.ProtectionDetected('captcha'))
        next_run=store.enqueue('p1','scheduled')
        self.assertTrue(next_run);self.assertTrue(retry_jobs.begin_attempt(next_run))
        retry_jobs.finish_run(next_run,'success',summary='Recovered')
        self.assertIsNone(safety.latest_protection('p1'))
        self.assertEqual(store.one('SELECT enabled FROM projects')['enabled'],1)

    async def test_08_normal_failure_still_gets_one_retry(self):
        rid=self.run_record()
        retry_jobs.finish_run(rid,'failed',error='Normal network timeout')
        self.assertEqual(retry_jobs.project_retry('p1')['state'],'pending')
        self.assertEqual(len(store.rows('SELECT * FROM run_retries')),1)

    async def test_09_untrusted_text_cannot_fake_a_protection_outcome(self):
        self.assertIsNone(safety.protected_run({'error':'Article says CAPTCHA','agent_result':safety.result(safety.ProtectionDetected())}))
        self.assertIsNone(safety.protected_run({'error':str(safety.ProtectionDetected()),'agent_result':'{"provider_protection":{"kind":"captcha"}}'}))

    async def test_10_default_is_gentle_and_profile_is_strict(self):
        self.assertEqual(safety.profile('p1'),'gentle')
        safety.save_profile('p1','standard');self.assertEqual(safety.profile('p1'),'standard')
        with self.assertRaises(ValueError):safety.save_profile('p1','unlimited')

    async def test_11_minimum_spacing_is_fixed_not_added_after_longer_work(self):
        clock=[0.0];sleeps=[]
        async def sleep(delta):sleeps.append(delta);clock[0]+=delta
        guard=safety.Guard('p1','https://example.test',5,clock=lambda:clock[0],sleep=sleep)
        await guard.wait();await guard.wait();clock[0]+=20;await guard.wait()
        self.assertEqual(sleeps,[5.0])

    async def test_12_observed_429_prevents_any_further_tool_request(self):
        guard=safety.Guard('p1','https://example.test',5)
        guard.note_response(SimpleNamespace(url='https://example.test/api',status=429,headers={},request=SimpleNamespace(resource_type='fetch')))
        with self.assertRaises(safety.ProtectionDetected):await guard.wait()
        other=safety.Guard('p1','https://example.test',5)
        other.note_response(SimpleNamespace(url='https://ads.test/api',status=429,headers={},request=SimpleNamespace(resource_type='fetch')))
        self.assertIsNone(other.pending)

    async def test_13_login_stops_before_credentials_or_a_second_navigation(self):
        from site_login import GenericFormLogin, LoginPolicy
        class Page:
            url='https://example.test/start'
            goto=AsyncMock(return_value=SimpleNamespace(status=403,headers={'content-type':'text/html'}))
            evaluate=AsyncMock(return_value={'title':'Security','text':GERMAN,'visible_captcha':False})
            close=AsyncMock()
            def locator(self,*args):raise AssertionError('No fields may be touched')
        page=Page();ctx=SimpleNamespace(new_page=AsyncMock(return_value=page))
        policy=LoginPolicy(origin='https://example.test',login_path='/login',form_selector='form',consent_origin='https://example.test')
        config={'origin':policy.origin,'login_url':policy.origin+'/login','form_selector':'form'}
        broker=GenericFormLogin(ctx,page.url,{'provider':'generic_form','origin':policy.origin},config,policy=policy)
        with self.assertRaises(safety.ProtectionDetected):await broker.ensure()
        self.assertEqual(page.goto.await_count,1);self.assertEqual(broker.attempts,0)

    async def test_14_run_controller_records_protection_without_model_or_archive(self):
        import engine
        rid=store.enqueue('p1','scheduled');p=store.one('SELECT * FROM projects WHERE id=?',('p1',))
        with patch.object(engine.project_icons,'refresh',new=AsyncMock()), \
             patch.object(engine.site_access,'get_secret',return_value=None), \
             patch.object(engine.site_access,'public_config',return_value=None), \
             patch.object(engine,'monitor',new=AsyncMock(side_effect=safety.ProtectionDetected())), \
             patch.object(engine,'llm',new=AsyncMock()) as llm:
            await engine.run_job(p,rid)
            llm.assert_not_awaited()
        r=store.one('SELECT * FROM runs WHERE id=?',(rid,))
        self.assertIsNotNone(safety.protected_run(r));self.assertEqual(r['status'],'failed')
        self.assertEqual(store.rows('SELECT * FROM snapshots'),[]);self.assertEqual(store.rows('SELECT * FROM changes'),[])
        self.assertIsNone(retry_jobs.project_retry('p1'))

    async def test_15_existing_retry_gets_cancelled_when_source_is_protected(self):
        rid=self.run_record();retry_jobs.finish_run(rid,'failed',error='Timeout')
        error=safety.ProtectionDetected()
        store.execute('UPDATE runs SET error=?,agent_result=? WHERE id=?',(str(error),safety.result(error),rid))
        retry_jobs.sweep_due()
        self.assertEqual(store.one('SELECT state FROM run_retries')['state'],'cancelled')

    async def test_16_provider_wait_does_not_send_early_requests(self):
        until=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
        self.run_record(safety.ProtectionDetected('rate_limit',until))
        store.enqueue('p1','scheduled')  # A new pending run must not hide the last terminal outcome.
        with self.assertRaises(safety.ProtectionDetected):safety.respect_wait('p1')
        self.assertEqual(store.one('SELECT enabled FROM projects')['enabled'],1)

    async def test_17_no_anonymous_favicon_probe_for_configured_login(self):
        import project_icons
        with patch('site_access.public_config',return_value={'enabled':True}), \
             patch.object(project_icons,'discover',new=AsyncMock()) as discover:
            await project_icons.refresh(store.one('SELECT * FROM projects'))
            discover.assert_not_awaited()

    async def test_18_tool_loop_does_not_turn_protection_into_more_model_calls(self):
        from agent_driver_checks import answer
        from agent_driver import run_agent
        llm=AsyncMock(return_value=answer('read_sources',{'urls':['https://example.test/start']}))
        dispatch=AsyncMock(side_effect=safety.ProtectionDetected())
        with self.assertRaises(safety.ProtectionDetected):
            await run_agent('Read requested sources',{},'test-model',llm,dispatch,AsyncMock(return_value={'type':'text','text':'page'}),lambda text:None)
        self.assertEqual(llm.await_count,1);self.assertEqual(dispatch.await_count,1)

    async def test_19_pacing_roundtrips_and_invalid_import_is_rejected(self):
        import transfer
        safety.save_profile('p1','standard')
        path=store.DATA/'project.json';transfer.export_to(path,history=False)
        payload=transfer.read_package(path)
        self.assertEqual(payload['crawl_profiles'],{'p1':'standard'})
        safety.save_profile('p1','gentle')
        transfer.import_package(path,payload,apply_settings=False)
        self.assertEqual(safety.profile('p1'),'standard')
        payload['crawl_profiles']['p1']='unlimited'
        with self.assertRaises(transfer.Invalid):transfer.validate(payload)

    async def test_20_ui_remains_active_and_has_no_extra_retry_countdown(self):
        self.run_record(safety.ProtectionDetected())
        from fastapi.testclient import TestClient
        import web
        client=TestClient(web.app);self.addCleanup(client.close)
        status=client.get('/api/project-status').json()['projects'][0]
        self.assertEqual(status['enabled'],1);self.assertEqual(status['status'],'protection');self.assertIsNone(status['retry'])
        text=client.get('/projects/p1').text
        self.assertIn('reguläre Zeitplan bleibt aktiv',text)
        self.assertIn('Nächster regulärer Versuch',text)
        self.assertNotIn('manuellen Freigabe',text)


if __name__=='__main__':
    unittest.main(verbosity=2)
