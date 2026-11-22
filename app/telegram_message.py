"""Bound outbox messages and apply Telegram formatting at delivery."""
import re

import httpx


SOURCE = '\n\nQuelle: '
DETAILS = '\n\nVollständige Auswertung im Webmonitor.'
LIMIT = 3900  # UTF-16 units, with a margin below Telegram's limit.
MORE = '\n\nWeitere Details im vollständigen Bericht.'
ENTRY = '▸ '


def units(text):
    return len(text.encode('utf-16-le')) // 2


def clip(text, limit, suffix):
    if units(text) <= limit:
        return text
    end = 2 * (limit - units(suffix))
    excerpt = text.encode('utf-16-le')[:end].decode('utf-16-le', errors='ignore')
    return excerpt.rstrip() + suffix


def preview(text, limit):
    if units(text) <= limit:
        return text
    available = limit - units(MORE)
    selected = []
    for block in text.split('\n\n'):
        size = units(block) + (2 if selected else 0)
        if size > available:
            break
        selected.append(block)
        available -= size
    return ('\n\n'.join(selected) if selected else clip(text, available, ' …')) + MORE


def compose(lines, source, report=None):
    source = safe_url(source.split('?', 1)[0].split('#', 1)[0])
    report = safe_url(report or '')
    details = DETAILS + (' ' + report if report else '')
    footer = SOURCE + source + details
    if units(footer) > LIMIT // 2:
        footer = SOURCE + details
    return preview('\n\n'.join(lines), LIMIT - units(footer)) + footer


def safe_url(url):
    if any(char.isspace() or ord(char) < 32 or char == '\\' for char in url):
        return ''
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return ''
    if target.scheme not in ('http', 'https') or not target.host or target.userinfo or target.query or target.fragment:
        return ''
    return str(target) if units(str(target)) <= LIMIT // 2 - 80 else ''


def with_source_link(text):
    before, marker, report = text.rpartition(DETAILS)
    body, source_marker, source = before.rpartition(SOURCE)
    if not marker or not source_marker or (source and not safe_url(source)):
        return text, []
    target = safe_url(report[1:]) if report.startswith(' ') else ''
    if report and not target:
        return text, []
    entities = []
    for url, label in ((source, 'Quelle öffnen'), (target, 'Auswertung im Webmonitor öffnen')):
        if url:
            body += '\n\n'
            entities.append({'type': 'text_link', 'url': url, 'offset': units(body), 'length': units(label)})
            body += label
    return body if target else body + DETAILS, entities


def for_delivery(text):
    text, links = with_source_link(text)
    if not text.startswith('KI-Webmonitor · '):
        return text, links
    headings = []
    offset = 0
    for line in text.split('\n'):
        length = units(line)
        if (offset == 0 or line in ('KI-Auswertung', 'Prüfung fehlgeschlagen', 'Prüfung unvollständig')
                or line.startswith(ENTRY) or re.fullmatch(r'\d+ Änderung(?:en)?', line)):
            headings.append({'type': 'bold', 'offset': offset, 'length': length})
        offset += length + 1
    return text, headings + links
