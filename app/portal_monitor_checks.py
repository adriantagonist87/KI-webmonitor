"""Real Brave/proxy/monitor E2E; private DB, local review fake, no external model."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from playwright.async_api import async_playwright

scratch = tempfile.TemporaryDirectory(prefix='.portal-monitor-check-', dir='/data')
os.environ['DATA_DIR'] = scratch.name
import store
store.CONFIG = Path(scratch.name) / 'settings.json'
store.SECRETS = Path(scratch.name) / 'credentials.json'
import engine
from relevance_test_model import review_only_model,assert_final_review

ORIGIN = 'http://fixture:8000'
START = ORIGIN + '/inventory-test/'
RULE = START + 'rule.pdf'
ANNEX = START + 'new.pdf'
COOKIE = 'kiwm_inventory_stage'


class PortalMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor(self):
        store.DATA = engine.DATA = Path(scratch.name)
        store.init()
        store.save_json(store.CONFIG, {key: 'synthetic-no-model' for key in
                        ('agent_model', 'analysis_model', 'vision_model')})
        pid = 'qa-document-inventory'
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (pid, 'Isolierter Dokumenttest', START, 'Prüfe die verlinkten Dateien.', 'fixture',
                       '0 6 * * *', 0, store.next_due('0 6 * * *'), store.now()))
        project = store.one('SELECT * FROM projects WHERE id=?', (pid,))
        agent_result = {'complete': True, 'checked': ['Teststartseite gelesen'],
                        'pending': [], 'findings': 'Isolierter Test ohne Dokument-Werkzeugaufruf.', 'actions': []}
        phases = []

        async def counts():
            async with httpx.AsyncClient(proxy='http://proxy:3128', timeout=15, trust_env=False) as client:
                response = await client.get(START + 'status')
                response.raise_for_status()
                values = response.json()
                self.assertEqual(set(values), {'rule', 'new'})
                self.assertTrue(all(type(value) is int for value in values.values()))
                return values

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(engine.BROWSER_CDP_URL)
            context = browser.contexts[0]
            try:
                before = await counts()
                original_rule = None
                second_rule = None
                for stage in (1, 2, 3):
                    await context.add_cookies([{'name': COOKIE, 'value': str(stage), 'domain': 'fixture',
                                               'path': '/inventory-test', 'httpOnly': False,
                                               'secure': False, 'sameSite': 'Lax'}])
                    self.assertTrue(any(c['name'] == COOKIE and c['value'] == str(stage)
                                        for c in await context.cookies(RULE)))
                    rid = 'qa-inventory-stage-' + str(stage)
                    store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',
                                  (rid, pid, store.now(), 'running', 'test'))
                    with patch('engine.run_agent', new=AsyncMock(return_value=agent_result)) as agent, \
                         patch('engine.create_summary', new=AsyncMock(return_value='Isolierter Dokumentinventar-Test.')) as summary, \
                         patch('engine.analyze_visual_changes', new=AsyncMock(return_value=[])), \
                         patch('engine.llm', new=AsyncMock(side_effect=review_only_model)) as model:
                        await asyncio.wait_for(engine.monitor(project, rid), timeout=180)
                        agent.assert_awaited_once()
                        summary.assert_awaited_once()
                    run = store.one('SELECT * FROM runs WHERE id=?', (rid,))
                    self.assertEqual(run['status'], 'success', f'Stage {stage}: {run["error"]}')
                    self.assertIsNone(run['error'])
                    evidence = json.loads((store.DATA / 'artifacts' / rid / 'evidence.json').read_text())
                    assert_final_review(self,evidence,rid,model,0 if stage==1 else 1)
                    inventory = evidence['document_check']
                    self.assertEqual(inventory['found'], 2 if stage == 2 else 1)
                    self.assertEqual(inventory['checked'], inventory['found'])
                    self.assertEqual(inventory['failed'], [])
                    self.assertEqual(inventory['unverified'], [])
                    document = store.one("SELECT * FROM snapshots WHERE run_id=? AND url=? AND kind='document'", (rid, RULE))
                    self.assertIsNotNone(document, f'Stage {stage}: stable-URL PDF was not archived.')
                    raw = (store.DATA / document['file']).read_bytes()
                    self.assertEqual(document['text'],'')
                    self.assertEqual(store.digest(raw),document['sha256'])
                    change = store.one('SELECT * FROM changes WHERE run_id=? AND url=?', (rid, RULE))
                    if stage == 1:
                        original_rule = (document, raw)
                        self.assertIsNone(change)
                    elif stage == 2:
                        second_rule = (document, raw)
                        self.assertEqual(document['url'], original_rule[0]['url'])
                        self.assertEqual(len(raw), len(original_rule[1]))
                        self.assertEqual(sum(a != b for a, b in zip(raw, original_rule[1])), 1)
                        self.assertNotEqual(document['sha256'], original_rule[0]['sha256'])
                        self.assertEqual(document['text_sha256'], original_rule[0]['text_sha256'])
                        self.assertEqual(change['kind'],'Datei geändert')
                        self.assertNotIn('text_diff',json.loads(change['evidence']))
                        self.assertIsNotNone(store.one("SELECT id FROM snapshots WHERE run_id=? AND url=? AND kind='document'", (rid, ANNEX)))
                        page_change = store.one('SELECT evidence FROM changes WHERE run_id=? AND url=?', (rid, START))
                        self.assertIn(ANNEX, json.loads(page_change['evidence'])['links_added'])
                    else:
                        self.assertNotEqual(document['sha256'], second_rule[0]['sha256'])
                        self.assertEqual(document['text_sha256'], second_rule[0]['text_sha256'])
                        self.assertEqual(document['text'], second_rule[0]['text'])
                        self.assertEqual(change['kind'], 'Datei geändert')
                        self.assertNotIn('text_diff', json.loads(change['evidence']))
                        page_change = store.one('SELECT evidence FROM changes WHERE run_id=? AND url=?', (rid, START))
                        self.assertIn(ANNEX, json.loads(page_change['evidence'])['links_removed'])
                        self.assertIsNone(store.one("SELECT id FROM snapshots WHERE run_id=? AND url=? AND kind='document'", (rid, ANNEX)))
                    current = await counts()
                    self.assertEqual(current['rule'] - before['rule'], stage)
                    self.assertEqual(current['new'] - before['new'], 0 if stage == 1 else 1,
                                     'Removed/header-only annex must not trigger a 404 fetch.')
                    phases.append({'stage': stage, 'status': run['status'], 'documents': inventory['checked'],
                                   'local_fake_review_calls': model.await_count,
                                   'rule_requests': current['rule'] - before['rule'],
                                   'annex_requests': current['new'] - before['new'],
                                   'rule_sha256': document['sha256'], 'rule_text_sha256': document['text_sha256']})
                print(json.dumps({'portal_monitor_e2e': 'PASS', 'no_document_agent_calls': True,
                                  'external_model_calls': 0,
                                  'local_fake_review_calls':sum(p['local_fake_review_calls'] for p in phases),
                                  'phases': phases}, indent=2), flush=True)
            finally:
                try:
                    await context.clear_cookies(name=COOKIE)
                finally:
                    await browser.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
