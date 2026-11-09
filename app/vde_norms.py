"""Verified, read-only navigation for the VDE NormenBibliothek."""
import re
from urllib.parse import urlsplit

from playwright.async_api import expect

import site_privacy
import site_safety
import store

URL = 'https://www.normenbibliothek.de/xopp/normenbibliothek/start.xav'
FILTER = 'Normen'
DATE_LABEL = 'Ausgabedatum'
TITLE = re.compile(r'^Normen\s*[-–]\s*[\d.]+\s+Dokumente?$', re.I)
MENU = 'dialog#dialog_mainmenu'


def matches(url):
    return url == URL


def library_page(url):
    if not isinstance(url, str):
        return False
    try:
        return urlsplit(url)._replace(query='', fragment='').geturl() == URL
    except ValueError:
        return False


def library_tab(pages):
    return next((page for page in reversed(pages) if library_page(page.url)), None)


def retained_tab(pages):
    """Keep one library tab for opted-in projects, without trusting its login state."""
    page = library_tab(pages)
    if page is None:
        return None
    enabled = store.one("SELECT 1 FROM projects p JOIN state s ON s.key='browser_flow:'||p.id "
                        "WHERE p.url=? AND s.value='vde_norms' LIMIT 1", (URL,))
    return page if enabled else None


async def check_page(page):
    await site_safety.check_page(page)
    if not library_page(page.url) or await site_privacy.contains_auth_form(page):
        raise RuntimeError('Die angemeldete VDE-NormenBibliothek ist nicht sichtbar.')


async def click(page, desktop, target):
    await check_page(page)
    await site_safety.wait()
    if await target.count() != 1 or not await target.is_visible():
        raise RuntimeError('Der erwartete VDE-Navigationsknopf ist nicht eindeutig sichtbar.')
    if not await target.evaluate("e => e.tagName === 'BUTTON' && !e.form && !e.closest('form') && !e.disabled"):
        raise RuntimeError('Die erwartete VDE-Schaltfläche ist kein freigegebener Navigationsknopf.')
    if not await desktop.click_locator(target):
        raise RuntimeError('Der VDE-Navigationsknopf ist verdeckt.')
    await desktop.wait_ready()


async def select_norms(page, desktop, *, mobile):
    await check_page(page)
    menu = page.locator(MENU)
    panel = page.locator('#normenuebersicht')
    heading = panel.locator('header .mainSwitchContainerChild__header__meta__title')
    if not (await heading.count() == 1 and await heading.is_visible()
            and TITLE.fullmatch(' '.join((await heading.inner_text()).split()))):
        if mobile and not await menu.is_visible():
            await click(page, desktop, page.locator('header.header__main button.icon__menu'))
            await menu.wait_for(state='visible', timeout=8000)
        navigation = menu if mobile else page.locator('#aside__main')
        await click(page, desktop, navigation.get_by_role('tab', name=FILTER, exact=True))
    if mobile and await menu.is_visible():
        await click(page, desktop, menu.locator('header.dialogHeader button.close'))
        await menu.wait_for(state='hidden', timeout=5000)
    await check_page(page)
    try:
        await expect(heading).to_be_visible(timeout=8000)
        await expect(heading).to_have_text(TITLE, use_inner_text=True, timeout=8000)
    except AssertionError:
        raise RuntimeError('Die VDE-Zielkategorie „Normen“ wurde nicht bestätigt.') from None
