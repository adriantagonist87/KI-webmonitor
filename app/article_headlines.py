"""Opt-in reports containing only titles of newly listed articles."""
from html import escape
import json
import re
from urllib.parse import urlsplit

import store


FORMAT = 'Ausgabeformat: Nur neu hinzugekommene Artikelüberschriften.'


def enabled(task):
    return FORMAT in (task or '').splitlines()


def _article(url):
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.username or parts.password or parts.query or parts.fragment:
            return False
        if parts.hostname == 'www.elektro.net':
            return bool(re.fullmatch(r'/praxisprobleme/[^/]+/', parts.path))
        return parts.hostname == 'www.iww.de' and bool(re.fullmatch(r'/pbp/.+-f\d+', parts.path))
    except ValueError:
        return False


def _listing(url):
    parts = urlsplit(url)
    return ((parts.hostname == 'www.elektro.net' and parts.path.rstrip('/') == '/praxisprobleme')
            or (parts.hostname == 'www.iww.de' and re.fullmatch(r'/pbp/archiv(?:/\d{4}/\d{1,2})?/?',parts.path)))


def record_additions(project, rid, observations):
    """Compare article identities across issues, preserving the first baseline."""
    prior = [row for row in store.rows("""SELECT s.id,s.url,s.links FROM snapshots s JOIN runs r ON r.id=s.run_id
        WHERE s.project_id=? AND s.run_id<>? AND s.kind='page' AND r.status='success'
        ORDER BY s.captured DESC,s.rowid DESC""",(project['id'],rid)) if _listing(row['url'])]
    if not prior:
        return
    known = {url for row in prior for url in json.loads(row['links']) if _article(url)}
    backed = set()
    for row in store.rows('SELECT evidence FROM changes WHERE run_id=?',(rid,)):
        detail = json.loads(row['evidence'])
        if (detail.get('ai_assessment') or {}).get('relevance') == 'relevant':
            backed.update(detail.get('links_added',[]))
    for source in observations:
        if source.get('kind') != 'page' or not source.get('snapshot_id') or not _listing(source['url']):
            continue
        additions = {}
        for link in source.get('metadata',{}).get('links',[]):
            if not isinstance(link,dict):
                continue
            url = link.get('url')
            title = ' '.join(str(link.get('label') or '').split())
            if _article(url) and url not in known and url not in backed and title and title != url:
                additions[url] = title
        if not additions:
            continue
        previous = next((p for p in prior if p['url']==source['url']),prior[0])
        detail = {'links_added':list(additions),
                  'article_additions':[{'url':url,'title':title} for url,title in additions.items()],
                  'ai_assessment':{'relevance':'relevant','policy':'article-headlines-v1',
                                   'reason':'Neu in der überwachten Artikelübersicht · ' + ' · '.join(additions.values())}}
        store.execute('INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (store.uid(),project['id'],rid,source['url'],'Links geändert',store.now(),
                       previous['id'],source['snapshot_id'],json.dumps(detail,ensure_ascii=False),
                       store.digest(rid+'|article-headlines|'+source['snapshot_id'])))
        backed.update(additions)


def from_evidence(evidence):
    """Take added links from the proof and exact titles from observed links."""
    titles = {}
    for observation in evidence.get('observations', []):
        if observation.get('kind') != 'page':
            continue
        for link in observation.get('metadata', {}).get('links', []):
            if not isinstance(link, dict) or not _article(link.get('url')):
                continue
            title = ' '.join(str(link.get('label') or '').split())
            if title and title != link['url']:
                titles.setdefault(link['url'], title)
    added = []
    uncertain = set()
    if not titles:
        return None
    for change in evidence.get('changes', []):
        detail = change.get('evidence') or {}
        if isinstance(detail, str):
            detail = json.loads(detail)
        relevance = (detail.get('ai_assessment') or {}).get('relevance')
        links = detail.get('links_added', [])
        if relevance == 'uncertain':
            uncertain.update(url for url in links if _article(url))
        if relevance != 'relevant':
            continue
        for url in links:
            if _article(url) and url not in added:
                added.append(url)
    if uncertain.difference(added) or any(url not in titles for url in added):
        return None
    return [{'url':url,'title':titles[url]} for url in added]


def from_changes(changes):
    """Use archived observations when producing mail or Telegram later."""
    groups = {}
    for change in changes:
        groups.setdefault(change.get('run_id'), []).append(change)
    result = {}
    for rid, rows in groups.items():
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', rid):
            return None
        try:
            evidence = json.loads((store.DATA/'artifacts'/rid/'evidence.json').read_text())
            entries = from_evidence({**evidence, 'changes': rows})
        except (OSError, ValueError, TypeError):
            return None
        if entries is None:
            return None
        result.update((entry['url'],entry) for entry in entries)
    return list(result.values())


def summary(evidence):
    if not enabled(evidence.get('task')):
        return None
    agent = evidence.get('agent') or {}
    if not evidence.get('observations') or agent.get('complete') is not True or agent.get('pending') or evidence.get('errors'):
        return None
    entries = from_evidence(evidence)
    if entries is None:
        return None
    if entries:
        return '\n\n'.join(f'### <a href="{escape(item["url"],quote=True)}">{escape(item["title"])}</a>' for item in entries)
    if not evidence.get('prior_successful_runs') or any(
        not item.get('prior_snapshot_available') for item in evidence['observations']
    ):
        return 'Ausgangsstand der Artikelübersicht erfasst.'
    return 'Keine neuen Artikel erkannt.'
