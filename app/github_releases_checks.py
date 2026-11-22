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
NOTES = '### Features\n- Added a searchable activity log.\n### Fixed\n- Fixed a tooltip.'
FEATURE = {'text': 'Durchsuchbares Aktivitätsprotokoll', 'ids': [1]}


def record(tag, **fields):
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'published_at': STAMP, 'body': NOTES, **fields}


def answer(features, *, fixes=(2,), other=(), ignored=()):
    result = {'features': features, 'fixes': list(fixes), 'other': list(other), 'ignored': list(ignored)}
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}


def keep_one():
    return {'choices': [{'message': {'content': '{"labels":["keep"]}'}}]}


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
        def respond(messages, *args, **kwargs):
            if messages[0]['content'] == github.RELEASE_PROMPT:
                return model_answer or answer([FEATURE])
            return keep_one()
        model = AsyncMock(side_effect=respond)
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
        self.assertEqual(model.await_count, 2)
        self.assertIn(FEATURE['text'], run['summary'])
        self.assertNotIn('Fixed a tooltip', run['summary'])
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
        run, model = await self.run_project(new, old, model_answer=answer([{**FEATURE, 'ids': [77]}]))
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(model.await_count, 4)
        self.assertEqual(store.rows('SELECT * FROM changes'), [])
        self.assertEqual(len(store.rows('SELECT * FROM snapshots')), 1)
        run, _ = await self.run_project(new, old)
        self.assertEqual(run['status'], 'success')
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 1)

    async def test_mixed_release_shows_features_only_and_links_back_on_next_check(self):
        old, new = record('v1.0.0'), record('v1.1.0')
        await self.run_project(old)
        run, model = await self.run_project(new, old)
        self.assertEqual(model.await_count, 2)
        self.assertEqual(run['summary'].split('\n\n')[1:], ['**Neue Features**', '- ' + FEATURE['text']])
        evidence = json.loads(store.one('SELECT evidence FROM changes')['evidence'])
        self.assertTrue(evidence['has_fixes'])
        self.assertEqual(evidence['features'][0]['quote'], '- Added a searchable activity log.')
        repeated, model = await self.run_project(new, old)
        model.assert_not_awaited()
        self.assertIn('/runs/' + run['id'], repeated['summary'])
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 1)

    async def test_pure_fixes_have_exactly_the_requested_text(self):
        old, new = record('v1.0.0'), record('v1.1.0', body='### Fixed\n- Fixed a tooltip.')
        await self.run_project(old)
        run, _ = await self.run_project(new, old, model_answer=answer([], fixes=[1]))
        self.assertEqual(run['status'], 'success')
        self.assertEqual(run['summary'].split('\n\n')[1:], ['nur Bugfixes'])
        evidence = json.loads(store.one('SELECT evidence FROM changes')['evidence'])
        self.assertEqual(evidence['ai_assessment']['summary'], 'Neue stabile Version v1.1.0 · nur Bugfixes')

    async def test_incomplete_mixed_batch_is_recovered_in_smaller_parts(self):
        incomplete = answer([], fixes=[2])
        model = AsyncMock(side_effect=[incomplete, incomplete, answer([FEATURE], fixes=[]), incomplete, keep_one()])
        findings = await github.summarize_release({'tag': 'v1', 'notes': NOTES}, '', model, 'test', str)
        self.assertEqual([f['text'] for f in findings['features']], [FEATURE['text']])
        self.assertTrue(findings['has_fixes'])
        self.assertEqual(model.await_count, 5)

    async def test_feature_and_repair_in_the_same_bullet(self):
        notes = '## What is new\n- Chat shows live progress. Disconnects no longer stop the chat.'
        feature = {'ids': [1], 'text': 'Der Chat zeigt Fortschritt und läuft nach Verbindungsfehlern weiter.'}
        review = {'labels': ['keep'], 'gekuerzt': {'0': 'Der Chat zeigt den Fortschritt live an.'}}
        model = AsyncMock(side_effect=[answer([feature], fixes=[1]), {'choices':[{'message':{'content':json.dumps(review)}}]}])
        result = await github.summarize_release({'tag':'v1','notes':notes}, '', model, 'test', str)
        self.assertEqual(result['features'][0]['text'], review['gekuerzt']['0'])
        self.assertIn('What is new', model.await_args.args[0][1]['content'])
        self.assertEqual(result['features'][0]['sources'], [notes.splitlines()[1]])

    async def test_fix_section_can_include_an_overlooked_api_feature(self):
        release = {'tag':'2','notes':'## Fixed\n- GET /widgets/{id} retrieves an individual widget.'}
        model = AsyncMock(side_effect=[answer([], fixes=[1]), answer([{'ids':[1],'text':'Widgets lassen sich einzeln abrufen.'}], fixes=[])])
        result = await github.summarize_release(release, '', model, 'test', str)
        self.assertEqual([f['text'] for f in result['features']], ['Widgets lassen sich einzeln abrufen.'])
        release['notes'] = '## Fixed\n- GET /widgets/{id} no longer returns private widgets without permission.'
        model = AsyncMock(return_value=answer([], fixes=[1]))
        result = await github.summarize_release(release, '', model, 'test', str)
        self.assertEqual(github.no_features_note(result), 'nur Bugfixes')

    async def test_api_context_only_uses_cited_prs_merged_by_release_date(self):
        requested = []
        def reply(request):
            requested.append(str(request.url))
            number = int(request.url.path.split('/')[-1])
            return httpx.Response(200, json={'number':number, 'title':'Add widget lookup',
                                             'merged_at':{4:STAMP,5:'2027-01-01T00:00:00Z',6:None}[number]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        with patch.object(github.httpx, 'AsyncClient', return_value=client), patch.object(github.site_safety, 'wait', new=AsyncMock()):
            refs = await github.referenced_api_changes({'url':URL+'/tag/v1', 'published':STAMP}, 'GET /widgets (#4, #5, #6, #4)')
        self.assertEqual(refs, [{'url':'https://github.com/example/monitor/pull/4', 'title':'Add widget lookup'}])
        self.assertEqual(requested, [f'https://api.github.com/repos/example/monitor/pulls/{n}' for n in (4,5,6)])

    async def test_new_capability_under_updated_is_named_without_the_fix(self):
        await self.run_project(record('v1.0.0'))
        notes = '### Updated\n- Users can now export their playlists as CSV.\n### Fixed\n- Fixed a sorting crash.'
        result = answer([{'ids': [1], 'text': 'Wiedergabelisten lassen sich als CSV exportieren.'}])
        run, _ = await self.run_project(record('v1.1.0', body=notes), model_answer=result)
        self.assertIn('als CSV exportieren', run['summary'])
        self.assertNotIn('sorting', run['summary'])
        self.assertNotIn('Bugfix', run['summary'])

    async def test_version_bump_alongside_fixes_is_not_a_pure_bugfix_release(self):
        notes = 'Bug fixes and a new backup component version.\n\n### Fixed\n- Fix crash on logout.'
        verdict = {'choices': [{'message': {'content': json.dumps({'other_quote': 'a new backup component version'})}}]}
        model = AsyncMock(side_effect=[answer([], fixes=[1, 2]), verdict])
        result = await github.summarize_release({'tag': 'v1.1.0', 'notes': notes}, '', model, 'test', str)
        self.assertEqual(github.no_features_note(result), 'Keine neuen Features in den Release Notes genannt.')
        self.assertIn('a new backup component version', result['other_quote'])
        model.assert_awaited_once()

    async def test_final_selection_retains_concrete_features_and_removes_a_repair(self):
        candidates = [{'text': text, 'sources': [text]} for text in ('Neue Exportfunktion', 'Korrigierter Neustart', 'Neue Filter')]
        model = AsyncMock(return_value={'choices': [{'message': {'content': '{"labels":["keep","fix","keep"]}'}}]})
        kept, fixes, other = await github.verify_features(candidates, model, 'test')
        self.assertEqual(kept, [candidates[0], candidates[2]])
        self.assertTrue(fixes)
        self.assertFalse(other)

    async def test_maintenance_does_not_imply_a_fix_or_feature(self):
        await self.run_project(record('v1.0.0'))
        run, _ = await self.run_project(record('v1.1.0', body='### Maintenance\n- Update dependencies.'),
                                        model_answer=answer([], fixes=[], other=[1]))
        self.assertEqual(run['summary'].split('\n\n')[1:], ['Keine neuen Features in den Release Notes genannt.'])

    async def test_publisher_fix_release_excludes_version_inventory_and_known_issues(self):
        notes = ('# Example 2: fixes in the app\n\nControl is 2.0.\n\n## Fixed\n- Fix search.\n\n'
                 '## Known problems\n- An existing issue still affects imports.')
        model = AsyncMock(side_effect=[
            answer([], fixes=[2], other=[1,3]),
            {'choices': [{'message': {'content': '{"other_quote":null}'}}]}])
        result = await github.summarize_release({'tag':'2','notes':notes}, '', model, 'test', str)
        self.assertEqual(github.no_features_note(result), 'nur Bugfixes')
        self.assertNotIn('Control is 2', model.await_args.args[0][1]['content'])
        self.assertEqual(github.publisher_fix_notes(github.note_blocks(notes+'\n## Changes\n- Retire the old theme.')), '')
        changed_intro = notes.replace('Control is 2.0.', 'Control is 2.0. We switched authentication providers.')
        self.assertIn('switched authentication',github.publisher_fix_notes(github.note_blocks(changed_intro)))


if __name__ == '__main__':
    unittest.main()
