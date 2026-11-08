import json
import unittest
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape

import obsidian_changelog as obsidian
from visual_browser import approved_login_action


class ObsidianChecks(unittest.IsolatedAsyncioTestCase):
    def test_feed_uses_publisher_status_not_version_string(self):
        def entry(day, label):
            return f"""<entry><title>Obsidian 1.2.3 Desktop ({label})</title>
            <link href="https://obsidian.md/changelog/2026-11-{day}-desktop-v1.2.3/"/>
            <updated>2026-11-{day}T00:00:00Z</updated>
            <content type="html">{escape('<h2>New</h2><p>Added search.</p>')}</content></entry>"""
        raw = (f'<feed xmlns="http://www.w3.org/2005/Atom">'
               f'{entry("07", "Early access")}{entry("08", "Public")}</feed>').encode()
        releases = obsidian.public_releases(raw)
        self.assertEqual(len(releases), 1)
        self.assertIn('11-08', releases[0]['url'])
        self.assertEqual(obsidian.source_blocks(releases[0], set())[0],
                         [{'id': 1, 'section': 'New', 'source': 'Added search.'}])
        with self.assertRaises(ValueError):
            obsidian.public_releases(raw.replace(b'(Public)', b'(Unknown)'))

    async def test_translates_every_batch_and_rejects_missing_source_items(self):
        html = '<h2>Features</h2>' + ''.join(f'<p>Feature {n}</p>' for n in range(1, 14))
        def model(messages, *args, **kwargs):
            rows = json.loads(messages[1]['content'])
            return {'choices': [{'message': {'content': json.dumps({'items': [
                {'id': row['id'], 'text': f'Funktion {row["id"]}'} for row in rows]})}}]}
        llm = AsyncMock(side_effect=model)
        result, refs = await obsidian.translate({'html': html}, set(), llm, 'model', lambda value: value)
        self.assertEqual(len(result), 13)
        self.assertEqual(llm.await_count, 2)
        self.assertEqual(refs, [])
        def sometimes_invalid(messages, *args, **kwargs):
            if len(json.loads(messages[1]['content'])) > 1:
                return {'choices': [{'message': {'content': '{"items":'}}]}
            return model(messages, *args, **kwargs)

        sometimes_async = AsyncMock(side_effect=sometimes_invalid)
        result, _ = await obsidian.translate({'html': '<p>First</p><p>Second</p>'}, set(),
                                            sometimes_async, 'model', lambda value: value)
        self.assertEqual(result, ['Funktion 1', 'Funktion 2'])
        self.assertEqual(sometimes_async.await_count, 3)
        with self.assertRaises(ValueError):
            obsidian.decode_batch(model([{}, {'content': '[{"id":1}]'}]), [1, 2])

    def test_visual_login_accepts_only_the_exact_project_as_new_return_target(self):
        endpoint = 'https://example.test/sso/login?'
        target = 'https://example.test/library/start'
        configured = endpoint + 'return_url=https%3A%2F%2Fexample.test%2Flibrary%2Flogin'
        valid = endpoint + 'return_url=https%3A%2F%2Fexample.test%2Flibrary%2Fstart%3F'
        self.assertTrue(approved_login_action(configured, configured, target))
        self.assertTrue(approved_login_action(valid, configured, target))
        for invalid in (valid + '&return_url=x', valid + '&mode=other',
                        valid.replace('example.test%2F', 'evil.test%2F'),
                        valid.replace('/sso/login', '/sso/other'),
                        valid.replace('start%3F', 'other%3F')):
            self.assertFalse(approved_login_action(invalid, configured, target))
