"""Offline acceptance checks for scheduling, tags and confirmed project deletion."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Archive acceptance uses the real free-space guard. /tmp is intentionally a
# small container tmpfs, so all isolated cases belong to the writable data mount.
scratch = tempfile.TemporaryDirectory(prefix='.management-check-', dir='/data')
os.environ['DATA_DIR'] = str(Path(scratch.name) / 'bootstrap')
import store
store.CONFIG = Path(scratch.name) / 'bootstrap-config.json'
store.SECRETS = Path(scratch.name) / 'bootstrap-credentials.json'
import engine
import scheduling
import tag_registry
import project_lifecycle
import site_access
import web
from fastapi.testclient import TestClient
from fastapi.responses import HTMLResponse

UTC = timezone.utc

def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=scratch.name)
        self.root = Path(self.directory.name)
        store.DATA = web.DATA = engine.DATA = self.root / 'data'
        store.CONFIG = web.CONFIG = self.root / 'config/settings.json'
        store.SECRETS = web.SECRETS = self.root / 'secrets/credentials.json'
        store.init()
        store.save_json(store.CONFIG, {'global_schedule': dict(scheduling.DEFAULT),
                                      'omlx_url': 'http://192.0.2.2:8000/v1',
                                      'agent_model': 'Synthetic local model'})
        store.save_json(store.SECRETS, {'omlx_key': 'SYNTHETIC-MANAGEMENT-KEY',
                                      'smtp_password': 'SYNTHETIC-SMTP-PASSWORD'})
        store.save_json(site_access.vault_path(), {'version': 1, 'logins': {}})
        # Backend-only checks do not depend on unfinished HTML or start the scheduler.
        self.render_patch = patch('web.render', side_effect=lambda *a, **k: HTMLResponse('<p>isolated backend check</p>'))
        self.render_patch.start()
        # Project creation schedules a favicon refresh. This suite exercises
        # management only; do not make a provider/proxy request from its fixture.
        icon_patch = patch('web.project_icons.refresh', new_callable=AsyncMock)
        icon_patch.start()
        self.addCleanup(icon_patch.stop)
        self.client = TestClient(web.app, follow_redirects=False)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.csrf = self.client.cookies['kiwm_csrf']

    def tearDown(self):
        self.client.close()
        self.render_patch.stop()
        self.directory.cleanup()

    def project(self, pid, *, enabled=1, global_time=0, tags=None, cron='0 6 * * *'):
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,color,tags,use_global_schedule,schedule_config) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (pid, pid.upper(), 'https://www.elektro.net/praxisprobleme/',
                       'Synthetic public monitoring task.', 'www.elektro.net', cron, enabled,
                       '2099-01-01T00:00:00+00:00', store.now(), '#1967d2', json.dumps(tags or []), global_time, '{}'))
        return store.one('SELECT * FROM projects WHERE id=?', (pid,))

    def post(self, path, **data):
        return self.client.post(path, data={'csrf': self.csrf, **data}, headers={'Origin': 'http://testserver'})

    def form(self, pid='', **extra):
        return {'id': pid, 'name': 'Edited project', 'url': 'https://www.elektro.net/praxisprobleme/',
                'prompt': 'Read only the selected public professional content.', 'domains': 'www.elektro.net',
                'enabled': 'on', 'schedule_mode': 'daily', 'schedule_time': '07:30',
                'schedule_weekday': '0', 'schedule_monthday': '1', 'schedule_interval': '6', **extra}

    def private_bytes(self):
        return {p: p.read_bytes() for p in (store.SECRETS, site_access.vault_path())}

    def test_01_daily_is_strictly_future_and_uses_berlin_wall_time(self):
        before = dt('2026-11-05T04:59:00Z')
        first = dt(scheduling.next_for({'mode': 'daily', 'time': '06:00'}, before))
        self.assertEqual(first, dt('2026-11-05T05:00:00Z'))
        self.assertEqual(dt(scheduling.next_for({'mode': 'daily', 'time': '06:00'}, first)),
                         dt('2026-11-06T05:00:00Z'))

    def test_02_weekly_and_weekdays_skip_unselected_days(self):
        friday = dt('2026-11-06T15:00:00Z')
        expected = dt('2026-11-09T07:30:00Z')
        self.assertEqual(dt(scheduling.next_for({'mode': 'weekly', 'weekday': 0, 'time': '08:30'}, friday)), expected)
        self.assertEqual(dt(scheduling.next_for({'mode': 'weekdays', 'time': '08:30'}, friday)), expected)
        sunday = dt(scheduling.next_for({'mode': 'weekly', 'weekday': 6, 'time': '08:30'}, friday))
        self.assertEqual(sunday, dt('2026-11-08T07:30:00Z'))

    def test_03_monthly_31_clamps_to_short_month_including_leap_year(self):
        for year, expected_day in ((2027, 28), (2028, 29)):
            with self.subTest(year=year):
                result = dt(scheduling.next_for({'mode': 'monthly', 'monthday': 31, 'time': '08:00'},
                                                dt(f'{year}-01-31T07:00:00Z'))).astimezone(scheduling.TZ)
                self.assertEqual((result.year, result.month, result.day, result.hour), (year, 2, expected_day, 8))

    def test_04_spring_gap_moves_forward_without_duplicate_day(self):
        config = {'mode': 'daily', 'time': '02:30'}
        first = dt(scheduling.next_for(config, dt('2026-03-29T00:00:00Z')))
        self.assertEqual(first, dt('2026-03-29T01:30:00Z'))
        self.assertEqual(first.astimezone(scheduling.TZ).hour, 3)
        self.assertEqual(dt(scheduling.next_for(config, first)), dt('2026-03-30T00:30:00Z'))

    def test_05_autumn_fold_runs_only_once_on_the_calendar_day(self):
        config = {'mode': 'daily', 'time': '02:30'}
        first = dt(scheduling.next_for(config, dt('2026-10-25T00:00:00Z')))
        self.assertEqual(first, dt('2026-10-25T00:30:00Z'))
        after_first = dt(scheduling.next_for(config, dt('2026-10-25T00:45:00Z')))
        self.assertEqual(after_first, dt('2026-10-26T01:30:00Z'))

    def test_06_five_hour_intervals_keep_phase_across_midnight_and_late_runs(self):
        config = {'mode': 'interval', 'interval': 5}
        anchor = dt('2026-11-04T22:00:00Z')
        self.assertEqual(dt(scheduling.next_for(config, dt('2026-11-05T00:00:00Z'), anchor)), dt('2026-11-05T03:00:00Z'))
        self.assertEqual(dt(scheduling.next_for(config, dt('2026-11-05T04:00:00Z'), anchor)), dt('2026-11-05T08:00:00Z'))
        project = {'cron': '0 */5 * * *', 'use_global_schedule': 0,
                   'schedule_config': json.dumps(scheduling.validate(config)), 'next_due': anchor.isoformat()}
        self.assertEqual(dt(scheduling.next_project_due(project, dt('2026-11-05T00:00:00Z'), advancing=True)), dt('2026-11-05T03:00:00Z'))

    def test_07_168_hour_interval_is_elapsed_utc_even_across_dst(self):
        anchor = dt('2026-10-23T08:00:00Z')
        result = dt(scheduling.next_for({'mode': 'interval', 'interval': 168}, anchor, anchor))
        self.assertEqual(result-anchor, timedelta(hours=168))
        self.assertEqual(result, dt('2026-10-30T08:00:00Z'))
        self.assertNotEqual(anchor.astimezone(scheduling.TZ).hour, result.astimezone(scheduling.TZ).hour)

    def test_08_invalid_schedule_fields_are_bounded_and_no_naive_clock(self):
        invalid = [{'mode': 'unsupported'}, {'time': '24:00'}, {'time': '6:00'}, {'interval': 0},
                   {'interval': 169}, {'interval': True}, {'weekday': 7}, {'monthday': 0},
                   {'monthday': 32}, {'cron': 'arbitrary'}, None]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): scheduling.validate(value)
        with self.assertRaises(ValueError): scheduling.next_for({}, datetime(2026, 11, 5))
        with self.assertRaises(ValueError): scheduling.from_form({'schedule_interval': 'not-a-number'})

    def test_09_legacy_cron_is_retained_until_explicit_schedule_edit(self):
        project = {'cron': '0 6 * * 1,3', 'use_global_schedule': 0, 'schedule_config': '{}'}
        self.assertEqual(scheduling.project_schedule(project)['mode'], 'custom')
        after = dt('2026-11-05T00:00:00Z')
        with patch('scheduling.store.next_due', return_value='legacy-next') as legacy:
            self.assertEqual(scheduling.next_project_due(project, after), 'legacy-next')
            legacy.assert_called_once_with(project['cron'], after)
        self.assertEqual(scheduling.label(project['cron']), 'Bisheriger Zeitplan')

    def test_10_global_schedule_changes_only_opt_in_projects(self):
        self.project('opt-in', global_time=1)
        self.project('own-active', cron='15 7 * * 1')
        self.project('own-disabled', enabled=0, cron='30 9 1 * *')
        before = {p['id']: dict(p) for p in store.rows('SELECT * FROM projects')}
        private = self.private_bytes()
        response = self.post('/settings/schedule', schedule_mode='weekly', schedule_time='08:45', schedule_weekday='2')
        self.assertEqual(response.status_code, 303)
        current = {p['id']: dict(p) for p in store.rows('SELECT * FROM projects')}
        self.assertNotEqual(current['opt-in']['next_due'], before['opt-in']['next_due'])
        local = dt(current['opt-in']['next_due']).astimezone(scheduling.TZ)
        self.assertEqual((local.weekday(), local.hour, local.minute), (2, 8, 45))
        for pid in ('own-active', 'own-disabled'): self.assertEqual(current[pid], before[pid])
        self.assertEqual(current['opt-in']['enabled'], 1)
        self.assertEqual(store.settings()['omlx_url'], 'http://192.0.2.2:8000/v1')
        self.assertEqual(self.private_bytes(), private)

    def test_11_apply_all_uses_one_global_time_without_activating_projects(self):
        self.project('active', enabled=1)
        self.project('paused', enabled=0)
        private = self.private_bytes()
        before = {p['id']: dict(p) for p in store.rows('SELECT * FROM projects')}
        self.assertEqual(self.post('/settings/schedule', schedule_mode='interval', schedule_interval='5', apply_all='on').status_code, 303)
        current = store.rows('SELECT * FROM projects')
        self.assertEqual(len({p['next_due'] for p in current}), 1)
        for p in current:
            self.assertEqual(p['use_global_schedule'], 1)
            for field in ('enabled', 'cron', 'name', 'prompt', 'color', 'tags'):
                self.assertEqual(p[field], before[p['id']][field])
        self.assertEqual(scheduling.global_schedule()['interval'], 5)
        self.assertEqual(self.private_bytes(), private)

    def test_12_global_route_rejects_bad_fields_csrf_and_foreign_origin(self):
        self.project('unchanged', global_time=1)
        before = store.CONFIG.read_bytes(), store.rows('SELECT * FROM projects')
        self.assertEqual(self.post('/settings/schedule', schedule_time='99:99').status_code, 400)
        self.assertEqual(self.post('/settings/schedule', csrf='invalid', schedule_mode='daily').status_code, 403)
        response = self.client.post('/settings/schedule', data={'csrf': self.csrf, 'schedule_mode': 'daily'}, headers={'Origin': 'https://elsewhere.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((store.CONFIG.read_bytes(), store.rows('SELECT * FROM projects')), before)

    def test_13_old_metadata_migrates_to_stable_tag_ids_without_resetting_color(self):
        legacy = self.root / 'legacy'; legacy.mkdir()
        store.DATA = web.DATA = engine.DATA = legacy
        with sqlite3.connect(legacy/'monitor.db') as c:
            c.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,prompt TEXT NOT NULL,domains TEXT NOT NULL,cron TEXT NOT NULL,enabled INTEGER NOT NULL,next_due TEXT NOT NULL,last_success TEXT,created TEXT NOT NULL,color TEXT NOT NULL,tags TEXT NOT NULL)')
            c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      ('old', 'Unchanged alias', 'https://example.org/', 'Unchanged task', 'example.org', '17 6 * * *', 0,
                       '2099-01-01T00:00:00+00:00', None, '2026-01-01', '#7c3aed', json.dumps(['Arbeit','Privat'])))
        store.init(); tags = tag_registry.all_tags()
        self.assertEqual({t['name'] for t in tags}, {'Arbeit','Privat'})
        row = store.one('SELECT * FROM projects WHERE id=?', ('old',))
        self.assertEqual((row['name'], row['prompt'], row['cron'], row['enabled'], row['color'], row['use_global_schedule']),
                         ('Unchanged alias', 'Unchanged task', '17 6 * * *', 0, '#7c3aed', 0))
        ident = next(t['id'] for t in tags if t['name']=='Arbeit')
        tag_registry.save('Arbeit', '#123abc', ident)
        store.init()
        current = {t['id']: t for t in tag_registry.all_tags()}
        self.assertEqual(set(current), {t['id'] for t in tags})
        self.assertEqual(current[ident]['color'], '#123abc')

    def test_14_tag_rename_propagates_case_insensitively_and_keeps_identity(self):
        self.project('alpha', tags=['Arbeit','Privat']);self.project('beta', tags=['arbeit'])
        store.init(); tag = next(t for t in tag_registry.all_tags() if t['name']=='Arbeit')
        self.assertEqual(tag_registry.save('Beruf', '#AABBCC', tag['id']), tag['id'])
        self.assertEqual(json.loads(store.one('SELECT tags FROM projects WHERE id=?', ('alpha',))['tags']), ['Beruf','Privat'])
        self.assertEqual(json.loads(store.one('SELECT tags FROM projects WHERE id=?', ('beta',))['tags']), ['Beruf'])
        self.assertEqual(next(t for t in tag_registry.all_tags() if t['id']==tag['id'])['color'], '#aabbcc')
        with self.assertRaises(ValueError): tag_registry.save('BERUF', '#ffffff')

    def test_15_deleting_tag_removes_assignments_but_preserves_projects(self):
        self.project('alpha', tags=['Arbeit','Privat']);self.project('beta', enabled=0, tags=['Arbeit'])
        store.init(); ident = next(t['id'] for t in tag_registry.all_tags() if t['name']=='Arbeit')
        self.assertEqual(self.post('/tags/'+ident+'/delete').status_code, 400)
        self.assertEqual(self.post('/tags/'+ident+'/delete', confirm='on').status_code, 303)
        self.assertEqual(json.loads(store.one('SELECT tags FROM projects WHERE id=?', ('alpha',))['tags']), ['Privat'])
        self.assertEqual(json.loads(store.one('SELECT tags FROM projects WHERE id=?', ('beta',))['tags']), [])
        self.assertEqual(store.one('SELECT enabled FROM projects WHERE id=?', ('beta',))['enabled'], 0)
        self.assertEqual(store.one('SELECT count(*) n FROM projects')['n'], 2)

    def test_16_tag_validation_rejects_unknown_assignment_hex_injection_and_csrf(self):
        ident = tag_registry.save('Arbeit', '#1967d2')
        self.assertEqual(self.post('/tags/save', name='Bad', color='red;background:url(https://invalid)').status_code, 400)
        self.assertEqual(self.post('/tags/save', name='No token', color='#ffffff', csrf='invalid').status_code, 403)
        self.assertEqual(len(tag_registry.all_tags()), 1)
        self.assertEqual(self.post('/projects/save', **self.form(tag_ids=['unknown'], tags_present='1')).status_code, 400)
        self.assertEqual(self.post('/projects/save', **self.form(tags='Never globally defined')).status_code, 400)
        self.assertEqual(store.one('SELECT count(*) n FROM projects')['n'], 0)
        self.assertEqual(tag_registry.assign([ident]), ['Arbeit'])

    def test_17_project_dropdown_assignments_clear_and_friendly_schedule_survives(self):
        ident = tag_registry.save('Arbeit', '#1967d2')
        response = self.post('/projects/save', **self.form(tag_ids=[ident], tags_present='1', schedule_mode='monthly', schedule_monthday='31', schedule_time='09:15'))
        self.assertEqual(response.status_code, 303)
        pid = response.headers['location'].split('?')[0].rsplit('/',1)[-1]
        project = store.one('SELECT * FROM projects WHERE id=?', (pid,))
        self.assertEqual(json.loads(project['tags']), ['Arbeit'])
        self.assertEqual(scheduling.project_schedule(project)['monthday'], 31)
        self.assertEqual(self.post('/projects/save', **self.form(pid, tags_present='1', use_global_schedule='on')).status_code, 303)
        project = store.one('SELECT * FROM projects WHERE id=?', (pid,))
        self.assertEqual(json.loads(project['tags']), [])
        self.assertEqual(project['use_global_schedule'], 1)
        self.assertEqual(scheduling.project_schedule(project), scheduling.global_schedule())

    def test_18_project_delete_requires_confirmation_and_blocks_active_target(self):
        self.project('alpha')
        self.assertEqual(self.post('/projects/alpha/delete').status_code, 400)
        self.assertEqual(self.post('/projects/alpha/delete', confirm='on', csrf='invalid').status_code, 403)
        for status in ('queued','running'):
            with self.subTest(status=status):
                store.execute('DELETE FROM runs')
                store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)', ('active','alpha',store.now(),status,'test'))
                self.assertEqual(self.post('/projects/alpha/delete', confirm='on').status_code, 409)
                self.assertIsNotNone(store.one('SELECT id FROM projects WHERE id=?', ('alpha',)))
        store.execute('DELETE FROM runs')
        self.assertEqual(self.post('/projects/alpha/delete', confirm='on').status_code, 303)
        self.assertIsNone(store.one('SELECT id FROM projects WHERE id=?', ('alpha',)))

    def seed_lifecycle(self):
        for pid in ('alpha','beta'):
            self.project(pid)
            site_access.save(pid, True, '987654321' if pid=='alpha' else '876543219', '54321')
            for phase in ('old','new'):
                rid = pid+'-'+phase
                store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)', (rid,pid,store.now(),store.now(),'success','test'))
                store.event(rid, 'Synthetic archived event')
                folder = store.DATA/'artifacts'/rid;folder.mkdir(parents=True,exist_ok=True)
                (folder/'evidence.json').write_text('{"synthetic":true}')
                (folder/'nested').mkdir();(folder/'nested/trace.txt').write_text('Synthetic trace')
            engine.archive({'id':pid},pid+'-old','https://www.elektro.net/shared.txt','document',b'Shared archive object','Shared archive object',suffix='.txt')
            for phase,text in (('old',pid+' 30'),('new',pid+' 31')):
                engine.archive({'id':pid},pid+'-'+phase,'https://www.elektro.net/'+pid+'.txt','document',text.encode(),text,suffix='.txt')
            store.execute('INSERT INTO state VALUES(?,?)', ('content_scopes:'+pid,json.dumps({'https://www.elektro.net/':'#answer'})))
        stamp = store.now()
        store.execute('INSERT INTO reports(id,period_key,created,start,end,content,mail_status) VALUES(?,?,?,?,?,?,?)', ('shared-report','test-shared',stamp,stamp,stamp,'Synthetic shared archived report','sent'))
        (store.DATA/'reports/shared-report.txt').write_text('Synthetic shared archived report')
        for pid in ('alpha','beta'):
            change=store.one('SELECT id FROM changes WHERE project_id=? AND run_id=?',(pid,pid+'-new'))['id']
            store.execute('INSERT INTO report_changes VALUES(?,?)',(change,'shared-report'))
            store.execute('INSERT INTO change_reviews VALUES(?,?)',(change,stamp))

    def test_19_delete_cleans_private_and_owned_history_but_keeps_shared_objects_reports(self):
        self.seed_lifecycle()
        beta_before={table:store.rows('SELECT * FROM '+table+' WHERE '+('project_id=?' if table in ('runs','snapshots','changes') else 'id=?'),('beta',)) for table in ('runs','snapshots','changes')}
        alpha_files={r[k] for r in store.rows('SELECT * FROM snapshots WHERE project_id=?',('alpha',)) for k in ('file','screenshot','html') if r[k]}
        beta_files={r[k] for r in store.rows('SELECT * FROM snapshots WHERE project_id=?',('beta',)) for k in ('file','screenshot','html') if r[k]}
        api_before=store.SECRETS.read_bytes();beta_secret=site_access.get_secret('beta')
        self.assertEqual(self.post('/projects/alpha/delete',confirm='on').status_code,303)
        self.assertIsNone(store.one('SELECT * FROM projects WHERE id=?',('alpha',)))
        for table in ('runs','snapshots','changes'):self.assertEqual(store.rows('SELECT * FROM '+table+' WHERE project_id=?',('alpha',)),[])
        self.assertEqual(store.rows('SELECT * FROM events WHERE run_id LIKE ?',('alpha-%',)),[])
        self.assertEqual(store.rows('SELECT * FROM state WHERE key IN (?,?)',('site_auth:alpha','content_scopes:alpha')),[])
        for table,expected in beta_before.items():self.assertEqual(store.rows('SELECT * FROM '+table+' WHERE project_id=?',('beta',)),expected)
        for name in alpha_files-beta_files:self.assertFalse((store.DATA/name).exists(),name)
        for name in beta_files:self.assertTrue((store.DATA/name).is_file(),name)
        for phase in ('old','new'):self.assertFalse((store.DATA/'artifacts'/('alpha-'+phase)).exists())
        self.assertTrue((store.DATA/'artifacts/beta-new/evidence.json').is_file())
        self.assertEqual(site_access.get_secret('beta'),beta_secret)
        self.assertNotIn('alpha',json.loads(site_access.vault_path().read_text())['logins'])
        self.assertEqual(store.SECRETS.read_bytes(),api_before)
        self.assertIsNotNone(store.one('SELECT * FROM reports WHERE id=?',('shared-report',)))
        self.assertTrue((store.DATA/'reports/shared-report.txt').is_file())
        self.assertEqual(store.one('SELECT count(*) n FROM report_changes')['n'],1)
        self.assertEqual(store.one('SELECT count(*) n FROM change_reviews')['n'],1)
        with store.db() as c:self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())

    def test_20_vault_write_failure_leaves_project_database_and_files_unchanged(self):
        self.seed_lifecycle()
        before={table:store.rows('SELECT * FROM '+table) for table in ('projects','runs','snapshots','changes','events','state','reports','report_changes','change_reviews')}
        private=self.private_bytes()
        files={p.relative_to(store.DATA):p.read_bytes() for p in (store.DATA/'artifacts').rglob('*') if p.is_file()}
        with patch('site_access.remove_secret',side_effect=site_access.StorageError('Synthetic write failure.')):
            with self.assertRaises(site_access.StorageError):project_lifecycle.remove('alpha')
        self.assertEqual({table:store.rows('SELECT * FROM '+table) for table in before},before)
        self.assertEqual(self.private_bytes(),private)
        self.assertEqual({p.relative_to(store.DATA):p.read_bytes() for p in (store.DATA/'artifacts').rglob('*') if p.is_file()},files)
        # A later database failure must restore a vault entry already removed,
        # not merely handle a failure before the private write took place.
        store.execute("CREATE TRIGGER fail_project_delete BEFORE DELETE ON projects WHEN OLD.id='alpha' BEGIN SELECT RAISE(ABORT,'Synthetic database write failure.'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            project_lifecycle.remove('alpha')
        self.assertEqual({table:store.rows('SELECT * FROM '+table) for table in before},before)
        self.assertEqual(self.private_bytes(),private)
        self.assertEqual({p.relative_to(store.DATA):p.read_bytes() for p in (store.DATA/'artifacts').rglob('*') if p.is_file()},files)


if __name__ == '__main__':
    unittest.main(verbosity=2)
