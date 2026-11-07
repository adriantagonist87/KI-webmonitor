"""Deterministic retry-controller checks; no browser, network or inference."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import store
import retry_jobs as retries


class RetryChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.t0 = datetime(2027, 1, 5, 10, 0, tzinfo=timezone.utc)
        self.clock = self.t0
        self.patches = [patch.object(store, 'DATA', root / 'data'),
                        patch.object(store, 'CONFIG', root / 'settings.json'),
                        patch.object(store, 'SECRETS', root / 'credentials.json'),
                        patch.object(store, 'now', lambda: self.clock.isoformat())]
        for p in self.patches: p.start()
        store.init()
        self.project('p')

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.folder.cleanup()

    def project(self, pid, enabled=1):
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pid, pid, 'https://example.org/', 'Read only', 'example.org', '0 6 * * *', enabled,
                       '2027-01-06T05:00:00+00:00', self.t0.isoformat()))

    def fail(self, pid='p', status='failed'):
        rid = store.enqueue(pid)
        self.assertTrue(retries.begin_attempt(rid))
        self.assertTrue(retries.finish_run(rid, status, finished=self.clock, error='Controlled test failure'))
        return rid

    def plan(self, source):
        return store.one('SELECT * FROM run_retries WHERE source_run_id=?', (source,))

    def child(self, parent):
        self.clock = self.t0 + retries.DELAY
        created = retries.sweep_due(self.clock)
        self.assertEqual(len(created), 1)
        self.assertEqual(self.plan(parent)['retry_run_id'], created[0])
        return created[0]

    def test_failure_has_exact_30_minute_deadline_and_no_early_claim(self):
        parent = self.fail()
        self.assertEqual(self.plan(parent)['due_at'], (self.t0 + timedelta(minutes=30)).isoformat())
        self.assertEqual(retries.sweep_due(self.t0 + retries.DELAY - timedelta(microseconds=1)), [])
        self.assertEqual(self.plan(parent)['state'], 'pending')

    def test_retry_failure_is_terminal_and_is_not_retried_again(self):
        parent = self.fail(); child = self.child(parent)
        self.assertTrue(retries.begin_attempt(child))
        self.clock += timedelta(seconds=5)
        retries.finish_run(child, 'failed', finished=self.clock, error='Controlled second failure')
        self.assertEqual(self.plan(parent)['state'], 'finished')
        self.assertEqual(store.rows('SELECT source_run_id FROM run_retries'), [{'source_run_id': parent}])
        self.assertEqual(retries.sweep_due(self.clock + timedelta(days=3)), [])
        self.assertIsNone(retries.project_retry('p'))

    def test_partial_run_gets_one_retry(self):
        parent = self.fail(status='partial')
        self.assertEqual(self.plan(parent)['state'], 'pending')
        child = self.child(parent); retries.begin_attempt(child)
        retries.finish_run(child, 'partial', finished=self.clock)
        self.assertEqual(retries.sweep_due(self.clock + timedelta(hours=2)), [])

    def test_success_never_creates_retry_and_persists_last_success(self):
        rid = store.enqueue('p'); retries.begin_attempt(rid)
        retries.finish_run(rid, 'success', finished=self.clock, summary='No change', agent_result='{}')
        self.assertFalse(store.rows('SELECT * FROM run_retries'))
        self.assertEqual(store.one('SELECT last_success FROM projects WHERE id=?', ('p',))['last_success'], self.clock.isoformat())

    def test_manual_failure_of_paused_project_does_not_start_automation(self):
        store.execute('UPDATE projects SET enabled=0 WHERE id=?', ('p',))
        parent = self.fail()
        self.assertIsNone(self.plan(parent)); self.assertIsNone(retries.project_retry('p'))

    def test_pause_cancels_pending_and_resume_does_not_resurrect(self):
        parent = self.fail()
        store.execute('UPDATE projects SET enabled=0 WHERE id=?', ('p',))
        self.assertIsNone(retries.project_retry('p'))
        self.assertEqual(retries.sweep_due(self.t0 + timedelta(hours=1), can_queue=False), [])
        self.assertEqual(self.plan(parent)['state'], 'cancelled')
        store.execute('UPDATE projects SET enabled=1 WHERE id=?', ('p',))
        self.assertEqual(retries.sweep_due(self.t0 + timedelta(hours=2)), [])

    def test_manual_new_run_supersedes_pending(self):
        parent = self.fail(); self.clock += timedelta(minutes=3)
        new = store.enqueue('p', 'manual')
        self.assertTrue(new); self.assertEqual(self.plan(parent)['state'], 'cancelled')
        self.assertEqual(retries.sweep_due(self.t0 + timedelta(hours=1)), [])
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (parent,))['status'], 'failed')

    def test_regular_schedule_supersedes_pending(self):
        parent = self.fail(); self.clock += timedelta(minutes=10)
        self.assertTrue(store.enqueue('p', 'scheduled'))
        self.assertEqual(self.plan(parent)['state'], 'cancelled')

    def test_manual_new_run_cancels_never_started_queued_retry(self):
        parent = self.fail(); child = self.child(parent)
        self.clock += timedelta(seconds=1)
        new = store.enqueue('p')
        self.assertTrue(new); self.assertFalse(retries.begin_attempt(child))
        row = store.one('SELECT status,error,finished FROM runs WHERE id=?', (child,))
        self.assertEqual(row['status'], 'cancelled'); self.assertEqual(row['error'], retries.CANCEL_REASON)
        self.assertIsNotNone(row['finished']); self.assertEqual(self.plan(parent)['state'], 'cancelled')

    def test_pause_after_claim_is_checked_atomically_before_browser_start(self):
        parent = self.fail(); child = self.child(parent)
        store.execute('UPDATE projects SET enabled=0 WHERE id=?', ('p',))
        self.assertFalse(retries.begin_attempt(child))
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (child,))['status'], 'cancelled')

    def test_pause_in_existing_transaction_cancels_queued_retry(self):
        parent = self.fail(); child = self.child(parent)
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE projects SET enabled=0 WHERE id=?', ('p',))
            retries.cancel_project('p', connection=c, stamp=self.clock)
        self.assertEqual(self.plan(parent)['state'], 'cancelled')
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (child,))['status'], 'cancelled')

    def test_pause_does_not_rewrite_an_already_running_retry(self):
        parent = self.fail(); child = self.child(parent); self.assertTrue(retries.begin_attempt(child))
        store.execute('UPDATE projects SET enabled=0 WHERE id=?', ('p',)); retries.cancel_project('p')
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (child,))['status'], 'running')
        self.assertEqual(self.plan(parent)['state'], 'running')

    def test_manual_browser_mode_delays_claim_without_resetting_deadline(self):
        parent = self.fail(); due = self.plan(parent)['due_at']
        store.execute("UPDATE state SET value='1' WHERE key='manual'")
        self.assertEqual(retries.sweep_due(self.t0 + timedelta(hours=2)), [])
        self.assertEqual(retries.project_retry('p')['due_at'], due)
        store.execute("UPDATE state SET value='0' WHERE key='manual'")
        self.assertEqual(len(retries.sweep_due(self.t0 + timedelta(hours=3))), 1)
        self.assertEqual(self.plan(parent)['due_at'], due)

    def test_busy_browser_delays_claim_and_preserves_countdown_deadline(self):
        parent = self.fail(); due = self.plan(parent)['due_at']
        self.assertEqual(retries.sweep_due(self.t0 + timedelta(hours=1), can_queue=False), [])
        self.assertEqual(retries.project_retry('p')['state'], 'pending')
        self.assertEqual(self.plan(parent)['due_at'], due)
        self.assertEqual(len(retries.sweep_due(self.t0 + timedelta(hours=1))), 1)

    def test_manual_mode_race_after_claim_leaves_one_queued_retry(self):
        parent = self.fail(); child = self.child(parent)
        store.execute("UPDATE state SET value='1' WHERE key='manual'")
        self.assertFalse(retries.begin_attempt(child))
        self.assertEqual(self.plan(parent)['state'], 'queued')
        store.execute("UPDATE state SET value='0' WHERE key='manual'")
        self.assertTrue(retries.begin_attempt(child))

    def test_restart_preserves_existing_pending_deadline(self):
        parent = self.fail(); due = self.plan(parent)['due_at']
        self.clock += timedelta(minutes=12); store.init()
        self.assertEqual(self.plan(parent)['due_at'], due)
        self.assertEqual(retries.sweep_due(self.clock), [])

    def test_only_newly_interrupted_running_job_is_registered_on_startup(self):
        old = self.fail(); retries.cancel_project('p')
        self.project('q'); self.clock += timedelta(minutes=2)
        active = store.enqueue('q'); retries.begin_attempt(active)
        self.clock += timedelta(minutes=7); store.init()
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (active,))['status'], 'failed')
        self.assertEqual(self.plan(active)['due_at'], (self.clock + retries.DELAY).isoformat())
        self.assertEqual(self.plan(old)['state'], 'cancelled')

    def test_restart_does_not_backfill_historical_failures(self):
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      ('historical', 'p', self.t0.isoformat(), self.t0.isoformat(), 'failed', 'manual'))
        store.init(); self.assertFalse(store.rows('SELECT * FROM run_retries'))

    def test_restart_of_running_retry_is_terminal(self):
        parent = self.fail(); child = self.child(parent); retries.begin_attempt(child)
        self.clock += timedelta(minutes=1); store.init()
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (child,))['status'], 'failed')
        self.assertEqual(self.plan(parent)['state'], 'finished')
        self.assertEqual(retries.sweep_due(self.clock + timedelta(days=1)), [])

    def test_restart_of_queued_retry_does_not_claim_again(self):
        parent = self.fail(); child = self.child(parent); store.init()
        self.assertEqual(retries.sweep_due(self.clock + timedelta(hours=1)), [])
        self.assertEqual(retries.project_retry('p')['retry_run_id'], child)
        self.assertTrue(retries.begin_attempt(child)); self.assertFalse(retries.begin_attempt(child))

    def test_concurrent_scheduler_claims_create_exactly_one_child(self):
        parent = self.fail(); self.clock += retries.DELAY
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: retries.sweep_due(self.clock), range(2)))
        self.assertEqual(sum(len(x) for x in results), 1)
        self.assertEqual(len(store.rows("SELECT id FROM runs WHERE origin='retry'")), 1)
        self.assertEqual(self.plan(parent)['state'], 'queued')

    def test_concurrent_manual_enqueues_create_one_normal_job(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: store.enqueue('p'), range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_duplicate_terminal_callback_never_resets_deadline(self):
        parent = self.fail(); before = self.plan(parent)
        self.assertFalse(retries.finish_run(parent, 'failed', finished=self.t0 + timedelta(minutes=9)))
        retries.register_result(parent)
        self.assertEqual(self.plan(parent), before)

    def test_status_and_retry_plan_rollback_together_on_write_failure(self):
        rid = store.enqueue('p'); retries.begin_attempt(rid)
        with patch.object(retries, '_register_result', side_effect=RuntimeError('controlled commit failure')):
            with self.assertRaises(RuntimeError): retries.finish_run(rid, 'failed', finished=self.t0)
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (rid,))['status'], 'running')
        self.assertIsNone(self.plan(rid))

    def test_deleted_source_cascades_plan_and_orphan_child_cannot_start(self):
        parent = self.fail(); child = self.child(parent)
        store.execute('DELETE FROM runs WHERE id=?', (parent,))
        self.assertIsNone(self.plan(parent)); self.assertFalse(retries.begin_attempt(child))
        self.assertEqual(store.one('SELECT status FROM runs WHERE id=?', (child,))['status'], 'cancelled')

    def test_new_normal_failure_after_exhausted_retry_has_its_own_single_retry(self):
        parent = self.fail(); child = self.child(parent); retries.begin_attempt(child)
        retries.finish_run(child, 'failed', finished=self.clock)
        self.clock += timedelta(minutes=1); fresh = self.fail()
        self.assertNotEqual(fresh, parent); self.assertEqual(self.plan(parent)['state'], 'finished')
        self.assertEqual(self.plan(fresh)['state'], 'pending')
        self.assertEqual(len(store.rows('SELECT * FROM run_retries')), 2)

    def test_utc_deadline_is_elapsed_time_across_dst_fallback(self):
        self.clock = datetime.fromisoformat('2026-10-25T02:45:00+02:00')
        parent = self.fail()
        self.assertEqual(self.plan(parent)['due_at'], '2026-10-25T01:15:00+00:00')

    def test_public_display_is_bound_to_actual_latest_attempt(self):
        parent = self.fail()
        self.assertEqual(retries.project_retry('p', parent)['state'], 'pending')
        self.assertIsNone(retries.project_retry('p', 'foreign'))
        child = self.child(parent)
        self.assertIsNone(retries.project_retry('p', parent))
        self.assertEqual(retries.project_retry('p', child)['state'], 'queued')
        retries.begin_attempt(child); self.assertEqual(retries.project_retry('p', child)['state'], 'running')

    def test_untracked_retry_origin_cannot_be_enqueued_directly(self):
        self.assertIsNone(store.enqueue('p', 'retry'))
        self.assertFalse(store.rows('SELECT id FROM runs'))

    def test_naive_timestamps_are_not_accepted_as_exact_deadlines(self):
        with self.assertRaises(ValueError): retries.sweep_due(datetime(2027, 1, 5, 10))


if __name__ == '__main__':
    unittest.main()
