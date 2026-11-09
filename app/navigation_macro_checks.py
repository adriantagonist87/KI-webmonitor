"""Macro validation, transfer and opt-in tests against an isolated Brave."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

import browser_mode
import engine
import navigation_macros as macros
import site_safety
import store
import transfer
import web


def add_project(pid, url, task='Lies die sichtbare Prüfkennung unter Normen. Nenne sie exakt im Bericht.'):
    store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,0,?,?)',
                  (pid, 'Mobilprobe '+pid, url, task, urlsplit(url).hostname, '0 6 * * *', store.now(), store.now()))
    p = store.one('SELECT * FROM projects WHERE id=?', (pid,))
    browser_mode.save(p, {'mode': 'visual_mobile', 'entry_url': ''})
    return p


def sample_macro(start):
    item = {'at': start, 'role': 'button', 'label': 'Menü', 'anchor': '', 'expanded': True, 'optional': False}
    return {'active': 'a'*32, 'macros': [
        {'id': 'a'*32, 'name': 'Zur Ansicht', 'start': start, 'target': start, 'steps': [item]}]}


class NavigationMacroChecks(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        old = store.DATA
        store.DATA = Path(self.work.name)
        self.addCleanup(setattr, store, 'DATA', old)
        store.init()
        self.project = add_project('demo', 'https://example.org/start')

    def test_legacy_project_has_no_macro(self):
        self.assertIsNone(macros.active(self.project))

    def test_blocks_other_origins_queries_and_form_actions(self):
        start = self.project['url']
        for url in ('https://evil.invalid/normen', 'https://example.org:444/normen',
                    'https://example.org/normen?token=secret', 'https://example.org/%6cogout'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                macros.location(url, start)
        for label in ('Passwort ändern', 'Anmelden', 'Kaufen'):
            value = sample_macro(start)
            value['macros'][0]['steps'][0]['label'] = label
            with self.assertRaises(ValueError):
                macros.validate(value, start)

    def test_optional_click_is_limited_to_notice(self):
        value = sample_macro(self.project['url'])
        item = value['macros'][0]['steps'][0]
        item['optional'] = True
        with self.assertRaises(ValueError):
            macros.validate(value, self.project['url'])
        item.update(label='Verstanden', expanded=None)
        self.assertTrue(macros.validate(value, self.project['url'])['macros'][0]['steps'][0]['optional'])

    def test_macros_round_trip_with_configuration(self):
        value = sample_macro(self.project['url'])
        macros.save(self.project, value)
        export = store.DATA/'configuration.json'
        transfer.export_to(export)
        data = transfer.read_package(export)
        store.execute('DELETE FROM state WHERE key=?', (macros.KEY+self.project['id'],))
        transfer.import_package(export, data)
        self.assertEqual(macros.get(self.project), value)


STYLE = """<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font:20px sans-serif;margin:28px}button,a{display:inline-block;padding:14px}
[hidden]{display:none} #mobile-toggle::before{content:'☰'}</style>"""


class MobileFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.server.mutations += 1
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        variant = self.server.variant
        if self.path == '/normen':
            body = f'<main><article><h1>Normen</h1><p>Die Prüfkennung lautet {variant}.</p><p>Stand November 2026.</p></article></main>'
        else:
            symbol = variant == 'MQC53'
            expanded = self.server.replay and variant == 'MQB47'
            name = '' if symbol else 'Menü'
            ident = 'mobile-toggle' if symbol else 'main-toggle' if self.server.replay else 'recorded-toggle'
            notice = ('<dialog open><p>Ein Hinweis zum neuen Menü.</p><button onclick="this.parentElement.close()">Verstanden</button></dialog>'
                      if variant == 'MQB47' and not self.server.replay else '')
            body = f"""<header><button id="{ident}" aria-expanded="{str(expanded).lower()}" aria-controls="navigation"
                onclick="const n=document.getElementById('navigation'); n.hidden=!n.hidden;
                this.setAttribute('aria-expanded', String(!n.hidden)); fetch('/mutation',{{method:'POST'}}).catch(()=>{{}})">{name}</button></header>
                <nav id="navigation" {'hidden' if not expanded else ''}><a href="/normen">Normen</a><a href="/iec">IEC Normen</a></nav>
                <main><h1>Normenbibliothek</h1><p>Mobile Ansicht</p></main>{notice}"""
        self.wfile.write((STYLE + body).encode())


@unittest.skipUnless(os.environ.get('KIWM_MACRO_QA_BROWSER') == '1', 'Isolated Brave required')
class NativeMacroChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin = os.environ['KIWM_MACRO_FIXTURE']
        if not self.origin.startswith('http://172.29.147.40:') or not engine.BROWSER_CDP_URL.startswith('http://172.29.147.20:'):
            raise RuntimeError('The disposable macro test network is required.')
        self.work = tempfile.TemporaryDirectory(dir='/data')
        self.addCleanup(self.work.cleanup)
        old = store.DATA
        store.DATA = Path(self.work.name)
        self.addCleanup(setattr, store, 'DATA', old)
        self.addCleanup(setattr, engine, 'DATA', engine.DATA)
        engine.DATA = store.DATA
        store.init()
        self.server = ThreadingHTTPServer(('0.0.0.0', 8000), MobileFixture)
        self.server.mutations = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addAsyncCleanup(web.macro_recorder.shutdown, engine.BUSY)

    async def exercise(self, variant):
        def alert(response):
            item = BeautifulSoup(response.text, 'html.parser').select_one('[role="alert"]')
            return item.get_text(' ', strip=True) if item else f'HTTP {response.status_code}'

        self.server.variant, self.server.replay = variant, False
        p = add_project(variant, self.origin+'/start')
        site_safety.save_profile(p['id'], 'standard')
        path = f"/projects/{p['id']}/macros"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url='http://testserver') as client:
            page = await client.get(path)
            self.assertIn('Aufnahme starten', page.text)
            csrf = client.cookies['kiwm_csrf']
            response = await client.post(path, data={'action': 'start', 'csrf': csrf})
            self.assertEqual(response.status_code, 303, alert(response))
            recording = web.macro_recorder.active
            brave = recording.page
            self.assertLessEqual(await brave.evaluate('innerWidth'), 600)
            self.assertEqual(await brave.evaluate('typeof window.kiwmRecordNavigation'), 'undefined')
            if variant == 'MQB47':
                await brave.get_by_role('button', name='Verstanden').click()
            menu = brave.locator('#mobile-toggle') if variant == 'MQC53' else brave.get_by_role('button', name='Menü')
            await menu.click()
            await brave.get_by_role('link', name='Normen', exact=True).click()
            await brave.wait_for_url(self.origin+'/normen')
            response = await client.post(path, data={'action': 'stop', 'csrf': csrf})
            self.assertEqual(response.status_code, 303, f'{alert(response)} · {len(recording.steps)} gespeicherte Klicks')
            draft = web.macro_recorder.draft_for(p['id'])
            self.assertEqual(draft['target'], self.origin+'/normen')
            self.assertEqual([item['label'] for item in draft['steps']],
                             ['Verstanden','Menü','Normen'] if variant=='MQB47' else ['' if variant=='MQC53' else 'Menü','Normen'])
            response = await client.post(path, data={'action':'save','csrf':csrf,'draft':draft['id'],
                                                    'name':'Mobile Navigation','optional':'0' if variant=='MQB47' else ''})
            self.assertEqual(response.status_code, 303, alert(response))
        self.server.replay = True
        before = self.server.mutations
        rid = store.enqueue(p['id'])
        self.assertTrue(rid)
        await asyncio.wait_for(engine.run_job(p, rid), timeout=480)
        row = store.one('SELECT status,summary,error FROM runs WHERE id=?', (rid,))
        self.assertEqual(row['status'], 'success', str(row['error']))
        self.assertIn(variant, row['summary'])
        self.assertEqual(self.server.mutations, before)
        result = json.loads((store.DATA/'artifacts'/rid/'evidence.json').read_text())
        self.assertTrue(result['navigation_macro']['target_reached'])
        self.assertEqual(result['observations'][0]['url'], self.origin+'/normen')
        if variant=='MQB47':
            self.assertEqual([item['result'] for item in result['navigation_macro']['steps'][:2]],
                             ['Hinweis nicht sichtbar','Bereits im aufgenommenen Zustand'])
        print(f'LIVE_QA {variant}: Brave-Aufnahme, Replay und oMLX-Bericht erfolgreich.', flush=True)

    async def test_labelled_mobile_menu(self):
        await self.exercise('MQA31')

    async def test_optional_notice_and_already_open_menu(self):
        await self.exercise('MQB47')

    async def test_unlabelled_menu_icon(self):
        await self.exercise('MQC53')
