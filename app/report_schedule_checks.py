"""SMTP recurrence, coverage and migration using isolated settings and archives."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

import engine
import report_batches
import report_schedule
import store
import transfer
import web


def at(value):
    return datetime.fromisoformat(value + '+00:00')


class ReportScheduleChecks(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(dir=store.DATA)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        for name, value in (('DATA', self.root), ('CONFIG', self.root/'settings.json'), ('SECRETS', self.root/'credentials.json')):
            for module in (store, web):
                override = patch.object(module, name, value)
                override.start()
                self.addCleanup(override.stop)
        store.init()

    def configure(self, schedule, period=None):
        values = {'report_schedule': schedule}
        if period is not None:
            values['report_period'] = period
        store.save_json(store.CONFIG, values)

    def test_default_and_legacy_pending_occurrence_keep_the_original_slot(self):
        initial = at('2026-11-08T10:00:00')
        self.assertEqual(report_schedule.current(initial)['due'], '2026-11-09T07:00:00+00:00')
        self.assertEqual(report_schedule.current(initial)['period'], {'amount': 7, 'unit': 'days'})
        store.execute("INSERT INTO state VALUES('weekly_due',?)", ('2026-11-02T07:00:00+00:00',))
        migrated = report_schedule.current(initial, persist=True)
        self.assertEqual(migrated['due'], '2026-11-02T07:00:00+00:00')
        self.assertEqual(report_schedule.occurrence_key(migrated, initial), 'week-2026-45')
        report_schedule.advance(migrated, initial)
        self.assertEqual(report_schedule.current(initial)['due'], '2026-11-09T07:00:00+00:00')

    def test_calendar_schedules_use_berlin_time_and_short_months(self):
        for options, after, expected in (
            ({'mode':'daily','time':'17:42'}, '2026-07-13T10:00:00', '2026-07-13T15:42:00+00:00'),
            ({'mode':'weekly','weekday':3,'time':'09:15'}, '2026-11-09T10:00:00', '2026-11-12T08:15:00+00:00'),
            ({'mode':'weekdays','time':'07:30'}, '2026-11-13T10:00:00', '2026-11-16T06:30:00+00:00'),
            ({'mode':'monthly','monthday':31,'time':'21:12'}, '2026-11-01T10:00:00', '2026-11-30T20:12:00+00:00'),
        ):
            with self.subTest(options=options):
                self.configure(options)
                self.assertEqual(report_schedule.current(at(after))['due'], expected)

    def test_interval_starts_at_selected_time_and_keeps_elapsed_hour_anchor(self):
        self.configure({'mode':'interval','time':'09:15','interval':5})
        plan = report_schedule.current(at('2026-03-28T07:00:00'), persist=True)
        self.assertEqual(plan['due'], '2026-03-28T08:15:00+00:00')
        report_schedule.advance(plan, at('2026-03-29T02:00:00'))
        self.assertEqual(report_schedule.current()['due'], '2026-03-29T04:15:00+00:00')

    def test_changing_coverage_keeps_the_cursor_but_changing_time_replaces_it(self):
        initial = at('2026-11-09T10:00:00')
        self.configure({'mode':'interval','time':'14:15','interval':4})
        original = report_schedule.current(initial, persist=True)
        self.configure({'mode':'interval','time':'14:15','interval':4}, {'amount':12,'unit':'hours'})
        self.assertEqual(report_schedule.current(initial, persist=True)['due'], original['due'])
        self.configure({'mode':'daily','time':'17:20'})
        changed = report_schedule.current(initial, persist=True)
        report_schedule.advance(original, initial)
        self.assertEqual(report_schedule.current(initial), changed)
        self.assertEqual(changed['due'], '2026-11-09T16:20:00+00:00')

    def test_smtp_form_saves_both_settings_and_preserves_connection_settings(self):
        store.save_json(store.CONFIG, {'smtp_host':'smtp.example.test','smtp_enabled':True})
        client = TestClient(web.app)
        page = BeautifulSoup(client.get('/smtp').text,'html.parser')
        form = page.select_one('form[action="/settings/smtp/schedule"]')
        self.assertIn('interval', form.select_one('[name=schedule_time]').parent['data-schedule-when'].split())
        values = {'csrf':client.cookies['kiwm_csrf'],'schedule_mode':'weekly','schedule_weekday':'3',
                  'schedule_time':'16:35','period_amount':'12','period_unit':'hours'}
        self.assertEqual(client.post(form['action'],data={**values,'csrf':'bad'}).status_code,403)
        self.assertEqual(client.post(form['action'],data={**values,'period_amount':'0'}).status_code,400)
        self.assertEqual(client.post(form['action'],data=values,follow_redirects=False).status_code,303)
        config=store.persisted_settings()
        self.assertEqual(config['smtp_host'],'smtp.example.test')
        self.assertTrue(config['smtp_enabled'])
        self.assertEqual(config['report_period'],{'amount':12,'unit':'hours'})
        self.assertIn('Donnerstag, 16:35 Uhr',client.get('/reports').text)
        archive=self.root/'settings.json.zip'
        transfer.export_to(archive, history=True)
        self.assertEqual(transfer.read_package(archive)['settings']['report_schedule'],config['report_schedule'])

    def test_dispatch_happens_at_due_and_advances_once(self):
        self.configure({'mode':'weekly','time':'09:40','weekday':2})
        when=at('2026-11-11T08:39:59')
        with patch.object(engine,'make_report',AsyncMock(return_value='report')) as make, \
             patch.object(report_schedule,'advance',wraps=report_schedule.advance) as advance:
            asyncio.run(engine.scheduled_report(when))
            make.assert_not_awaited()
            asyncio.run(engine.scheduled_report(when+timedelta(seconds=1)))
            make.assert_awaited_once()
            self.assertEqual(make.call_args.kwargs['occurrence']['due'],'2026-11-11T08:40:00+00:00')
            advance.assert_called_once()
            self.assertGreater(report_schedule.current()['due'],'2026-11-11T08:40:00+00:00')

    def test_same_week_can_contain_multiple_reports_but_each_occurrence_is_idempotent(self):
        self.configure({'mode':'daily','time':'17:20'},{'amount':30,'unit':'hours'})
        first_plan=report_schedule.current(at('2026-11-09T10:00:00'),persist=True)
        with patch.object(report_batches,'dispatch',AsyncMock()):
            first=asyncio.run(engine.make_report(occurrence=first_plan))
            self.assertEqual(asyncio.run(engine.make_report(occurrence=first_plan)),first)
            report_schedule.advance(first_plan,at('2026-11-09T16:20:00'))
            self.assertIsNone(asyncio.run(engine.make_report(occurrence=first_plan)))
            second=asyncio.run(engine.make_report(occurrence=report_schedule.current()))
            preview=asyncio.run(engine.make_report(test=True))
        self.assertNotEqual(first,second)
        reports=store.rows('SELECT id,period_key,start,end FROM reports ORDER BY period_key')
        self.assertEqual({r['id'] for r in reports},{first,second,preview})
        for report in reports:
            self.assertEqual(datetime.fromisoformat(report['end'])-datetime.fromisoformat(report['start']),timedelta(hours=30))
