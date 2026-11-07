"""Compose the stored notification and attach its explicit Telegram source link."""
import httpx


SOURCE = '\n\nQuelle: '
DETAILS = '\n\nVollständige Auswertung im Webmonitor.'
LIMIT = 3900 * 2  # UTF-16 bytes, with a margin below Telegram's limit.


def compose(lines, source):
    url = source.split('?', 1)[0].split('#', 1)[0]
    footer = SOURCE + url + DETAILS
    if len(footer.encode('utf-16-le')) > LIMIT // 2:
        footer = DETAILS
    remaining = LIMIT - len(footer.encode('utf-16-le'))
    body = '\n\n'.join(lines)
    raw = body.encode('utf-16-le')
    if len(raw) > remaining:
        body = raw[:remaining - 4].decode('utf-16-le', errors='ignore') + '\n…'
    return body + footer


def with_source_link(text):
    body, marker, footer = text.rpartition(SOURCE)
    if not marker or not footer.endswith(DETAILS):
        return text, []
    url = footer[:-len(DETAILS)]
    if any(char.isspace() or ord(char) < 32 or char == '\\' for char in url):
        return text, []
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        return text, []
    if target.scheme not in ('http', 'https') or not target.host or target.userinfo or target.query or target.fragment:
        return text, []
    prefix = body + '\n\n'
    label = 'Quelle öffnen'
    entity = {'type': 'text_link', 'url': str(target),
              'offset': len(prefix.encode('utf-16-le')) // 2,
              'length': len(label.encode('utf-16-le')) // 2}
    return prefix + label + DETAILS, [entity]
