import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import document_relinks
import article_headlines
import email_reports
import engine
import github_releases
import reader_summary
import store


class ArticleHeadlines(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(dir='/data')
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        override = patch.multiple(store,DATA=root,CONFIG=root/'settings.json',SECRETS=root/'secrets.json')
        override.start()
        self.addCleanup(override.stop)
        store.init()
        self.project = {'id':'articles','name':'IWW','url':'https://www.iww.de/pbp/archiv',
                        'prompt':article_headlines.FORMAT,'tags':'[]'}
        store.execute("""INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created)
            VALUES(?,?,?,?,?,'0 8 * * 1',?,?)""",
            ('articles','IWW',self.project['url'],self.project['prompt'],'www.iww.de',store.now(),store.now()))
        self.old = {'url':'https://www.iww.de/pbp/fachwissen/bisheriger-artikel-f100','label':'Bekannter Artikel'}
        self.new = {'url':'https://www.iww.de/pbp/fachwissen/neuer-artikel-f101','label':'Neue Vorgabe für die Planung'}

    def capture(self,rid,month,links,status='success'):
        stamp=store.now()
        store.execute("INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,'articles',?,?,?,'test')",
                      (rid,stamp,stamp,status))
        url=f'https://www.iww.de/pbp/archiv/2026/{month}'
        store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (rid,rid,'articles',url,'page',stamp,'sha','sha','Titles',json.dumps([x['url'] for x in links]),None,None,None))
        return {'url':url,'kind':'page','snapshot_id':rid,'metadata':{'links':links},'prior_snapshot_available':False}

    def evidence(self,rid,observation):
        return {'task':article_headlines.FORMAT,'agent':{'complete':True},'prior_successful_runs':1,
                'observations':[observation],'changes':store.rows('SELECT * FROM changes WHERE run_id=?',(rid,))}

    def test_month_rollover_reports_only_previously_unknown_articles(self):
        self.capture('before',11,[self.old])
        current=self.capture('after',12,[self.old,self.new])
        article_headlines.record_additions(self.project,'after',[current])
        evidence=self.evidence('after',current)
        text=reader_summary.verified_summary(evidence)
        self.assertIn(self.new['label'],text)
        self.assertNotIn(self.old['label'],text)
        self.assertEqual(text.count('### '),1)
        following=self.capture('next',12,[self.new,self.old])
        following['prior_snapshot_available']=True
        article_headlines.record_additions(self.project,'next',[following])
        self.assertEqual(reader_summary.verified_summary(self.evidence('next',following)),'Keine neuen Artikel erkannt.')

    def test_first_inventory_is_a_baseline_and_incomplete_inventory_stays_open(self):
        current=self.capture('initial',11,[self.old,self.new])
        article_headlines.record_additions(self.project,'initial',[current])
        self.assertEqual(store.rows('SELECT * FROM changes'),[])
        evidence=self.evidence('initial',current)
        evidence['prior_successful_runs']=0
        self.assertEqual(reader_summary.verified_summary(evidence),'Ausgangsstand der Artikelübersicht erfasst.')
        evidence['errors']=['Inhaltsverzeichnis fehlt']
        self.assertIsNone(reader_summary.verified_summary(evidence))

    def test_titles_reach_mail_and_telegram_without_old_titles_or_assessment_prose(self):
        import notifications
        self.capture('before',11,[self.old])
        current=self.capture('after',12,[self.old,self.new])
        article_headlines.record_additions(self.project,'after',[current])
        evidence=self.evidence('after',current)
        folder=store.DATA/'artifacts'/'after'
        folder.mkdir(parents=True)
        (folder/'evidence.json').write_text(json.dumps(evidence))
        changes=evidence['changes']
        run=store.one("SELECT * FROM runs WHERE id='after'")
        report=email_reports.build_model([self.project],[run],changes,[],run['started'],run['finished'])
        self.assertEqual(report['projects']['articles']['details'],[self.new['label']])
        text=notifications.message(self.project,run,[(c,json.loads(c['evidence'])) for c in changes])
        self.assertIn(self.new['label'],text)
        self.assertNotIn(self.old['label'],text)
        self.assertNotIn('Neu in der überwachten Artikelübersicht',text)

    def test_removed_links_and_ads_never_become_added_article_headings(self):
        evidence={'observations':[{'kind':'page','metadata':{'links':[self.old,self.new]}}],
                  'changes':[{'evidence':{'links_added':[self.new['url'],'https://shop.iww.de/products/pbp'],
                                         'links_removed':[self.old['url']],
                                         'ai_assessment':{'relevance':'relevant'}}}]}
        self.assertEqual(article_headlines.from_evidence(evidence),[{'url':self.new['url'],'title':self.new['label']}])
        evidence['observations'][0]['metadata']['links']=[self.old]
        self.assertIsNone(article_headlines.from_evidence(evidence))


class ReaderSummaries(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.url = 'https://example.org/downloads/'
        self.evidence = {
            'task': reader_summary.PREFIX + 'Alle {Dateizahl} Unterlagen sind unverändert.',
            'prior_successful_runs': 2, 'agent': {'complete': True},
            'document_check': {'checked': 3}, 'changes': [],
            'observations': [{'kind': 'page', 'url': self.url, 'prior_snapshot_available': True}],
        }

    def test_quiet_only_with_compatible_successful_comparison(self):
        self.assertEqual(reader_summary.verified_summary(self.evidence), 'Alle 3 Unterlagen sind unverändert.')
        for field, value in [('agent', {'complete': False}), ('agent', {'complete': True, 'pending': ['Quelle']}),
                             ('prior_successful_runs', 0), ('errors', ['Timeout']),
                             ('document_check', {'unverified': [self.url]}),
                             ('observations', [{'prior_snapshot_available': False}]),
                             ('observations', [{'prior_snapshot_available': True, 'metadata': {'comparison_scope_changed': True}}])]:
            with self.subTest(field=field):
                self.assertIsNone(reader_summary.verified_summary({**self.evidence, field: value}))

    async def test_incomplete_sources_cannot_produce_quiet_report(self):
        self.evidence['document_check']['failed'] = [{'url': self.url}]
        with patch.object(engine, 'llm', new_callable=AsyncMock) as model:
            result = await engine.create_summary(self.evidence, 'test')
        model.assert_not_awaited()
        self.assertIn('Ein Dokument konnte nicht geprüft werden', result)
        self.assertNotIn('unverändert', result)

    async def test_open_difference_links_to_affected_source(self):
        self.evidence['changes'] = [{'kind': 'Links geändert', 'url': self.url,
                                     'evidence': {'ai_assessment': {'relevance': 'uncertain'}}}]
        with patch.object(engine, 'llm', new_callable=AsyncMock) as model:
            result = await engine.create_summary(self.evidence, 'test')
        model.assert_not_awaited()
        self.assertIn('Bedeutung ist noch offen', result)
        self.assertIn(self.url, result)

    async def test_document_names_and_separate_editorial_change_both_reach_the_reader(self):
        dest = self.url + 'hinweis.pdf'
        self.evidence['changes'] = [{'id': 'mixed', 'kind': 'Inhalt geändert; Links geändert', 'url': self.url,
            'evidence': {'links_added': [dest], 'ai_assessment': {'relevance': 'relevant'},
                         'document_relinks': {'complete': False, 'files': [], 'entries': [
                             {'direction': 'added', 'title': 'Hinweis zur Prüfstelle', 'url': dest}]}}}]
        answer = 'Die Seite hat die bisherige Angabe zur Zuständigkeit entfernt.'
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}]}
        with patch.object(engine, 'llm', new=AsyncMock(return_value=response)):
            result = await engine.create_summary(self.evidence, 'test')
        self.assertIn('Hinweis zur Prüfstelle', result)
        self.assertIn(answer, result)

    async def test_changed_pdf_and_removed_notice_use_only_recorded_wording(self):
        url = self.url + 'merkblatt.pdf'
        detail = {'links_added': [url], 'ai_assessment': {'relevance': 'relevant'},
                  'document_relinks': {'complete': False, 'links_complete': True, 'entries': [
                      {'direction': 'added', 'title': 'Neues Merkblatt', 'url': url}]},
                  'text_diff': '--- vorher\n+++ nachher\n@@ -1,2 +1,2 @@\n-Alte Auskunftsstelle\n+Neues Merkblatt\n+PDF-Dokument (62.8 kB)'}
        self.evidence['changes'] = [{'kind': 'Inhalt geändert; Links geändert', 'url': self.url, 'evidence': detail}]
        with patch.object(engine, 'llm', new_callable=AsyncMock) as model:
            report = await engine.create_summary(self.evidence, 'test')
        model.assert_not_awaited()
        self.assertIn('Von der Seite entfernt · „Alte Auskunftsstelle“', report)
        self.assertIn('Neues Merkblatt', report)
        self.assertNotIn('62.8', report)

    def test_explicit_article_list_does_not_receive_quiet_override(self):
        self.evidence['task'] = 'Titel und ersten Absatz aller Artikel anzeigen.'
        self.assertIsNone(reader_summary.verified_summary(self.evidence))

    def test_failed_day_survives_next_success_but_not_same_day_recovery(self):
        runs = [{'finished': f'2026-11-{day}T{hour:02}:00:00Z', 'status': status}
                for day, hour, status in [(16, 9, 'failed'), (17, 8, 'failed'), (17, 9, 'success')]]
        self.assertEqual(email_reports.days_without_success(runs), ['16.11.2026'])

    def test_release_details_precede_the_less_specific_overview(self):
        notes = '# Release\n## What changes for you\n- More space.\n## Setup\n- Only for model X.\n## Fixed\n- Typo.'
        blocks = github_releases.note_blocks(notes)
        self.assertEqual(blocks[0]['source'], '- Only for model X.')
        self.assertTrue(blocks[-1]['section'].endswith('What changes for you'))

    def test_unclassified_or_invented_release_sources_are_rejected(self):
        batch = [{'id': 1, 'source': 'Added tabs.'}, {'id': 2, 'source': 'Crash fixed.'}]
        for fixes in [[], [3], [True]]:
            content = {'features': [{'text': 'Neue Reiter', 'ids': [1]}], 'fixes': fixes, 'other': [], 'ignored': []}
            with self.subTest(fixes=fixes), self.assertRaises(ValueError):
                github_releases.decode_changes({'choices':[{'message':{'content':json.dumps(content)}}]}, batch)

    def test_every_named_feature_keeps_its_original_source(self):
        batch = [{'id': i, 'source': f'Feature {i}'} for i in range(1, 8)]
        content = {'features': [{'ids': [b['id']], 'text': 'Funktion ' + str(b['id'])} for b in batch],
                   'fixes': [], 'other': [], 'ignored': []}
        features, fixes, other = github_releases.decode_changes({'choices':[{'message':{'content':json.dumps(content)}}]}, batch)
        self.assertEqual([item['quote'] for item in features], [b['source'] for b in batch])
        self.assertFalse(fixes or other)


class ReplacedDownloads(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.enterContext(patch.object(store, 'DATA', self.root))
        self.url = 'https://example.org/list'
        self.pages = {}
        self.docs = {}
        self.change = {'kind': 'Links geändert', 'previous_id': 'old', 'current_id': 'new',
                       'project_id': 'project', 'run_id': 'new', 'url': self.url}

    def snapshot(self, run, entries):
        folder = self.root / 'artifacts' / run
        folder.mkdir(parents=True)
        html = folder / 'page.html'
        html.write_text(''.join(f'<a href="{name}.pdf">{label}</a>' for name, label, _ in entries))
        self.pages[run] = {'run_id': run, 'url': self.url, 'html': str(html.relative_to(self.root))}
        self.docs[run] = []
        for name, _, data in entries:
            path = folder / (name + '.pdf')
            path.write_bytes(data)
            self.docs[run].append({'url': 'https://example.org/' + path.name, 'id': run + name,
                                   'file': str(path.relative_to(self.root)), 'sha256': hashlib.sha256(data).hexdigest()})

    def compare(self, task=''):
        self.change['evidence'] = json.dumps({'links_removed': [d['url'] for d in self.docs['old']],
                                               'links_added': [d['url'] for d in self.docs['new']]})
        with patch.object(store, 'one', side_effect=lambda q, p: self.pages[p[0]]), \
                patch.object(store, 'rows', side_effect=lambda q, p: self.docs[p[1]]):
            return document_relinks.compare(self.change, task)

    def test_changed_and_byte_identical_downloads_are_distinguished(self):
        self.snapshot('old', [('a', 'Anlage A', b'1'), ('b', 'Anlage B', b'2')])
        self.snapshot('new', [('c', 'Anlage A', b'3'), ('d', 'Anlage B', b'2')])
        result = self.compare()
        self.assertTrue(result['complete'])
        lines = reader_summary._change_lines(self.change, {'document_relinks': result}, {})
        self.assertEqual(len(lines), 1)
        self.assertIn('Anlage A', lines[0])
        self.assertIn('c.pdf', lines[0])

    def test_digest_mismatch_disqualifies_pair(self):
        self.snapshot('old', [('a', 'Anlage A', b'1')])
        self.snapshot('new', [('b', 'Anlage A', b'2')])
        (self.root / self.docs['new'][0]['file']).write_bytes(b'3')
        self.assertIsNone(self.compare())

    def test_ambiguous_title_is_not_a_pair(self):
        self.snapshot('old', [('a', 'Anlage', b'1'), ('b', 'Anlage', b'2')])
        self.snapshot('new', [('c', 'Anlage', b'3')])
        self.assertIsNone(self.compare())

    def test_generic_download_uses_its_own_visible_card_title(self):
        self.snapshot('old', [('a', 'Erste Prüfstelle', b'1')])
        html = self.root / self.pages['old']['html']
        html.write_text('<ul><li><strong class="title">Erste Prüfstelle</strong><div><div>'
                        '<a href="a.pdf">Download</a></div></div></li>'
                        '<li><strong class="title">Zweite Stelle</strong><a href="b.pdf">Download</a></li></ul>')
        self.assertEqual(document_relinks._titles(self.pages['old']), {
            'Erste Prüfstelle': {'https://example.org/a.pdf'}, 'Zweite Stelle': {'https://example.org/b.pdf'}})

    def test_distinct_titles_are_listed_separately_without_guessing_identity(self):
        self.snapshot('old', [('a', 'Prüfstelle für Fliegende Bauten', b'1')])
        self.snapshot('new', [('b', 'Zuständige Prüfstelle, Hinweise zu Fliegenden Bauten', b'2')])
        self.assertIsNone(self.compare())
        result = self.compare(document_relinks.INVENTORY_REQUEST)
        self.assertTrue(result['complete'])
        self.assertFalse(result['files'])
        self.assertEqual([v['direction'] for v in result['entries']], ['added', 'removed'])

    def test_identical_files_with_duplicate_labels_and_new_cache_dates(self):
        for run, stamp in [('old', '1786349494'), ('new', '1795181916')]:
            self.snapshot(run, [('a', 'Begründung', b'1'), ('b', 'Begründung', b'2')])
            path = self.root / self.pages[run]['html']
            path.write_text(path.read_text().replace('.pdf', '.pdf?ts=' + stamp))
            for d in self.docs[run]:
                d['url'] += '?ts=' + stamp
        result = self.compare(document_relinks.INVENTORY_REQUEST)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['files']), 2)
        self.assertFalse(any(f['previous_sha256'] != f['current_sha256'] for f in result['files']))
