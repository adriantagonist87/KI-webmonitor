"""Compare complete, publicly listed VdS category tables."""
import asyncio
from html import escape
import json
import re

from playwright.async_api import TimeoutError as BrowserTimeout
from readonly_graphql import is_vds_category_read
import retry_jobs
import site_safety
import store


ORIGIN = 'https://shop.vds.de'
CATEGORY = re.compile(ORIGIN.replace('.', r'\.') + r'/de/kategorie/(?:brandschutz|security)/[a-z0-9-]+/?')
SLUG = re.compile(r'[a-z0-9-]{1,180}')
INDEX = 'vds-category-v1:'
ROWS = 'table.table-hover tbody > tr'
WITHDRAWN = 'Zurückgezogen'


def matches(url):
    return bool(CATEGORY.fullmatch(url))


def category_response(response):
    return response.url == ORIGIN + '/graphql' and is_vds_category_read(response.request)


def expected_entries(payload):
    data = payload.get('data') if isinstance(payload, dict) and not payload.get('errors') else None
    items = data.get('publicationsByCategory') if isinstance(data, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise RuntimeError('VdS lieferte keine vollständige, nichtleere Kategorieliste. Vergleichsstand beibehalten.')
    expected = {}
    for item in items:
        slug = item.get('slugName') if isinstance(item, dict) else None
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or slug in expected:
            raise RuntimeError('VdS lieferte uneindeutige Publikationskennungen. Vergleichsstand beibehalten.')
        expected[slug] = item.get('state') == 'WITHDRAWN'
    return expected


def verified_entries(rows, expected):
    entries = {}
    prefix = ORIGIN + '/de/publikation/'
    for row in rows:
        url, number, title = (row[key] for key in ('url', 'number', 'title'))
        slug = url.removeprefix(prefix) if url.startswith(prefix) else ''
        if (slug not in expected or url in entries or not 0 < len(number) <= 100
                or not 0 < len(title) <= 400 or (WITHDRAWN in row['status']) != expected[slug]):
            raise RuntimeError('VdS-Tabelle und Katalogantwort stimmen nicht überein. Vergleichsstand beibehalten.')
        entries[url] = row
    if len(entries) != len(expected):
        raise RuntimeError('VdS-Kategorietabelle ist unvollständig. Vergleichsstand beibehalten.')
    return entries


async def monitor(page, project, rid):
    url = project['url']
    store.event(rid, 'Öffentliche VdS-Kategorietabelle „Alle anzeigen“ wird in Brave gelesen.')
    for attempt in range(2):
        try:
            async with page.expect_response(category_response, timeout=40000) as pending:
                await site_safety.goto(page, url, wait_until='domcontentloaded', timeout=30000)
            response = await pending.value
            break
        except BrowserTimeout:
            await site_safety.check_page(page)
            if attempt:
                raise RuntimeError('VdS hat die öffentliche Kategorieliste zweimal nicht rechtzeitig geliefert. Der letzte erfolgreiche Stand bleibt erhalten.') from None
            store.event(rid, 'Die Katalogantwort fehlt. Ein weiterer Leseversuch folgt.')
            await asyncio.sleep(3)
    site_safety.raise_http(response.url, response.status, response.headers)
    await site_safety.check_page(page)
    if response.status != 200 or page.url.rstrip('/') != url.rstrip('/'):
        raise RuntimeError('VdS-Kategorie konnte nicht geladen werden. Vergleichsstand beibehalten.')
    expected = expected_entries(await response.json())
    try:
        await page.wait_for_function(
            "({selector, count}) => document.querySelectorAll(selector).length === count",
            arg={'selector': ROWS, 'count': len(expected)}, timeout=15000,
        )
    except BrowserTimeout:
        await site_safety.check_page(page)
        raise RuntimeError('Die sichtbare VdS-Tabelle wurde nicht vollständig aufgebaut. Vergleichsstand beibehalten.') from None
    rows = await page.locator(ROWS).evaluate_all(r"""rows => rows.map(row => {
        const clean = text => (text || '').replace(/\s+/g, ' ').trim();
        const heading = row.querySelector('h5');
        const link = heading?.querySelector('a[href]');
        return {
            url: link?.href || '',
            number: clean(link?.innerText),
            title: clean(heading?.querySelector('small')?.innerText),
            status: Array.from(heading?.querySelectorAll('.badge') || [], badge => clean(badge.innerText)),
        };
    })""")
    record_run(project, rid, verified_entries(rows, expected))


def findings(previous, current):
    for url, entry in sorted(current.items()):
        old = previous.get(url)
        if old is None:
            label = 'Neu in der Kategorie'
            if WITHDRAWN in entry['status']:
                label += ' (als zurückgezogen gekennzeichnet)'
            yield url, 'Links geändert', label, entry
        elif WITHDRAWN in entry['status'] and WITHDRAWN not in old['status']:
            yield url, 'Inhalt geändert', 'Als zurückgezogen gekennzeichnet', entry
        elif entry['number'] != old['number']:
            yield url, 'Inhalt geändert', 'Ausgabe geändert', entry
    for url in sorted(previous.keys() - current.keys()):
        yield url, 'Links geändert', 'Aus der Kategorie verschwunden', previous[url]


def record_run(project, rid, current):
    url, pid = project['url'], project['id']
    stamp, sid = store.now(), store.uid()
    archive = INDEX + json.dumps(current, ensure_ascii=False, sort_keys=True)
    sha = store.digest(archive)
    with store.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        prior = connection.execute('''SELECT s.id,s.text FROM snapshots s JOIN runs r ON r.id=s.run_id
            WHERE s.project_id=? AND s.url=? AND s.kind='page' AND r.status='success'
            AND substr(s.text,1,?)=? ORDER BY s.captured DESC,s.rowid DESC LIMIT 1''',
            (pid, url, len(INDEX), INDEX)).fetchone()
        connection.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (sid,rid,pid,url,'page',stamp,sha,sha,archive,json.dumps(sorted(current)),None,None,None))
        lines = []
        if prior:
            previous = json.loads(prior['text'][len(INDEX):])
            for target, kind, label, entry in findings(previous, current):
                title = f'{entry["number"]} „{entry["title"]}“'
                summary = f'{label} · {title}'
                detail = {'publication': entry, 'ai_assessment': {
                    'relevance':'relevant','reason':summary,'summary':summary,
                }}
                if kind == 'Links geändert':
                    detail['links_added' if target in current else 'links_removed'] = [target]
                fingerprint = store.digest(f'{prior["id"]}\n{sha}\n{target}\n{label}')
                connection.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                                   (store.uid(),pid,rid,target,kind,stamp,prior['id'],sid,
                                    json.dumps(detail,ensure_ascii=False),fingerprint))
                lines.append(f'- {label} · <a href="{target}">{escape(title)}</a>')
        intro = f'**{len(current)} Publikationen** in der Kategorie geprüft.'
        if lines:
            summary = intro + '\n\n' + '\n'.join(lines)
        elif prior:
            summary = intro + ' Bestand, Ausgaben und Rückzugskennzeichnungen sind gegenüber der letzten erfolgreichen Prüfung unverändert.'
        else:
            summary = intro + ' Ausgangsstand erfasst.'
        if not retry_jobs.finish_run(rid,'success',summary=summary,finished=stamp,connection=connection):
            raise RuntimeError('VdS-Prüfung wurde bereits beendet.')
    store.event(rid,f'VdS-Kategorie vollständig erfasst: {len(current)} Publikationen, {len(lines)} Änderungen.')
