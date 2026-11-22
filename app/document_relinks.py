"""Recognize a new file path at a uniquely named position in the same source list."""
from collections import defaultdict
import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import change_review
from document_inventory import declared_file, document_identity
from document_types import DOC_EXT
from page_content import _link_metadata
import retention
import store

INVENTORY_REQUEST = 'Neu verlinkte und entfernte Originaldateien jeweils mit ihrem Titel melden.'


def _titles(page):
    if not page.get('html'):
        return {}
    try:
        path = retention._safe(store.DATA, page['html'], ('artifacts',))
        if path.stat().st_size > 12 * 1024**2:
            return {}
        soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    except (OSError, UnicodeError, ValueError):
        return {}
    names = defaultdict(set)
    for anchor in soup.select('a[href]'):
        try:
            link = _link_metadata(anchor, page['url'])
            path = urlsplit(link['url']).path.lower()
        except ValueError:
            continue
        if not (path.endswith(DOC_EXT) or declared_file(link)):
            continue
        name = ' '.join(link['label'].split())
        if name and name.casefold() not in ('pdf', 'download', 'herunterladen', 'dokument herunterladen', 'datei herunterladen') and name != link['url']:
            names[name].add(link['url'])
    return names


def compare(row, task=''):
    if 'Links geändert' not in row['kind'].split('; '):
        return None
    try:
        detail = json.loads(row['evidence'])
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    additions, removals = detail.get('links_added'), detail.get('links_removed')
    if any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
           for items in (additions, removals)):
        return None
    added, removed = set(additions), set(removals)
    inventory_requested = INVENTORY_REQUEST in task
    if not (added or removed) or (not inventory_requested and not (added and removed)):
        return None
    pages = [store.one("SELECT * FROM snapshots WHERE id=? AND project_id=? AND kind='page' AND url=?",
                       (row[k], row['project_id'], row['url'])) for k in ('previous_id', 'current_id')]
    previous, current = pages
    if not all(pages) or previous['run_id'] == current['run_id'] or current['run_id'] != row['run_id']:
        return None
    old, new = map(_titles, pages)
    docs = [{s['url']: s for s in store.rows("SELECT * FROM snapshots WHERE project_id=? AND run_id=? AND kind='document'",
                                             (row['project_id'], page['run_id']))} for page in pages]
    pairs = []
    for title in sorted(old.keys() & new.keys()):
        if len(old[title]) != 1 or len(new[title]) != 1:
            continue
        before, after = next(iter(old[title])), next(iter(new[title]))
        if before not in removed or after not in added:
            continue
        a, b = docs[0].get(before), docs[1].get(after)
        if not a or not b or not all(change_review._verified_original(s) for s in (a, b)):
            continue
        pairs.append({'title':title, 'before':before, 'after':after,
                      'previous_snapshot_id':a['id'], 'current_snapshot_id':b['id'],
                      'previous_sha256':a['sha256'], 'current_sha256':b['sha256']})
    entries = []
    covered_old = {p['before'] for p in pairs}
    covered_new = {p['after'] for p in pairs}
    # Several files can share a label such as "Begründung". The inventory's
    # existing identity rule recognizes a cache timestamp without discarding
    # version parameters. Exclude such links only after verifying both originals.
    prior_by_identity = defaultdict(list)
    current_by_identity = defaultdict(list)
    for url in removed - covered_old:
        if url in docs[0]:
            prior_by_identity[document_identity(url)].append(url)
    for url in added - covered_new:
        if url in docs[1]:
            current_by_identity[document_identity(url)].append(url)
    for key in sorted(prior_by_identity.keys() & current_by_identity.keys()):
        previous_urls, current_urls = prior_by_identity[key], current_by_identity[key]
        if len(previous_urls) != 1 or len(current_urls) != 1:
            continue
        before, after = previous_urls[0], current_urls[0]
        a, b = docs[0][before], docs[1][after]
        if a['sha256'] == b['sha256'] and all(change_review._verified_original(s) for s in (a, b)):
            pairs.append({'title':urlsplit(after).path.rsplit('/', 1)[-1], 'before':before, 'after':after,
                'previous_snapshot_id':a['id'], 'current_snapshot_id':b['id'],
                'previous_sha256':a['sha256'], 'current_sha256':b['sha256']})
            covered_old.add(before)
            covered_new.add(after)
    if inventory_requested:
        for direction, names, documents, unmatched in (
            ('added', new, docs[1], added - covered_new), ('removed', old, docs[0], removed - covered_old),
        ):
            for url in sorted(unmatched):
                titles = [name for name, links in names.items() if links == {url}]
                original = documents.get(url)
                if len(titles) == 1 and original and change_review._verified_original(original):
                    entries.append({'direction': direction, 'title': titles[0], 'url': url,
                                    'snapshot_id': original['id'], 'sha256': original['sha256']})
            covered = covered_new if direction == 'added' else covered_old
            covered.update(item['url'] for item in entries if item['direction'] == direction)
    if not pairs and not entries:
        return None
    result = {'files': pairs, 'complete': row['kind'] == 'Links geändert' and covered_old == removed and covered_new == added}
    if not result['complete']:
        result['links_complete'] = covered_old == removed and covered_new == added
    if entries:
        result['entries'] = entries
    return result


def review(row, comparison):
    changed = [p for p in comparison['files'] if p['previous_sha256'] != p['current_sha256']]
    entries = comparison.get('entries', [])
    if not changed and not entries and not comparison['complete']:
        return False
    names = ['Neue Dateifassung · ' + p['title'] for p in changed]
    names.extend(('Neu verlinkt · ' if p['direction'] == 'added' else 'Nicht mehr verlinkt · ') + p['title'] for p in entries)
    reason = '\n'.join(names) or 'Die Downloadadressen haben sich geändert. Die zugeordneten Originaldateien sind bytegleich.'
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        rec = c.execute('SELECT evidence FROM changes WHERE id=? AND previous_id=? AND current_id=?',
                        (row['id'], row['previous_id'], row['current_id'])).fetchone()
        if not rec:
            return False
        detail = json.loads(rec['evidence'])
        if detail.get('document_relinks') == comparison and detail.get('ai_assessment', {}).get('policy') == 'document-relinks-v1':
            return True
        stamp = store.now()
        if 'ai_assessment' in detail:
            detail.setdefault('assessment_history', []).append({'assessment':detail['ai_assessment'],
                'replaced_at':stamp, 'replaced_by_policy':'document-relinks-v1'})
            if not comparison['complete'] and detail['ai_assessment'].get('policy') != 'document-relinks-v1':
                detail['page_assessment'] = detail['ai_assessment']
        detail.update(document_relinks=comparison, ai_assessment={'policy':'document-relinks-v1','method':'deterministic',
                      'relevance':'relevant' if names else 'irrelevant','summary':reason,'reason':reason,'reviewed_at':stamp})
        c.execute('UPDATE changes SET evidence=? WHERE id=?', (json.dumps(detail, ensure_ascii=False), row['id']))
    return True
