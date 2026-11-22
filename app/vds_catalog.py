"""Publication names from the archived VdS start-page comparison."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import retention
import store


START = 'https://shop.vds.de/'
PUBLICATION = re.compile(r'https://shop\.vds\.de/(?:de|en)/publikation/[a-z0-9-]+/?')
MAX_HTML = 1024 * 1024


def _cards(row, snapshot_key, urls):
    if not urls or not row.get(snapshot_key):
        return {}
    snapshot = store.one(
        "SELECT html FROM snapshots WHERE id=? AND project_id=? AND url=? AND kind='page'",
        (row[snapshot_key], row['project_id'], START),
    )
    if not snapshot or not snapshot['html']:
        return {}
    try:
        path = retention._safe(retention._root(), snapshot['html'], ('artifacts',))
        with path.open('rb') as file:
            html = file.read(MAX_HTML + 1).decode('utf-8')
    except (OSError, ValueError):
        return {}
    if len(html) > MAX_HTML:
        return {}
    matches = {}
    for card in BeautifulSoup(html, 'html.parser').select('a[href]'):
        try:
            target = urljoin(START, card['href'])
        except ValueError:
            continue
        if target not in urls:
            continue
        number = card.find(['h4', 'h5', 'h6'])
        title = card.find('p')
        if number and title:
            designation = ' '.join(number.get_text(' ', strip=True).split())
            name = ' '.join(title.get_text(' ', strip=True).split())
            if designation.startswith('VdS ') and 4 < len(designation) <= 80 and 0 < len(name) <= 350:
                matches.setdefault(target, set()).add((designation, name))
    return {url: next(iter(entries)) for url, entries in matches.items() if len(entries) == 1}


def list_changes(row, evidence):
    if row.get('url') != START or not row.get('project_id'):
        return []
    removed, added = evidence.get('links_removed', []), evidence.get('links_added', [])
    if not removed or not isinstance(removed, list) or not isinstance(added, list):
        return []
    result = []
    for state, snapshot_key, links in (('removed', 'previous_id', removed), ('added', 'current_id', added)):
        urls = {url for url in links if isinstance(url, str) and PUBLICATION.fullmatch(url)}
        for url, (number, title) in sorted(_cards(row, snapshot_key, urls).items()):
            result.append({'state': state, 'url': url, 'number': number, 'title': title})
    return result if any(item['state'] == 'removed' for item in result) else []
