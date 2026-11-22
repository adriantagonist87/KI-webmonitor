"""Reader-facing summaries from archived findings for opted-in monitoring tasks."""
from datetime import datetime
from html import escape
import json
import re
from urllib.parse import unquote, urlsplit

import email_reports
import article_headlines


PREFIX = 'Kurzmeldung bei vollständigem unverändertem Bestand: '


def _source(title, url):
    url = email_reports.safe_url(url)
    return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>' if url else escape(title)


def _documents(evidence):
    return {x['url']: x for x in (evidence.get('document_check') or {}).get('documents', [])
            if isinstance(x, dict) and x.get('url')}


def _file_title(url, files):
    title = files.get(url, {}).get('title')
    if title and title != url and title.casefold() not in ('download', 'herunterladen', 'pdf'):
        return title
    try:
        return unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or 'Verlinkte Datei'
    except ValueError:
        return 'Verlinkte Datei'


def _catalog_lines(collection):
    lines = []
    for key in ('added', 'updated', 'removed'):
        for obj in collection.get(key, []):
            if not isinstance(obj, dict):
                continue
            record = obj.get('after', {}) if key == 'updated' else obj
            if not isinstance(record, dict):
                continue
            title = record.get('title', '')
            for label in email_reports.catalog_details({key: [obj]}, limit=None):
                if title and title in label:
                    prefix, suffix = label.split(title, 1)
                    line = escape(prefix) + _source(title, record.get('url')) + escape(suffix)
                    date = re.search(r'(?m)^Erscheinungsdatum:[ \t]*\r?\n(\d{4}-\d{2}-\d{2})\s*$', record.get('text') or '')
                    if date:
                        try:
                            line += ' · Erscheinungsdatum laut Katalog ' + datetime.fromisoformat(date[1]).strftime('%d.%m.%Y')
                        except ValueError:
                            pass
                    lines.append(line)
    return lines


def _change_lines(change, detail, files):
    if detail.get('collection'):
        return _catalog_lines(detail['collection'])
    relinks = detail.get('document_relinks') or {}
    lines = ['Neue Dateifassung · ' + _source(f['title'], f['after']) for f in relinks.get('files', [])
             if f['previous_sha256'] != f['current_sha256']]
    for item in relinks.get('entries', []):
        label = 'Neu verlinkt' if item['direction'] == 'added' else 'Nicht mehr verlinkt'
        lines.append(label + ' · ' + _source(item['title'], item['url']))
    if lines:
        return lines
    url = change['url']
    if change['kind'] == 'Datei geändert':
        title = detail.get('document_title') or _file_title(url, files)
        line = 'Neue Dateifassung · ' + _source(title, url)
        metadata = detail.get('document_metadata_comparison') or {}
        dates = []
        for side in ('previous', 'current'):
            date = (((metadata.get(side) or {}).get('pdf') or {}).get('creation_date') or {}).get('europe_berlin')
            try:
                dates.append(datetime.fromisoformat(date).strftime('%d.%m.%Y'))
            except (TypeError, ValueError):
                break
        if len(dates) == 2 and dates[0] != dates[1]:
            line += f'. Das PDF-Erstellungsdatum wechselte vom {dates[0]} auf den {dates[1]}.'
        return [line]
    return []


def _page_wording(detail):
    """Quote a short, complete page delta alongside the separately named downloads."""
    relinks = detail.get('document_relinks') or {}
    diff = detail.get('text_diff') or ''
    if (not relinks.get('links_complete') or detail.get('diff_truncated') or len(diff) >= 60000
            or not diff.startswith('--- vorher\n+++ nachher\n@@')):
        return None
    names = {item['title'] for item in [*relinks.get('entries', []), *relinks.get('files', [])]}
    blocks = []
    previous = ''
    for line in diff.splitlines()[2:]:
        sign, wording = line[:1], line[1:].strip()
        if sign not in ('+', '-') or not wording:
            previous = ''
            continue
        if wording in names or re.fullmatch(r'PDF-Dokument\s*\(\d+(?:[.,]\d+)?\s*[kKmMgG]?B\)', wording):
            previous = ''
            continue
        if sign != previous:
            blocks.append((sign, []))
        blocks[-1][1].append(wording)
        previous = sign
    if len(blocks) > 4 or sum(len(s) for _, lines in blocks for s in lines) > 750:
        return None
    return [('Von der Seite entfernt' if sign == '-' else 'Neu auf der Seite') + ' · „' +
            escape(' '.join(lines)) + '“' for sign, lines in blocks]


def verified_summary(evidence):
    """A quiet message requires complete coverage and a compatible prior capture."""
    article_summary = article_headlines.summary(evidence)
    if article_summary is not None:
        return article_summary
    task = evidence.get('task', '')
    quiet = next((line[len(PREFIX):].strip() for line in task.splitlines() if line.startswith(PREFIX)), '')
    if not quiet and evidence.get('controller') != 'vde-norms-v1':
        return None
    inventory = evidence.get('document_check') or {}
    agent = evidence.get('agent') or {}
    observations = evidence.get('observations') or []
    if (not observations or not evidence.get('prior_successful_runs') or agent.get('complete') is not True
            or agent.get('pending') or evidence.get('errors') or inventory.get('failed') or inventory.get('unverified')):
        return None
    files = _documents(evidence)
    findings = []
    for change in evidence.get('changes', []):
        detail = change.get('evidence') or {}
        if isinstance(detail, str):
            detail = json.loads(detail)
        assessment = detail.get('ai_assessment') or {}
        if assessment.get('relevance') == 'irrelevant':
            continue
        lines = _change_lines(change, detail, files) if assessment.get('relevance') == 'relevant' else []
        if not lines:
            return None  # Other findings, baselines and open questions require the full report.
        if detail.get('document_relinks') and not detail['document_relinks'].get('complete'):
            wording = _page_wording(detail)
            if wording is None or (detail.get('page_assessment') or {}).get('relevance') == 'uncertain':
                return None
            lines.extend(wording)
        findings.extend(lines)
    if findings:
        return '\n\n'.join(dict.fromkeys(findings))
    if quiet and all(x.get('prior_snapshot_available') and not x.get('metadata', {}).get('comparison_scope_changed')
                     for x in observations):
        return escape(quiet.replace('{Dateizahl}', str(inventory.get('checked', 0))))
    return None


def mixed_document_lines(evidence):
    if PREFIX not in evidence.get('task', ''):
        return []
    lines = []
    for change in evidence.get('changes', []):
        detail = email_reports._evidence(change)
        relinks = detail.get('document_relinks') or {}
        if relinks and not relinks.get('complete') and (detail.get('ai_assessment') or {}).get('relevance') == 'relevant':
            lines.extend(_change_lines(change, detail, _documents(evidence)))
    return list(dict.fromkeys(lines))


def open_comparisons(evidence):
    """Keep unresolved nonvisual changes visible even if a model writes an all-clear."""
    urls = []
    for change in evidence.get('changes', []):
        detail = change.get('evidence') or {}
        if isinstance(detail, str):
            detail = json.loads(detail)
        if (any((detail.get(key) or {}).get('relevance') == 'uncertain' for key in ('ai_assessment', 'page_assessment'))
                and change.get('kind') != 'Visuelle Abweichung'):
            urls.append(change['url'])
    return list(dict.fromkeys(urls))


def coverage_note(evidence):
    inventory = evidence.get('document_check') or {}
    missing = len(inventory.get('failed') or []) + len(inventory.get('unverified') or [])
    if missing == 1:
        return 'Ein Dokument konnte nicht geprüft werden. Die Auswertung gilt nur für die geprüften Quellen.'
    if missing:
        return f'{missing} Dokumente konnten nicht geprüft werden. Die Auswertung gilt nur für die geprüften Quellen.'
    agent = evidence.get('agent') or {}
    if evidence.get('errors') or agent.get('pending') or agent.get('complete') is False:
        return 'Der Prüfauftrag konnte nur teilweise abgeschlossen werden. Einzelheiten stehen unter „Quellen & Details“.'
    return ''
