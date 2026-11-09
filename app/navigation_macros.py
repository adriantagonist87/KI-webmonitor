"""Project-owned recordings of visible, same-origin navigation."""
import json
import re
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout

import browser_mode
from browser_actions import DISMISS_LABELS, UNSAFE_LABEL, normalize_label, safe_navigation_url
import site_privacy
import site_safety
import store

KEY = 'navigation_macros:'
MAX_MACROS = 8
MAX_STEPS = 20
CONTROL = re.compile(r'[A-Za-z][A-Za-z0-9_:-]{0,79}\Z')
ROLES = {'button', 'link', 'tab'}


def entry(project, connection=None):
    return browser_mode.get(project, connection)['entry_url'] or project['url']


def location(value, start):
    if not isinstance(value, str) or len(value) > 2048 or not safe_navigation_url(start, value):
        raise ValueError('Makros bleiben auf der freigegebenen Start-Website.')
    url = urlsplit(value)
    if url.scheme not in ('https', 'http') or not url.hostname or url.query or url.username or url.password:
        raise ValueError('Makro-Adressen müssen ohne URL-Parameter und Zugangsdaten auskommen.')
    if url.fragment and not CONTROL.fullmatch(url.fragment):
        raise ValueError('Makro-Ziel hat keinen stabilen Seitenanker.')
    return url._replace(path=url.path or '/').geturl()


def step(value, start):
    if not isinstance(value, dict) or set(value) != {'at', 'role', 'label', 'anchor', 'expanded', 'optional'}:
        raise ValueError('Ungültiger Makroschritt.')
    label, anchor = value['label'], value['anchor']
    if (not isinstance(label, str) or len(label) > 160 or any(ord(c) < 32 for c in label)
            or UNSAFE_LABEL.search(label) or not isinstance(anchor, str) or (anchor and not CONTROL.fullmatch(anchor))
            or (not label and not anchor) or not isinstance(value['role'], str) or value['role'] not in ROLES):
        raise ValueError('Makros benötigen einen sichtbaren und eindeutig bestimmbaren Navigationsknopf.')
    label = ' '.join(label.split())
    expanded, optional = value['expanded'], value['optional']
    if (expanded is not None and type(expanded) is not bool) or type(optional) is not bool:
        raise ValueError('Ungültiger Makroschritt.')
    if optional and (not label or normalize_label(label) not in DISMISS_LABELS):
        raise ValueError('Nur bestätigte Hinweisdialoge können optional sein.')
    return {'at': location(value['at'], start), 'role': value['role'], 'label': label, 'anchor': anchor,
            'expanded': expanded, 'optional': optional}


def validate(value, start):
    if not isinstance(value, dict) or set(value) != {'active', 'macros'} or not isinstance(value['macros'], list) or len(value['macros']) > MAX_MACROS:
        raise ValueError('Ungültige Projektmakros.')
    macros, seen = [], set()
    for macro in value['macros']:
        if not isinstance(macro, dict) or set(macro) != {'id', 'name', 'start', 'target', 'steps'}:
            raise ValueError('Ungültiges Makro.')
        ident, name = macro['id'], macro['name']
        if (not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident) or ident in seen
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name)):
            raise ValueError('Makro benötigt einen eindeutigen Namen und eine gültige ID.')
        if location(macro['start'], start) != location(start, start):
            raise ValueError('Die Aufnahme gehört zu einer anderen Startseite.')
        if not isinstance(macro['steps'], list) or not 1 <= len(macro['steps']) <= MAX_STEPS:
            raise ValueError('Ein Makro benötigt 1 bis 20 sichere Klicks.')
        steps = [step(item, start) for item in macro['steps']]
        macros.append({'id': ident, 'name': name.strip(), 'start': location(start, start),
                       'target': location(macro['target'], start), 'steps': steps})
        seen.add(ident)
    if not isinstance(value['active'], str) or value['active'] and value['active'] not in seen:
        raise ValueError('Das ausgewählte Makro fehlt.')
    return {'active': value['active'], 'macros': macros}


def get(project, connection=None):
    sql, args = 'SELECT value FROM state WHERE key=?', (KEY + project['id'],)
    row = connection.execute(sql, args).fetchone() if connection else store.one(sql, args)
    if row:
        try:
            return validate(json.loads(row['value']), entry(project, connection))
        except (ValueError, TypeError, KeyError):
            pass
    return {'active': '', 'macros': []}


def save(project, value, connection=None):
    if connection is None:
        with store.db() as connection:
            return save(project, value, connection)
    clean = validate(value, entry(project, connection))
    connection.execute('INSERT OR REPLACE INTO state VALUES(?,?)',
                       (KEY + project['id'], json.dumps(clean, ensure_ascii=False)))


def active(project):
    value = get(project)
    return next((item for item in value['macros'] if item['id'] == value['active']), None)


def locator(page, item):
    if item['label']:
        return page.get_by_role(item['role'], name=item['label'], exact=True)
    value = item['anchor']
    return page.locator(f':is(button,a,[role="button"],[role="tab"],summary):is([id="{value}"],[aria-controls="{value}"])')


async def visible_target(page, item):
    candidates = locator(page, item)
    if await candidates.count() == 0:
        try:
            await candidates.first.wait_for(state='visible', timeout=1800 if item['optional'] else 6000)
        except BrowserTimeout:
            return None
    visible = [candidate for candidate in await candidates.all() if await candidate.is_visible()]
    if len(visible) > 1:
        raise ValueError('Mehrere sichtbare Ziele passen zur Aufnahme.')
    return visible[0] if visible else None


async def replay(page, desktop, macro, log):
    start = macro['start']

    async def guard(route):
        request = route.request
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return await route.abort('blockedbyclient')
        if request.is_navigation_request():
            try:
                location(request.url, start)
            except ValueError:
                return await route.abort('blockedbyclient')
        await route.fallback()

    async def check():
        await site_safety.check_page(page)
        if await site_privacy.contains_auth_form(page):
            raise ValueError('Anmeldung erforderlich. Makro wartet auf den freigegebenen Webseitenzugang.')
        if await page.evaluate('Boolean(navigator.serviceWorker?.controller)'):
            raise ValueError('Makros benötigen eine Seite ohne aktiven Service Worker.')
        return location(page.url, start)

    actions = []
    await page.route('**/*', guard)
    try:
        if await check() != start:
            await desktop.goto(start)
        for number, item in enumerate(macro['steps'], 1):
            try:
                if await check() != item['at']:
                    raise ValueError('Die Seite stimmt nicht mit der Aufnahme überein.')
                target = await visible_target(page, item)
                if target is None and item['optional']:
                    status = 'Hinweis nicht sichtbar'
                elif target is None:
                    raise ValueError('Das aufgezeichnete Ziel ist nicht sichtbar.')
                else:
                    expanded = item['expanded']
                    before = await target.get_attribute('aria-expanded', timeout=2000) if expanded is not None else None
                    if expanded is not None and before == str(expanded).lower():
                        status = 'Bereits im aufgenommenen Zustand'
                    else:
                        await target.scroll_into_view_if_needed(timeout=3000)
                        x, y = await desktop.point(target)
                        await desktop.click(x, y, item['label'], macro_anchor=item['anchor'])
                        if expanded is not None and await check() == item['at'] and await target.count() == 1:
                            if await target.get_attribute('aria-expanded', timeout=2000) != str(expanded).lower():
                                raise ValueError('Der aufgezeichnete Menüzustand wurde nicht erreicht.')
                        status = 'Ausgeführt'
            except site_safety.ProtectionDetected:
                raise
            except (ValueError, RuntimeError) as error:
                raise RuntimeError(f'Makro bei Schritt {number} angehalten. {error}') from None
            actions.append({'step': number, 'label': item['label'] or 'Menüsymbol', 'result': status})
            log(f'Makroschritt {number}: {status}.')
        if await check() != macro['target']:
            raise RuntimeError('Makro-Zielseite wurde nicht erreicht.')
        return {'name': macro['name'], 'steps': actions, 'target_reached': True}
    finally:
        await page.unroute('**/*', guard)
