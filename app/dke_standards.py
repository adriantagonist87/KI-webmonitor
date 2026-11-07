"""Monitor the public table fragments used by DKE's national Norm selector."""
from datetime import datetime
import json
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx

import retry_jobs
import site_safety
import store


ORIGIN = 'https://www.dke.de'
PAGE_PATH = '/de/normen-standards/arbeitsergebnisse'
SOURCE = ORIGIN + PAGE_PATH + '#aetable'
DETAIL_PATH = '/de/normen-standards/dokument'
CHANGE_KIND = 'Neue Normausgabe'
PRESTANDARD_KIND = 'Neue Vornorm'
INDEX = 'dke-normausgaben-v1:'
PERIODS = {
    'month': ('n_am', 'aktueller-monat', 'aktueller Monat'),
    'quarter': ('n_aq', 'aktuelles-quartal', 'aktuelles Quartal'),
    'previous_quarter': ('n_q1', 'letztes-quartal', 'letztes Quartal'),
}
EDITION = re.compile(r'(.+?)\s*:\s*([0-9]{4}-(?:0[1-9]|1[0-2]))')
PRESTANDARD = re.compile(r'\bVDE\s+V\s+(?=[0-9])', re.IGNORECASE)


def matches(url):
    return url in (SOURCE, ORIGIN + PAGE_PATH)


def table_urls(html):
    page = BeautifulSoup(html, 'html.parser')
    urls = {}
    for period, (selector, slug, label) in PERIODS.items():
        links = page.select(f'a#{selector}[href="#aetable"][data-table]')
        identifier = links[0]['data-table'] if len(links) == 1 else ''
        wrappers = page.find_all(id='dbq' + identifier) if re.fullmatch(r'[0-9]+', identifier) else []
        path = wrappers[0].get('data-datatable-url', '') if len(wrappers) == 1 else ''
        expected = f'{PAGE_PATH}/ergebnisse-national--normen--{slug}-{identifier}?view=fragment'
        if path != expected:
            raise RuntimeError(f'DKE-Auswahl „Norm · {label}“ nicht eindeutig gefunden.')
        urls[period] = ORIGIN + path
    return urls


def norm_link(cell):
    links = cell.select('a[href]')
    link = urlsplit(urljoin(ORIGIN, links[0]['href'])) if len(links) == 1 else None
    if link and link.scheme == 'https' and link.netloc == 'www.dke.de' and link.path == DETAIL_PATH:
        query = parse_qs(link.query, keep_blank_values=True)
        identifiers = query.get('id', [])
        if len(identifiers) == 1 and re.fullmatch(r'[0-9]+', identifiers[0]) and query.get('type') == ['dke|dokument']:
            return ORIGIN + DETAIL_PATH + '?' + urlencode({'type': 'dke|dokument', 'id': identifiers[0]})
    raise RuntimeError('DKE-Tabelle enthält keinen eindeutigen Link zum Schriftstück.')


def parse_table(html, period):
    if not html.strip():
        return []  # DKE sends HTTP 200 with an empty HTML body for zero matches.
    fragment = BeautifulSoup(html, 'html.parser')
    heading = fragment.find('h1')
    tables = fragment.select('.js-datatable > table')
    expected = 'Ergebnisse national: Normen, ' + PERIODS[period][2]
    if not heading or heading.get_text(' ', strip=True) != expected or len(tables) != 1:
        raise RuntimeError(f'DKE-Ergebnistabelle für „{PERIODS[period][2]}“ fehlt oder hat ein unbekanntes Format.')
    rows = tables[0].select('tr')
    headers = [cell.get_text(' ', strip=True) for cell in rows[0].find_all(['td', 'th'], recursive=False)] if rows else []
    closing = html.lower()
    if (headers != ['Schriftstück', 'Gremium', 'Titel'] or closing.count('</tr>') != len(rows)
            or '</table>' not in closing):
        raise RuntimeError('DKE-Ergebnistabelle hat unbekannte oder unvollständige Zeilen.')

    records = []
    for row in rows[1:]:
        cells = row.find_all('td', recursive=False)
        if len(cells) != 3:
            raise RuntimeError('DKE-Ergebnistabelle enthält eine unvollständige Normzeile.')
        name, committee, title = (' '.join(cell.get_text(' ', strip=True).split()) for cell in cells)
        if re.match(r'^E\s+(?:DIN|VDE)\b', name):
            continue
        match = EDITION.fullmatch(name)
        if not match or not title:
            raise RuntimeError('DKE-Normzeile enthält keine eindeutige Nummer, Ausgabe oder Titel.')
        document_type = 'Vornorm' if PRESTANDARD.search(unicodedata.normalize('NFKC', match[1])) else 'Norm'
        records.append({'number': match[1], 'edition': match[2], 'title': title, 'document_type': document_type,
                        'committee': committee, 'url': norm_link(cells[0])})
    return records


async def get_html(client, url, label):
    await site_safety.wait()
    try:
        async with client.stream('GET', url) as response:
            site_safety.raise_http(url, response.status_code, response.headers)
            if response.status_code != 200:
                raise RuntimeError(f'DKE-Abruf „{label}“ fehlgeschlagen (HTTP {response.status_code}).')
            if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/html':
                raise RuntimeError(f'DKE-Abruf „{label}“ lieferte kein HTML.')
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > 2 * 1024 * 1024:
                    raise RuntimeError('DKE-Antwort überschreitet das Größenlimit.')
                data.extend(chunk)
            html = data.decode(response.encoding or 'utf-8', errors='strict')
            site_safety.raise_http(url, 200, response.headers, html)
            return html
    except (httpx.HTTPError, UnicodeError):
        raise RuntimeError(f'DKE-Abruf „{label}“ konnte nicht vollständig gelesen werden.') from None


def edition_key(record):
    number = unicodedata.normalize('NFKC', record['number']).casefold()
    number = re.sub(r'[‐‑‒–−]', '-', number)
    return re.sub(r'\s+', '', number) + ':' + record['edition']


def quarter_window(month, offset=0):
    year, month_number = map(int, month.split('-'))
    start = year * 12 + ((month_number - 1) // 3 + offset) * 3
    return {f'{index // 12:04d}-{index % 12 + 1:02d}' for index in range(start, start + 3)}


async def fetch_tables(client, month):
    urls = table_urls(await get_html(client, ORIGIN + PAGE_PATH, 'Auswahl'))
    periods = {'month': {month}, 'quarter': quarter_window(month), 'previous_quarter': quarter_window(month, -1)}
    tables = {}
    for period, url in urls.items():
        records = parse_table(await get_html(client, url, PERIODS[period][2]), period)
        if any(record['edition'] not in periods[period] for record in records):
            raise RuntimeError(f'DKE lieferte für „{PERIODS[period][2]}“ einen abweichenden Ausgabemonat.')
        tables[period] = records
    return tables


def markdown(value):
    return re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', value)


def line(record):
    citation = markdown(record['number'] + ':' + record['edition'])
    committee = f" ({markdown(record['committee'])})" if record['committee'] else ''
    return f"- **{record['document_type']}** · [{citation}]({record['url']}) · {markdown(record['title'])}{committee}"


def record_run(project, rid, month, tables):
    # Prefer the exact monthly source when quarterly metadata differs.
    observed = {edition_key(r): r for period in reversed(PERIODS) for r in tables[period] if r['edition'] <= month}
    monthly = {edition_key(r): r for r in tables['month']}
    counts = {period: len({edition_key(r) for r in records if r['edition'] <= month}) for period, records in tables.items()}
    future = len({edition_key(r) for r in tables['quarter'] if r['edition'] > month})
    url, stamp, sid = project['url'], store.now(), store.uid()
    with store.db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        prior = connection.execute('''SELECT s.id,s.text FROM snapshots s JOIN runs r ON r.id=s.run_id
            WHERE s.project_id=? AND s.url=? AND s.kind='page' AND r.status='success'
            AND substr(s.text,1,?)=? ORDER BY s.captured DESC,s.rowid DESC LIMIT 1''',
            (project['id'], url, len(INDEX), INDEX)).fetchone()
        previous = json.loads(prior['text'][len(INDEX):]) if prior else {}
        seen = set(previous.get('seen', []))
        since = previous.get('baseline_month', month)
        added = []
        archive = INDEX + json.dumps({
            'month': month, 'baseline_month': since, 'seen': sorted(seen | observed.keys()),
            'counts': counts, 'future_excluded': future,
            'publications': [observed[key] for key in sorted(observed)],
        }, ensure_ascii=False)
        sha = store.digest(archive)
        connection.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (sid, rid, project['id'], url, 'page', stamp, sha, sha, archive,
                            json.dumps(sorted({r['url'] for r in observed.values()})), None, None, None))
        if prior:
            for key, record in sorted(observed.items()):
                if key in seen or record['edition'] < since:
                    continue
                kind = PRESTANDARD_KIND if record['document_type'] == 'Vornorm' else CHANGE_KIND
                heading = f"{record['document_type']} · {record['number']}:{record['edition']} · {record['title']}"
                evidence = {'publication': record, 'links_added': [record['url']],
                            'ai_assessment': {'relevance': 'relevant', 'summary': heading,
                                              'reason': 'Neu in der DKE-Kategorie „Norm“: ' + heading}}
                result = connection.execute('''INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(project_id,fingerprint) DO NOTHING''',
                    (store.uid(), project['id'], rid, record['url'], kind, stamp, prior['id'], sid,
                     json.dumps(evidence, ensure_ascii=False), store.digest(INDEX + key)))
                if result.rowcount:
                    added.append(record)
        summary = [f"**{month} · {len(monthly)} Veröffentlichungen** in der DKE-Kategorie „Norm“."]
        prestandards = sum(record['document_type'] == 'Vornorm' for record in monthly.values())
        if prestandards:
            label = 'Vornorm' if prestandards == 1 else 'Vornormen'
            summary[0] += f' Darunter {prestandards} {label}.'
        if not prior:
            summary.append('Ausgangsstand erfasst. Bereits gelistete Ausgaben lösen keine Meldung aus.')
        elif added:
            summary.append('### Neu erfasste Ausgaben\n\n' + '\n'.join(map(line, added)))
        else:
            summary.append('Keine neue Norm oder Vornorm erkannt.')
        if monthly:
            summary.append('<details>\n<summary>Alle Einträge im aktuellen Monat</summary>\n\n'
                           + '\n'.join(map(line, monthly.values())) + '\n\n</details>')
        else:
            summary.append('Die Monatsabfrage war erfolgreich und lieferte keine Einträge.')
        if not retry_jobs.finish_run(rid, 'success', summary='\n\n'.join(summary), finished=stamp, connection=connection):
            raise RuntimeError('DKE-Prüfung wurde bereits beendet.')
    store.event(rid, f"DKE-Monat: {len(monthly)} · Quartal: {counts['quarter']} · Vorquartal: {counts['previous_quarter']} · "
                     f"künftige Ausgaben übersprungen: {future} · neu: {len(added)}.")


async def monitor(project, rid):
    month = datetime.now(store.TZ).strftime('%Y-%m')
    store.event(rid, 'Öffentliche DKE-Tabelle „Neue DKE Arbeitsergebnisse (national) · Norm“ wird abgerufen.')
    async with httpx.AsyncClient(proxy='http://proxy:3128', timeout=httpx.Timeout(25, connect=8),
                                 follow_redirects=False, trust_env=False) as client:
        tables = await fetch_tables(client, month)
    if datetime.now(store.TZ).strftime('%Y-%m') != month:
        raise RuntimeError('Der Ausgabemonat wechselte während der DKE-Abfrage.')
    record_run(project, rid, month, tables)
