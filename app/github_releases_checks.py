import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

scratch = tempfile.TemporaryDirectory(prefix='github-releases-qa-', dir='/data')
os.environ['DATA_DIR'] = scratch.name

import attention
import engine
import github_releases as github
import store


REPO = ('example', 'monitor')
URL = 'https://github.com/example/monitor/releases'
STAMP = '2026-11-07T12:00:00Z'
NOTES = 'New features: Added a searchable activity log. Fixed a tooltip.'
FEATURE = {'text': 'Durchsuchbares Aktivitätsprotokoll', 'quote': 'Added a searchable activity log.'}


def record(tag, **fields):
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'published_at': STAMP, 'body': NOTES, **fields}


def answer(features):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'features': features})}}]}


class GitHubReleaseChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=scratch.name)
        self.addCleanup(self.root.cleanup)
        from pathlib import Path
        store.DATA = engine.DATA = Path(self.root.name)
        store.CONFIG = store.DATA / 'settings.json'
        store.SECRETS = store.DATA / 'credentials.json'
        store.init()
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      ('github', 'GitHub QA', URL, 'Neue Funktionen knapp benennen', 'github.com, api.github.com', '0 6 * * *', 1, STAMP, STAMP))

    async def run_project(self, *records, model_answer=None):
        rid = store.enqueue('github')
        project = store.one('SELECT * FROM projects WHERE id=?', ('github',))
        releases = [item for item in (github.stable_release(r, REPO) for r in records) if item]
        model = AsyncMock(return_value=model_answer or answer([FEATURE]))
        with patch.object(github, 'fetch_releases', new=AsyncMock(return_value=releases)), \
                patch.object(engine.project_icons, 'refresh', new=AsyncMock()), \
                patch.object(engine, 'llm', new=model), patch.object(engine.notifications, 'enabled', return_value=True):
            await engine.run_job(project, rid)
        run = store.one('SELECT * FROM runs WHERE id=?', (rid,))
        return run, model

    def test_provider_url_and_prerelease_filters(self):
        self.assertEqual(github.repository(URL + '/'), REPO)
        for url in (URL + '/tag/v1', URL + '?query=1', URL.replace('github.com', 'github.com.evil.test')):
            self.assertIsNone(github.repository(url))
        for entry in (record('v1.2.3', draft=True), record('v1.2.3', prerelease=True), record('v1.2.3-rc.1'),
                      record('v1.2.3beta1'), record('nightly'), record('v1.2.3-preview.4'), record('v1.2.3', name='v1.2.3 (Beta 2)')):
            self.assertIsNone(github.stable_release(entry, REPO))
        self.assertIsNotNone(github.stable_release(record('v1.2.3', name='New beta features now available'), REPO))
        with self.assertRaises(ValueError):
            github.stable_release(record('v1.2.3', prerelease=None), REPO)

    async def test_pagination_uses_only_the_fixed_api_host(self):
        requests = []
        def response(request):
            requests.append(request.url)
            if request.url.params['page'] == '1':
                return httpx.Response(200, json=[record('v1.1.0-rc.1'), record('v1.0.0')],
                                      headers={'Link': '<https://untrusted.example/next>; rel="next"'})
            return httpx.Response(200, json=[record('v0.9.0')])
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            releases = await github.fetch_releases(client, REPO)
        self.assertEqual([r['tag'] for r in releases], ['v1.0.0', 'v0.9.0'])
        self.assertEqual([u.host for u in requests], ['api.github.com', 'api.github.com'])

    async def test_baseline_prereleases_promotion_and_deduplication(self):
        old, candidate, final = record('v1.0.0'), record('v1.1.0', prerelease=True), record('v1.1.0')
        for records in ((old,), (old, candidate, record('v1.2.0-beta.1'))):
            run, model = await self.run_project(*records)
            self.assertEqual(run['status'], 'success')
            model.assert_not_awaited()
            self.assertEqual(store.rows('SELECT * FROM changes'), [])
            self.assertEqual(store.rows('SELECT * FROM notifications'), [])
        run, model = await self.run_project(final, record('v1.2.0', body=''), old)
        model.assert_awaited_once()
        self.assertIn(FEATURE['text'], run['summary'])
        self.assertIn('Keine Release Notes veröffentlicht.', run['summary'])
        self.assertEqual(attention.summary('github')['attention_count'], 2)
        self.assertEqual(len(store.rows('SELECT * FROM notifications')), 1)
        await self.run_project(old)
        run, model = await self.run_project(final, old)
        self.assertIn('Keine neue stabile Version erkannt.', run['summary'])
        model.assert_not_awaited()
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 2)

    async def test_failed_feature_evidence_does_not_advance_baseline(self):
        old, new = record('v1.0.0'), record('v1.1.0')
        await self.run_project(old)
        run, model = await self.run_project(new, old, model_answer=answer([{**FEATURE, 'quote': 'Erfunden'}]))
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(model.await_count, 2)
        self.assertEqual(store.rows('SELECT * FROM changes'), [])
        self.assertEqual(len(store.rows('SELECT * FROM snapshots')), 1)
        run, _ = await self.run_project(new, old)
        self.assertEqual(run['status'], 'success')
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 1)


if __name__ == '__main__':
    unittest.main()
