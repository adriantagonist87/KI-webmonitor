"""Bounded capture of trusted clicks in one temporary Brave page."""
import asyncio
from contextlib import suppress
import json
from urllib.parse import urlsplit

from playwright.async_api import Error, async_playwright

import browser_endpoint
import browser_mode
import navigation_macros as macros
import site_access
import site_privacy
import site_safety
import store
from visual_browser import mobile_window

RECORD_SECONDS = 10 * 60
WORLD = 'kiwm-navigation-recording'
BINDING = 'kiwmRecordNavigation'
SCRIPT = r"""
origin => {
    if (globalThis.kiwmRecorderInstalled) return;
    globalThis.kiwmRecorderInstalled = true;
    document.addEventListener('click', event => {
        if (window !== window.top || location.origin !== origin) return;
        if (!event.isTrusted || event.button !== 0 || event.ctrlKey || event.altKey || event.shiftKey || event.metaKey) return;
        const item = event.target instanceof Element && event.target.closest('a,button,[role="button"],[role="tab"],summary');
        if (!item || item.form || item.closest('form,[contenteditable],input,textarea,select') || item.matches(':disabled,[aria-disabled="true"]')) return;
        if (item.closest('.nav-account,.account,.user-account,[data-account],[data-login],[data-auth]')) return;
        if (item.target && item.target !== '_self') return;
        if ([...document.querySelectorAll('input[type="password"],input[autocomplete="one-time-code"]')]
                .some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')) return;
        const label = (item.getAttribute('aria-label') || item.innerText || item.title || '').trim().replace(/\s+/g, ' ');
        const anchor = label ? '' : [item.id,item.getAttribute('aria-controls')].find(s => /^[A-Za-z][A-Za-z0-9_:-]{0,79}$/.test(s || '')) || '';
        const role = item.getAttribute('role') === 'tab' ? 'tab' : item.tagName === 'A' && item.href ? 'link' : 'button';
        const expanded = item.getAttribute('aria-expanded');
        if (item.href && new URL(item.href,location.href).origin !== origin) return;
        if ((!label && !anchor) || label.length > 160) return;
        kiwmRecordNavigation(JSON.stringify({at:location.href,role,label,anchor,
            expanded:expanded === 'true' ? false : expanded === 'false' ? true : null,optional:false}));
    }, true);
}
"""


class Recording:
    def __init__(self, project, cdp_url):
        self.project = project
        self.start = macros.location(macros.entry(project), project['url'])
        self.cdp_url = cdp_url
        self.steps = []
        self.playwright = self.browser = self.page = self.cdp = None
        self.timeout = None
        self.accepting = False
        self.install_task = None
        self.guard = site_safety.Guard(project['id'], project['url'], site_safety.PROFILES[site_safety.profile(project['id'])])

    def accept(self, event):
        payload = event.get('payload', '')
        if not self.accepting or self.guard.pending or event.get('name') != BINDING or len(self.steps) >= macros.MAX_STEPS or len(payload) > 3500:
            return
        try:
            item = macros.step(json.loads(payload), self.start)
            if site_safety.classify(item['at']):
                return
            token = site_privacy.bind(site_access.get_secret(self.project['id']), site_access.public_config(self.project['id']))
            try:
                if site_privacy.redact_payload(item) != item:
                    return
            finally:
                site_privacy.reset(token)
        except (ValueError, TypeError, KeyError, site_access.AccessError):
            return
        self.steps.append(item)

    async def install(self):
        frame = (await self.cdp.send('Page.getFrameTree'))['frameTree']['frame']
        context = await self.cdp.send('Page.createIsolatedWorld', {'frameId': frame['id'], 'worldName': WORLD})
        result = await self.cdp.send('Runtime.evaluate', {'contextId': context['executionContextId'],
                                                        'expression': self.source})
        if 'exceptionDetails' in result:
            raise ValueError('Brave konnte den Klickrekorder nicht starten.')

    def install_after_navigation(self, *_):
        if self.accepting:
            if self.install_task and not self.install_task.done():
                self.install_task.cancel()
            self.install_task = asyncio.create_task(self._install_after_navigation())

    async def _install_after_navigation(self):
        # CDP document-start scripts can be lost when Chromium swaps the
        # renderer. Attach to the new document as well, in the same private world.
        with suppress(Error, ValueError):
            await self.install()

    async def open(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(browser_endpoint.resolve(self.cdp_url), timeout=10000)
        context = self.browser.contexts[0]
        self.page = (await mobile_window(self.browser, context) if browser_mode.get(self.project)['mode'] == 'visual_mobile'
                     else await context.new_page())
        self.page.on('response', self.guard.note_response)
        self.cdp = await context.new_cdp_session(self.page)
        await self.cdp.send('Runtime.enable')
        await self.cdp.send('Runtime.addBinding', {'name': BINDING, 'executionContextName': WORLD})
        self.cdp.on('Runtime.bindingCalled', self.accept)
        origin = urlsplit(self.start)
        self.source = f'({SCRIPT})({json.dumps(origin.scheme + "://" + origin.netloc)})'
        await self.cdp.send('Page.addScriptToEvaluateOnNewDocument', {'source': self.source, 'worldName': WORLD})
        response = await self.page.goto(self.start, wait_until='domcontentloaded', timeout=30000)
        await self.page.bring_to_front()
        self.guard.raise_pending()
        await site_safety.check_page(self.page, response)
        await self.install()
        self.page.on('domcontentloaded', self.install_after_navigation)
        self.accepting = True

    async def result(self):
        self.accepting = False
        self.guard.raise_pending()
        await site_safety.check_page(self.page)
        if await site_privacy.contains_auth_form(self.page):
            raise ValueError('Bitte zuerst im Brave-Browser anmelden und erneut aufnehmen.')
        if not self.steps:
            raise ValueError('Keine sicheren Navigationsklicks aufgenommen.')
        return {'id': store.uid(), 'start': self.start, 'target': macros.location(self.page.url, self.start), 'steps': self.steps}

    async def close(self):
        self.accepting = False
        if self.timeout and self.timeout is not asyncio.current_task():
            self.timeout.cancel()
        if self.install_task:
            self.install_task.cancel()
            await asyncio.gather(self.install_task, return_exceptions=True)
        try:
            if self.page:
                with suppress(Error):
                    await self.page.close()
            if self.browser:
                with suppress(Error):
                    await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()


class Recorder:
    def __init__(self):
        self.active = self.draft = None
        self.control = asyncio.Lock()

    async def _end(self, busy):
        try:
            await self.active.close()
        finally:
            self.active = None
            busy.release()

    async def _expire(self, recording, busy):
        await asyncio.sleep(RECORD_SECONDS)
        async with self.control:
            if self.active is recording:
                await self._end(busy)

    async def start(self, project, cdp_url, busy):
        async with self.control:
            if self.active or busy.locked() or store.manual():
                raise ValueError('Laufende Prüfung oder manuellen Browsermodus zuerst beenden.')
            site_safety.respect_wait(project['id'])
            recording = Recording(project, cdp_url)
            await busy.acquire()
            self.active = recording
            try:
                await recording.open()
                recording.timeout = asyncio.create_task(self._expire(recording, busy))
                self.draft = None
            except BaseException:
                await self._end(busy)
                raise

    async def stop(self, pid, busy, *, keep):
        async with self.control:
            if not self.active or self.active.project['id'] != pid:
                raise ValueError('Die Aufnahme ist nicht mehr aktiv.')
            try:
                if keep:
                    self.draft = (pid, await self.active.result())
            finally:
                await self._end(busy)

    def draft_for(self, pid):
        return self.draft[1] if self.draft and self.draft[0] == pid else None

    async def shutdown(self, busy):
        async with self.control:
            if self.active:
                await self._end(busy)
