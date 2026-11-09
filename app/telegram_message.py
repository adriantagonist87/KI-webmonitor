"""Keep report structure in the outbox and add Telegram formatting at delivery."""
import re

import httpx
from bs4 import BeautifulSoup, Tag

from report_render import render_report


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


def report_blocks(value):
    soup = BeautifulSoup(str(render_report(value)), 'html.parser')
    for node in soup.select('details,table,pre'):
        node.decompose()
    for br in soup.find_all('br'):
        br.replace_with('\n')

    def clean(node):
        text = node.get_text() if isinstance(node, Tag) else str(node)
        return '\n'.join(line for row in text.splitlines() if (line := ' '.join(row.split())))

    section = []
    for node in soup.children:
        if node.name in ('ul', 'ol'):
            if section:
                yield '\n'.join(section)
                section = []
            for item in node.find_all('li', recursive=False):
                if text := clean(item):
                    yield '• ' + text
            continue
        text = clean(node)
        if not text:
            continue
        if node.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            if section:
                yield '\n'.join(section)
            section = [ENTRY + text.replace('\n', ' ')]
        elif section:
            section.append(text)
        else:
            yield text
    if section:
        yield '\n'.join(section)


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
