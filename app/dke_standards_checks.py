"""DKE fragment validation and isolated change/report integration."""
from datetime import datetime
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import attention
import dke_standards as dke
import email_reports
import engine
import store


def selections():
    parts = []
    for identifier, (selector, slug, _) in enumerate(dke.PERIODS.values(), 1):
        path = f'{dke.PAGE_PATH}/ergebnisse-national--normen--{slug}-{identifier}?view=fragment'
        parts.append(f'<a id="{selector}" href="#aetable" data-table="{identifier}"></a>'
                     f'<div id="dbq{identifier}" data-datatable-url="{path}"></div>')
    return ''.join(parts)


def table(period, *entries):
    rows = ''.join(
        f'<tr><td><a href="{dke.DETAIL_PATH}?type=dke%7Cdokument&amp;id={identifier}">'
        f'{escape(name)}</a></td><td>DKE/K 1</td><td>Elektrische Anlage &amp; Prüfung</td></tr>'
        for name, identifier in entries)
    return (f'<h1>Ergebnisse national: Normen, {dke.PERIODS[period][2]}</h1>'
            '<div class="js-datatable"><table><tbody><tr><td>Schriftstück</td><td>Gremium</td>'
            f'<td>Titel</td></tr>{rows}</tbody></table></div>')


def publication(number, edition, identifier):
    return dke.parse_table(table('month', (f'{number}:{edition}', identifier)), 'month')[0]


class DKEChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir='/data')
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        for module, values in ((store, {'DATA': root, 'CONFIG': root/'settings.json', 'SECRETS': root/'secrets.json'}),
                               (engine, {'DATA': root})):
            override = patch.multiple(module, **values)
            override.start()
            self.addCleanup(override.stop)
        store.init()
        store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            ('dke', 'DKE QA', dke.SOURCE, 'National, Norm', 'www.dke.de', '0 6 * * *',
             1, '2099-01-01T00:00:00Z', store.now()))

    def test_only_the_public_national_norm_fragments_are_selected(self):
        self.assertTrue(dke.matches(dke.SOURCE))
        self.assertFalse(dke.matches(dke.SOURCE.replace('www.dke.de', 'www.dke.de.evil.test')))
        urls = dke.table_urls(selections())
        self.assertTrue(urls['month'].endswith('--aktueller-monat-1?view=fragment'))
        self.assertTrue(urls['quarter'].endswith('--aktuelles-quartal-2?view=fragment'))
        self.assertTrue(urls['previous_quarter'].endswith('--letztes-quartal-3?view=fragment'))
        for altered in (selections().replace('id="n_am"', 'id="ne_am"'),
                        selections().replace('national--normen', 'national--norm-entwuerfe'),
                        selections().replace('data-datatable-url="/de/', 'data-datatable-url="//evil.test/de/')):
            with self.assertRaisesRegex(RuntimeError, 'nicht eindeutig gefunden'):
                dke.table_urls(altered)

    def test_complete_rows_empty_drafts_and_malformed_markup(self):
        self.assertEqual(dke.parse_table('', 'month'), [])
        self.assertEqual(dke.parse_table(table('month'), 'month'), [])
        html = table('month', ('DIN EN 123:2026-11', '123'), ('E DIN EN 456:2026-11', '456'))
        records = dke.parse_table(html, 'month')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0], {'number': 'DIN EN 123', 'edition': '2026-11', 'committee': 'DKE/K 1', 'document_type': 'Norm',
                                      'title': 'Elektrische Anlage & Prüfung',
                                      'url': dke.ORIGIN + dke.DETAIL_PATH + '?type=dke%7Cdokument&id=123'})
        for invalid in ('<h1>Fehler</h1>', html.replace('</tr>', '', 1), html.replace('2026-11', 'unbekannt'),
                        html.replace('aktueller Monat', 'letztes Quartal'), html.replace('href="/de/', 'href="https://evil.test/de/')):
            with self.assertRaises(RuntimeError):
                dke.parse_table(invalid, 'month')

    async def test_http_empty_success_stale_month_and_transport_error(self):
        state = {'month': '', 'status': 200}

        def response(request):
            if request.url.path == dke.PAGE_PATH:
                return httpx.Response(200, text=selections(), headers={'content-type': 'text/html'})
            if request.url.path.endswith('aktueller-monat-1'):
                return httpx.Response(state['status'], text=state['month'], headers={'content-type': 'text/html'})
            return httpx.Response(200, text='', headers={'content-type': 'text/html'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            self.assertEqual(await dke.fetch_tables(client, '2026-11'), {period: [] for period in dke.PERIODS})
            state['month'] = table('month', ('DIN 123:2026-10', '123'))
            with self.assertRaisesRegex(RuntimeError, 'abweichenden Ausgabemonat'):
                await dke.fetch_tables(client, '2026-11')
            state.update(month='', status=502)
            with self.assertRaisesRegex(RuntimeError, 'HTTP 502'):
                await dke.fetch_tables(client, '2026-11')

    async def run_project(self, month, *, monthly=(), quarter=(), previous=(), error=None):
        rid = store.enqueue('dke')
        project = store.one("SELECT * FROM projects WHERE id='dke'")
        with patch.object(dke, 'datetime') as clock, \
                patch.object(dke, 'fetch_tables', new=AsyncMock(
                    return_value={'month': monthly, 'quarter': quarter, 'previous_quarter': previous},
                    side_effect=error)), \
                patch.object(engine.project_icons, 'refresh', new=AsyncMock()), \
                patch.object(engine.notifications, 'enabled', return_value=True):
            clock.now.return_value = datetime.fromisoformat(month + '-01')
            await engine.run_job(project, rid)
        return store.one('SELECT * FROM runs WHERE id=?', (rid,))

    async def test_initial_state_new_edition_and_report_source_links(self):
        old = publication('DIN EN 123', '2026-10', '123')
        new = publication('DIN EN 123', '2026-11', '456')
        baseline = await self.run_project('2026-10', monthly=[old], quarter=[old, new])
        self.assertEqual(baseline['status'], 'success')
        self.assertIn('Ausgangsstand', baseline['summary'])
        self.assertEqual(store.rows('SELECT * FROM changes'), [])
        self.assertEqual(store.rows('SELECT * FROM notifications'), [])
        run = await self.run_project('2026-11', monthly=[new], quarter=[old, new])
        self.assertEqual(run['status'], 'success')
        self.assertIn('DIN EN 123:2026-11', run['summary'])
        changes = store.rows('SELECT * FROM changes')
        self.assertEqual(len(changes), 1)
        self.assertEqual(json.loads(changes[0]['evidence'])['publication'], new)
        self.assertEqual(attention.summary('dke')['attention_count'], 1)
        self.assertEqual(len(store.rows('SELECT * FROM notifications')), 1)
        model = email_reports.build_model(store.rows('SELECT * FROM projects'), [run], changes, [],
                                          run['started'], run['finished'])
        self.assertEqual(model['projects']['dke']['category'], 'changed')
        self.assertEqual(model['projects']['dke']['source_page'], dke.SOURCE)
        await self.run_project('2026-11', quarter=[old])
        await self.run_project('2026-11', monthly=[{**new, 'url':new['url'].replace('456', '987')}], quarter=[old])
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 1)

    async def test_quarter_rollover_and_error_leave_the_comparison_intact(self):
        self.assertEqual(dke.quarter_window('2027-01', -1), {'2026-10', '2026-11', '2026-12'})
        old = publication('DIN 100', '2026-12', '123')
        recovered = publication('DIN 200', '2026-12', '456')
        await self.run_project('2026-12', monthly=[old], quarter=[old])
        failed = await self.run_project('2027-01', error=RuntimeError('DKE-Abruf fehlgeschlagen'))
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(store.rows('SELECT * FROM snapshots')), 1)
        self.assertEqual(store.rows('SELECT * FROM changes'), [])
        run = await self.run_project('2027-01', previous=[old, recovered])
        self.assertEqual(run['status'], 'success')
        self.assertIn('lieferte keine Einträge', run['summary'])
        self.assertEqual(len(store.rows('SELECT * FROM changes')), 1)
        self.assertEqual(store.one('SELECT url FROM changes')['url'], recovered['url'])

    async def test_vornorm_labels_in_database_ui_reports_and_telegram(self):
        old = publication('DIN VDE V 0126-95', '2026-10', '100')
        new = publication('DIN VDE V 0126-95', '2026-11', '101')
        parenthetical = publication('DIN CLC IEC/TS 62271-5 (VDE\u00a0V 0671-5)', '2026-11', '102')
        norm = publication('DIN EN 50286 (VDE 0682-301)', '2026-11', '103')
        self.assertEqual([r['document_type'] for r in (old, new, parenthetical, norm)],
                         ['Vornorm', 'Vornorm', 'Vornorm', 'Norm'])
        self.assertEqual(dke.parse_table(table('month', ('E DIN VDE V 0126-95:2026-11', '104')), 'month'), [])

        await self.run_project('2026-10', monthly=[old], quarter=[old])
        snapshot = store.one('SELECT id,text FROM snapshots')
        previous = json.loads(snapshot['text'].removeprefix(dke.INDEX))
        previous['publications'][0].pop('document_type')
        legacy = dke.INDEX + json.dumps(previous, ensure_ascii=False)
        store.execute('UPDATE snapshots SET text=?,sha256=?,text_sha256=? WHERE id=?',
                      (legacy, store.digest(legacy), store.digest(legacy), snapshot['id']))
        run = await self.run_project('2026-11', monthly=[new, parenthetical, norm], quarter=[old, new, parenthetical, norm])
        self.assertEqual(run['status'], 'success')
        self.assertIn('Darunter 2 Vornormen.', run['summary'])
        self.assertIn('**Vornorm** · [DIN VDE V 0126-95:2026-11]', run['summary'])
        changes = store.rows('SELECT * FROM changes ORDER BY url')
        self.assertEqual([change['kind'] for change in changes], ['Neue Vornorm', 'Neue Vornorm', 'Neue Normausgabe'])
        archived = store.one('SELECT text FROM snapshots WHERE run_id=?', (run['id'],))['text']
        archive = json.loads(archived.removeprefix(dke.INDEX))
        self.assertEqual(sum(r['document_type'] == 'Vornorm' for r in archive['publications']), 3)
        self.assertEqual(attention.summary('dke')['attention_count'], 3)
        telegram = store.one('SELECT message FROM notifications')['message']
        self.assertIn('Vornorm · DIN VDE V 0126-95:2026-11', telegram)
        model = email_reports.build_model(store.rows('SELECT * FROM projects'), [run], changes, [],
                                          run['started'], run['finished'])
        llm = AsyncMock()
        model = await email_reports.compact_headlines(model, llm, 'synthetic')
        llm.assert_not_awaited()
        self.assertIn('davon 2 Vornormen', model['projects']['dke']['message'])
        distribution, _ = email_reports.for_distribution(
            model, {'tag_ids': [], 'project_ids': ['dke'], 'alias': 'QA'})
        for view in (model, distribution):
            report = email_reports.render(view)
            self.assertIn('Vornorm DIN VDE V 0126-95:2026-11', report['text'])
            self.assertIn('Vornorm DIN CLC IEC/TS 62271-5 (VDE V 0671-5):2026-11', report['html'])
