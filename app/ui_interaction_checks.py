"""Real Brave checks of actual templates/assets in a private synthetic origin.

No production routes, database, default browser pages, credentials or model calls.
Every ui.test request is fulfilled inside a new incognito context.
"""
import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit, parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright, expect
from web import breadcrumb_path

ROOT = Path(__file__).parent
ORIGIN = 'https://ui.test'
TAGS = [
    {'id': 'tag-work', 'name': 'Arbeit', 'label': 'Arbeit', 'key': 'arbeit', 'color': '#8b5cf6'},
    {'id': 'tag-private', 'name': 'Privat', 'label': 'Privat', 'key': 'privat', 'color': '#12a88d'},
    {'id': 'tag-norms', 'name': 'Normen', 'label': 'Normen', 'key': 'normen', 'color': '#f0ac30'},
]


def project(pid='alpha', tag=0):
    return {'id': pid, 'name': 'Projekt ' + pid, 'url': 'https://example.org/' + pid,
            'prompt': 'Prüfe ausschließlich die synthetischen Fachinhalte.', 'domains': 'example.org',
            'cron': '15 7 * * *', 'schedule_label': 'Täglich um 07:15 Uhr',
            'use_global_schedule': False, 'color': '#1967d2', 'tags': [TAGS[tag]['name']],
            'tag_keys': [TAGS[tag]['key']], 'tag_chips': [TAGS[tag]], 'enabled': 1,
            'status': 'success', 'attention_count': 0, 'notice_count': 0, 'pending_changes': [],
            'last_checked': None, 'last_success': None, 'next_due': None,
            'active_run': False, 'review_through': '2026-11-05T00:00:00+00:00'}


class FrontendInteractions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(
            os.environ.get('BROWSER_CDP_URL', 'http://172.29.133.20:9223'))
        self.context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 900}, service_workers='block',
            reduced_motion='no-preference')
        self.env = Environment(loader=FileSystemLoader(ROOT / 'templates'),
                               autoescape=select_autoescape(['html']))
        self.env.filters.update({
            'markdown': lambda value: value,
            'status_label': lambda value: {'success': 'Erfolgreich'}.get(value, value or 'Ungeprüft'),
            'relative_age': lambda value: 'vor 2 Stunden',
            'localtime': lambda value: value or 'Noch keine Prüfung',
            'hostname': lambda value: urlsplit(value).hostname,
            'schedule_label': lambda value: value,
            'diff_markup': lambda value: value,
            # Like the other filters, presentation is stubbed in this isolated
            # frontend fixture; retention_ui_checks exercises the real formatter.
            'storage_size': lambda value: 'synthetische Größe',
        })
        self.template = self.env.get_template('index.html')
        self.posts = []
        await self.context.route(ORIGIN + '/**', self.serve)
        self.page = await self.context.new_page()
        self.dialogs = []

        async def accept_dialog(dialog):
            self.dialogs.append(dialog.type)
            await dialog.accept()
        self.page.on('dialog', accept_dialog)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.pw.stop()

    async def serve(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path in ('/icons/discover','/icons/upload'):
            raw=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="#1967d2" /></svg>'
            import hashlib
            data={'mode':'manual' if path.endswith('upload') else 'auto','url':'','media':'image/svg+xml','data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
            return await route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
        if request.method != 'GET':
            self.posts.append({'path': path, 'data': request.post_data or ''})
            return await route.fulfill(status=200, content_type='text/html', body='Synthetic saved.')
        if path.startswith('/static/'):
            asset = (ROOT / path.lstrip('/')).resolve()
            assert asset.is_relative_to((ROOT / 'static').resolve())
            return await route.fulfill(status=200, content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream', body=asset.read_bytes())
        page = 'home'
        if path == '/projects/new': page = 'edit'
        elif path.endswith('/edit'): page = 'edit'
        elif path.endswith('/access'): page = 'site_access'
        elif path.startswith('/projects/'): page = 'project'
        elif path == '/settings': page = 'settings'
        elif path == '/tags': page = 'tags'
        elif path.startswith('/runs/'): page = 'run'
        elif path.startswith('/documents/'): page = 'document'
        current = project()
        current['active_run'] = parse_qs(urlsplit(request.url).query).get('active') == ['1']
        ctx = {'page': page, 'csrf': 'synthetic-csrf-token', 'manual': False, 'busy': False,
               'notice': None, 'settings': {'omlx_url': 'http://192.0.2.1:8000/v1'},
               'global_schedule': {'mode': 'weekly', 'time': '08:30', 'weekday': 1,
                                   'monthday': 1, 'interval': 6},
               'global_schedule_label': 'Dienstags um 08:30 Uhr',
               'schedule': {'mode': 'daily', 'time': '07:15', 'weekday': 0,
                            'monthday': 1, 'interval': 6},
               'tag_options': copy.deepcopy(TAGS), 'tags': copy.deepcopy(TAGS),
               'projects': getattr(self, 'dashboard_projects', [project(), project('beta', 1)]),
               'project': None if path == '/projects/new' else current,
               'latest': None, 'runs': [], 'coverage': {}, 'models': [], 'capabilities': {},
               'run': {'id': 'synthetic-run', 'project_id': 'alpha', 'name': 'Projekt alpha',
                       'status': 'success', 'started': None, 'error': None, 'summary': 'Synthetischer Bericht.'},
               'snapshots': [], 'changes': [], 'events': [], 'evidence_available': False,
               'document': {'id': 'synthetic-document', 'run_id': 'synthetic-run', 'project_id': 'alpha', 'project_name': 'Projekt alpha',
                            'url': 'https://example.org/document.pdf', 'captured': None,
                            'sha256': 'a' * 64, 'file': 'artifacts/synthetic.pdf', 'text': ''},
               # Match the real document route's hash-only view model. Route
               # integration tests separately verify its hash/metadata binding.
               'file_info': {'hash_only': True, 'comparison': 'baseline',
                             'previous_sha256': None, 'previous_id': None, 'metadata': {},
                             'source_url': 'https://example.org/document.pdf'},
               'access_error': None, 'access_saved': None,
               'access': {'supported': True, 'enabled': False, 'configured': False},
               'access_config': {'origin': 'https://www.elektro.net', 'provider': 'elektro_subscription',
                                 'login_url': 'https://www.elektro.net/login', 'submit_url': '',
                                 'form_selector': 'form', 'username_selector': 'input[name=email]',
                                 'password_selector': 'input[type=password]', 'submit_selector': 'button[type=submit]',
                                 'success_selector': 'a.logout', 'consent_selector': '', 'close_selector': ''}}
        ctx['breadcrumbs'] = breadcrumb_path(page, ctx)
        return await route.fulfill(status=200, content_type='text/html',
                                   body=self.template.render(**ctx),
                                   headers={'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; form-action 'self'"})

    async def open(self, path='/'):
        await self.page.goto(ORIGIN + path, wait_until='networkidle')

    async def test_dashboard_sort_options_filters_and_live_updates(self):
        alpha, beta, gamma, delta = project(), project('beta', 1), project('gamma'), project('delta', 1)
        alpha.update(last_checked='2026-11-03T10:00:00Z', last_change='2026-11-02T10:00:00Z',
                     last_success='2026-11-03T10:00:00Z')
        beta.update(status='queued', last_checked='2026-11-04T10:00:00Z', last_change='2026-11-01T10:00:00Z')
        gamma.update(status='failed', last_checked='2026-11-01T10:00:00Z')
        delta.update(notice_count=1)
        self.dashboard_projects = [alpha, beta, gamma, delta]

        async def status(route):
            await route.fulfill(json={'projects':self.dashboard_projects})

        await self.page.route('**/api/project-status', status)
        await self.open()
        sort = self.page.get_by_label('Projekte sortieren')
        cards = self.page.locator('[data-project]:visible')

        async def order():
            return await cards.evaluate_all('cards => cards.map(card => card.dataset.projectId)')

        self.assertEqual(await sort.input_value(), 'lastChecked')
        self.assertEqual(await order(), ['gamma', 'beta', 'alpha', 'delta'])
        await sort.select_option('lastChange')
        self.assertEqual(await order(), ['gamma', 'alpha', 'beta', 'delta'])
        await self.page.reload(wait_until='networkidle')
        self.assertEqual(await sort.input_value(), 'lastChange')
        await sort.select_option('lastSuccess')
        self.assertEqual(await order(), ['gamma', 'alpha', 'beta', 'delta'])
        await sort.select_option('name')
        self.assertEqual(await order(), ['gamma', 'alpha', 'beta', 'delta'])
        await self.page.locator('[data-tag-filter]').select_option('privat')
        self.assertEqual(await order(), ['beta', 'delta'])
        await self.page.locator('[data-tag-filter]').select_option('')
        await sort.select_option('lastChecked')
        alpha.update(status='running')
        gamma.update(status='success', last_checked='2026-11-06T10:00:00Z')
        delta.update(status='partial', last_checked='2026-11-05T10:00:00Z')
        await self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await expect(cards.first).to_have_attribute('data-project-id', 'alpha')
        self.assertEqual(await order(), ['alpha', 'delta', 'gamma', 'beta'])

        for key, expected in [('error', ['delta']), ('notices', ['delta']),
                              ('active', ['delta', 'gamma', 'beta', 'alpha'])]:
            chip = self.page.locator(f'[data-filter={key}]')
            await chip.click()
            await expect(chip).to_have_attribute('aria-pressed', 'true')
            self.assertEqual(await order(), expected)
            await chip.click()
            await expect(self.page.locator('[data-filter=all]')).to_have_attribute('aria-pressed', 'true')
            self.assertEqual(await order(), ['alpha', 'delta', 'gamma', 'beta'])

        tag = self.page.locator('[data-tag-choice=arbeit]')
        await tag.click()
        self.assertEqual(await order(), ['gamma', 'alpha'])
        await tag.click()
        await expect(self.page.locator('[data-tag-choice=""]')).to_have_attribute('aria-pressed', 'true')
        self.assertEqual(await order(), ['alpha', 'delta', 'gamma', 'beta'])

        search = self.page.locator('[data-project-search]')
        await search.fill('Projekt')
        self.assertEqual(await order(), ['delta', 'gamma', 'beta', 'alpha'])
        await search.clear()
        self.assertEqual(await order(), ['alpha', 'delta', 'gamma', 'beta'])
        await self.page.reload(wait_until='networkidle')
        self.assertEqual(await order(), ['alpha', 'delta', 'gamma', 'beta'])

        alpha.update(status='success', last_checked='2026-11-07T10:00:00Z')
        await self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await expect(cards.first).to_have_attribute('data-project-id', 'delta')
        self.assertEqual(await order(), ['delta', 'alpha', 'gamma', 'beta'])

    async def test_01_icon_actions_breadcrumbs_and_keyboard_preserve_typing(self):
        await self.open()
        self.assertEqual(await self.page.locator('.breadcrumbs').count(), 0)
        run = self.page.locator('.project-actions form[action$="/run"] button').first
        self.assertEqual((await run.inner_text()).strip(), '')
        self.assertTrue(await run.get_attribute('aria-label'))
        self.assertEqual(await run.locator('use').get_attribute('href'), '#i-refresh')
        eye = self.page.locator('.project-actions a[title="Projekt ansehen"]').first
        self.assertEqual(await eye.locator('use').get_attribute('href'), '#i-eye')
        await eye.click()
        self.assertEqual(urlsplit(self.page.url).path, '/projects/alpha')
        await self.page.locator('body.page-project').wait_for(state='visible')
        await self.page.keyboard.press('Escape')
        await self.page.wait_for_url(ORIGIN + '/')
        await self.open('/projects/alpha/edit')
        name = self.page.locator('[name=name]')
        await name.fill('Bearbeitet')
        await name.press('Backspace')
        self.assertEqual(await name.input_value(), 'Bearbeite')
        self.assertEqual(urlsplit(self.page.url).path, '/projects/alpha/edit')
        await name.press('Escape')
        self.assertEqual(urlsplit(self.page.url).path, '/projects/alpha/edit')
        await self.page.locator('h1').click()
        await self.page.keyboard.press('Backspace')
        await self.page.wait_for_url(ORIGIN + '/')
        self.assertIn('beforeunload', self.dialogs)
        await self.open('/documents/synthetic-document')
        self.assertEqual(await self.page.locator('.breadcrumbs a').evaluate_all('links => links.map(link => link.pathname)'),
                         ['/', '/projects/alpha', '/runs/synthetic-run', '/documents/synthetic-document'])
        await self.page.locator('.breadcrumbs a[href="/runs/synthetic-run"]').click()
        await self.page.wait_for_url(ORIGIN + '/runs/synthetic-run')
        await self.page.locator('.breadcrumbs a[href="/projects/alpha"]').click()
        await self.page.wait_for_url(ORIGIN + '/projects/alpha')
        for path in ('/runs/synthetic-run', '/documents/synthetic-document'):
            await self.open(path)
            await self.page.keyboard.press('Escape')
            await self.page.wait_for_url(ORIGIN + '/')

    async def test_02_schedule_controls_match_the_selected_mode_and_global_checkbox(self):
        await self.open('/projects/alpha/edit')
        editor = self.page.locator('[data-schedule-editor]')
        toggle = self.page.locator('[name=use_global_schedule]')
        await toggle.check()
        self.assertFalse(await self.page.locator('[data-custom-schedule]').is_visible())
        await toggle.uncheck()
        self.assertTrue(await editor.is_visible())
        await editor.locator('[name=schedule_mode]').select_option('weekly')
        self.assertTrue(await editor.locator('[name=schedule_weekday]').is_visible())
        self.assertFalse(await editor.locator('[name=schedule_monthday]').is_visible())
        await editor.locator('[name=schedule_mode]').select_option('monthly')
        await editor.locator('[name=schedule_monthday]').select_option('31')
        self.assertFalse(await editor.locator('[name=schedule_weekday]').is_enabled())
        self.assertTrue(await editor.locator('[name=schedule_monthday]').is_visible())
        await editor.locator('[name=schedule_mode]').select_option('interval')
        await editor.locator('[name=schedule_interval]').select_option('7')
        data = await self.page.locator('.project-form').evaluate('f => [...new FormData(f).entries()]')
        self.assertIn(['schedule_interval', '7'], data)
        self.assertFalse(any(key == 'schedule_time' for key, value in data))
        self.assertEqual(await self.page.locator('[name=cron]').count(), 0)

    async def test_03_existing_tag_dropdown_chips_and_query_filter(self):
        await self.open('/?tag=arbeit')
        self.assertEqual(await self.page.locator('[data-project]:visible').count(), 1)
        self.assertEqual(await self.page.locator('[data-project]:visible').get_attribute('data-project-id'), 'alpha')
        self.assertEqual(await self.page.locator('.sidebar-tags .color-tag').count(), 0)
        await self.page.locator('[data-tag-choice=privat]').click()
        self.assertEqual(await self.page.locator('[data-project]:visible').get_attribute('data-project-id'), 'beta')
        await self.open('/projects/alpha/edit')
        self.assertEqual(await self.page.locator('input[name=tags]').count(), 0)
        dropdown = self.page.locator('[data-tag-dropdown]')
        await dropdown.locator('summary').click()
        await dropdown.locator('[value=tag-private]').check()
        self.assertIn('Privat', await dropdown.locator('summary').inner_text())
        await self.page.keyboard.press('Escape')
        self.assertFalse(await dropdown.evaluate('d => d.open'))
        self.assertEqual(urlsplit(self.page.url).path, '/projects/alpha/edit')
        self.assertTrue(await self.page.locator('[name=tags_present]').count())

    async def test_04_central_tag_color_fields_are_independent_and_delete_requires_confirmation(self):
        await self.open('/tags')
        rows = self.page.locator('.tag-entry')
        self.assertEqual(await rows.count(), 3)
        second = rows.nth(1)
        await second.locator('[data-color-text]').fill('#102030')
        self.assertEqual(await second.locator('[data-color-picker]').input_value(), '#102030')
        self.assertEqual(await rows.first.locator('[data-color-picker]').input_value(), TAGS[0]['color'])
        await self.open('/projects/alpha/edit')
        delete = self.page.locator('form[action$="/delete"]')
        self.assertEqual(await delete.locator('[name=confirm]').get_attribute('required'), '')
        self.assertFalse(await delete.locator('[name=confirm]').is_checked())
        await self.open('/projects/alpha/edit?active=1')
        self.assertTrue(await self.page.locator('form[action$="/delete"] button').is_disabled())

    async def test_05_font_range_persists_and_dark_surfaces_are_neutral(self):
        await self.open('/settings')
        selects = self.page.locator('[data-ui-size]')
        self.assertEqual(await selects.first.locator('option').first.get_attribute('value'), '50')
        self.assertEqual(await selects.first.locator('option').last.get_attribute('value'), '150')
        await selects.first.select_option('50')
        self.assertEqual(await self.page.evaluate('getComputedStyle(document.documentElement).fontSize'), '8px')
        await selects.first.select_option('150')
        self.assertEqual(await selects.last.input_value(), '150')
        await self.page.reload(wait_until='networkidle')
        self.assertEqual(await self.page.evaluate('getComputedStyle(document.documentElement).fontSize'), '24px')
        await self.page.locator('[data-theme-toggle]').first.click()
        self.assertEqual(await self.page.evaluate('getComputedStyle(document.body).backgroundColor'), 'rgb(30, 30, 30)')
        theme_button = self.page.locator('[data-theme-toggle]').first
        self.assertGreater(await theme_button.locator('.ripple').count(), 0)
        # Screenshot animation disabling fast-forwards this transient effect.
        # The finished ripple must remain clipped to its own control.
        await theme_button.evaluate("e => e.querySelectorAll('.ripple').forEach(r => r.getAnimations().forEach(a => a.finish()))")
        widths = await self.page.evaluate('({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})')
        self.assertLessEqual(widths['content'], widths['viewport'])
        logo_version=hashlib.sha256((ROOT/'static/app-logo.png').read_bytes()).hexdigest()[:12]
        self.assertEqual(await self.page.locator('.brand-mark img').get_attribute('src'), '/static/app-logo.png?v='+logo_version)
        self.assertIn('brave-mark', await self.page.locator('.nav-logo').get_attribute('class'))

    async def test_06_hover_press_and_reduced_motion_use_the_actual_styles(self):
        await self.open()
        button = self.page.locator('.analyze-button').first
        await button.hover()
        await self.page.wait_for_timeout(260)
        matrix = await button.evaluate('e => getComputedStyle(e).transform')
        self.assertAlmostEqual(float(re.findall(r'-?[0-9.]+', matrix)[0]), 1.08, delta=.005)
        card_matrix = await self.page.locator('[data-project]').first.evaluate('e => getComputedStyle(e).transform')
        self.assertAlmostEqual(float(re.findall(r'-?[0-9.]+', card_matrix)[0]), 1.012, delta=.005)
        await self.page.mouse.down()
        await self.page.wait_for_timeout(260)
        pressed = await button.evaluate('e => getComputedStyle(e).transform')
        self.assertAlmostEqual(float(re.findall(r'-?[0-9.]+', pressed)[-1]), 2, delta=.1)
        # Cancel the click by moving outside the button before release.
        await self.page.mouse.move(1240, 860)
        await self.page.mouse.up()
        await self.page.emulate_media(reduced_motion='reduce')
        await button.hover()
        await self.page.wait_for_timeout(200)
        self.assertEqual(await self.page.locator('html').get_attribute('data-motion'), 'on')
        matrix = await button.evaluate('e => getComputedStyle(e).transform')
        self.assertAlmostEqual(float(re.findall(r'-?[0-9.]+', matrix)[0]), 1.08, delta=.005)

    async def test_07_access_provider_hides_and_excludes_the_other_providers_fields(self):
        await self.open('/projects/alpha/access')
        form = self.page.locator('[data-site-access-form]')
        generic = form.locator('[data-access-fields=generic_form]')
        specific = form.locator('[data-access-fields=elektro_subscription]')
        self.assertFalse(await generic.is_visible())
        self.assertTrue(await generic.evaluate('e => e.disabled'))
        await form.locator('[name=abonummer]').fill('123456789')
        await form.locator('[name=postcode]').fill('12345')
        await form.locator('[data-access-provider]').select_option('generic_form')
        self.assertTrue(await generic.is_visible())
        self.assertFalse(await specific.is_visible())
        data = await form.evaluate('f => [...new FormData(f).entries()]')
        self.assertFalse(any(key in ('abonummer', 'postcode') for key, value in data))
        self.assertTrue(any(key == 'username' for key, value in data))


    async def test_08_color_dice_contrast_and_no_accidental_submit(self):
        await self.open('/tags')
        button=self.page.locator('[data-random-color]').first
        colors=[]
        for _ in range(30):
            await button.click()
            colors.append(await self.page.locator('[data-color-text]').first.input_value())
        def luminance(rgb):
            channels=[c/255/12.92 if c/255<=.04045 else ((c/255+.055)/1.055)**2.4 for c in rgb]
            return sum(c*w for c,w in zip(channels,[.2126,.7152,.0722]))
        dark=luminance([38,38,38])
        for value in colors:
            light=luminance([int(value[i:i+2],16) for i in (1,3,5)])
            self.assertGreaterEqual((1.05)/(light+.05),3)
            self.assertGreaterEqual((light+.05)/(dark+.05),3)
        self.assertGreater(len(set(colors)),20);self.assertEqual(self.posts,[])
        await self.open('/projects/alpha/edit');self.assertEqual(await self.page.locator('[data-random-color]').count(),1)

    async def test_09_favicon_download_upload_and_clear_preview(self):
        await self.open('/projects/new')
        await self.page.locator('#url').fill('https://example.org/')
        await self.page.locator('[data-icon-fetch]').click()
        await self.page.wait_for_function("() => document.querySelector('[data-icon-payload]').value.length>0")
        self.assertEqual(json.loads(await self.page.locator('[data-icon-payload]').input_value())['mode'],'auto')
        await self.page.wait_for_function("() => document.querySelector('[data-icon-preview] img')?.naturalWidth>0")
        await self.page.locator('[data-icon-file]').set_input_files({'name':'my.svg','mimeType':'image/svg+xml','buffer':b'<svg xmlns="http://www.w3.org/2000/svg"/>'})
        await self.page.wait_for_function("() => JSON.parse(document.querySelector('[data-icon-payload]').value).mode==='manual'")
        await self.page.locator('[data-icon-clear]').click()
        self.assertEqual(await self.page.locator('[data-icon-action]').input_value(),'clear')
        self.assertEqual(await self.page.locator('[data-icon-preview] img').count(),0)

    async def test_10_brand_without_border_browser_without_globe_and_150_layout(self):
        await self.open('/browser')
        self.assertEqual(await self.page.locator('.brand-mark').evaluate('e=>getComputedStyle(e).borderTopWidth'),'0px')
        self.assertEqual(await self.page.locator('.page-browser .panel-heading .icon-wrap').count(),0)
        self.assertIn('brave-mono.svg',await self.page.locator('.brave-mark').evaluate('e=>getComputedStyle(e).maskImage'))
        await self.open('/projects/alpha/edit')
        await self.page.locator('[data-ui-size]').first.select_option('150')
        sizes=await self.page.evaluate('({a:document.documentElement.clientWidth,b:document.documentElement.scrollWidth})')
        self.assertLessEqual(sizes['b'],sizes['a'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
