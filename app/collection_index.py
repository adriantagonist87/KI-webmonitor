"""Pure, bounded comparison of observed collection entries.

The caller owns browser navigation, date-window selection and persistence. No
store, network, model, credentials or current clock is read here. A successful
scan may cover only the newest prefix: unvisited older records remain retained
and must never be reported as removals or as freshly verified.

Persist ``plan_update(...)["next_state"]`` only when ``commit`` is true, for
example under ``collection_index:<project-id>:<scope_fingerprint(...)>``. The
scope describes stable selectors and the exact filter identity; calendar years
and per-run cutoff dates are deliberately not scope inputs.
"""
from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from html import escape
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlsplit


VERSION = 1
MAX_RECORDS = 10000
MAX_URL = 4096
MAX_TITLE = 1000
MAX_TEXT = 12000
MAX_CHARACTERS = 16 * 1024 * 1024
FIELDS = ('url', 'title', 'date', 'text')
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?\Z')
_PRIVATE_QUERY_KEYS = {'password', 'passwort', 'pw', 'pwd', 'api_key', 'apikey',
                       'api-key', 'token', 'access_token', 'refresh_token',
                       'authorization', 'x-amz-security-token', 'abonummer',
                       'abonr', 'subscription_number', 'customer_number',
                       'kundennummer', 'plz', 'postcode', 'postal_code'}


def _string(value, label, limit, *, empty=False, multiline=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('Ungültiges oder zu langes Sammlungsfeld: ' + label)
    if any(ord(c) < 32 and not (multiline and c in '\n\r\t') for c in value):
        raise ValueError('Steuerzeichen im Sammlungsfeld: ' + label)
    value = unicodedata.normalize('NFC', value).strip()
    if not value and not empty:
        raise ValueError('Leeres Sammlungsfeld: ' + label)
    return value


def _record(record):
    if not isinstance(record, dict) or set(record) != set(FIELDS):
        raise ValueError('Sammlungseintrag benötigt genau URL, Titel, Datum und Text.')
    url = _string(record['url'], 'url', MAX_URL)
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or '\\' in url or any(c.isspace() for c in url)
                or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError()
        fragments = (parsed.fragment, parsed.fragment.partition('?')[2])
        if any(key.casefold() in _PRIVATE_QUERY_KEYS
               for query in (parsed.query, *fragments)
               for key, _ in parse_qsl(query, keep_blank_values=True, max_num_fields=100)):
            raise ValueError()
    except ValueError:
        raise ValueError('Sammlungseinträge benötigen eine HTTPS-URL ohne Zugangsdaten.') from None
    # Preserve the actually observed URL, including queries and SPA fragments.
    title = ' '.join(_string(record['title'], 'title', MAX_TITLE).split())
    observed_date = _string(record['date'], 'date', 10)
    if not _DATE.fullmatch(observed_date):
        raise ValueError('Das beobachtete Datum muss YYYY-MM oder YYYY-MM-DD sein.')
    try:
        parts = [int(part) for part in observed_date.split('-')]
        date(*parts, *([1] if len(parts) == 2 else []))
    except ValueError:
        raise ValueError('Ungültiges beobachtetes Kalenderdatum.') from None
    # A month remains a month. Never invent a day, or derive a year from a title.
    text = _string(record['text'], 'text', MAX_TEXT, empty=True, multiline=True)
    text = '\n'.join(line.rstrip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'))
    return {'url': url, 'title': title, 'date': observed_date, 'text': text}


def normalize_records(records):
    """Return fresh URL-sorted records; dedupe identical repeated observations.

    Conflicting observations of the same URL are rejected rather than choosing
    a potentially stale row. Date precision is preserved and dates are never
    extracted from titles or prose.
    """
    if not isinstance(records, (list, tuple)) or len(records) > MAX_RECORDS:
        raise ValueError('Maximal 10000 Sammlungseinträge sind erlaubt.')
    by_url = {}
    characters = 0
    for value in records:
        normalized = _record(value)
        previous = by_url.get(normalized['url'])
        if previous is not None:
            if previous != normalized:
                raise ValueError('Widersprüchliche Sammlungseinträge für dieselbe URL.')
            continue
        characters += sum(len(value) for value in normalized.values())
        if characters > MAX_CHARACTERS:
            raise ValueError('Die Sammlung überschreitet die erlaubte Textmenge.')
        by_url[normalized['url']] = normalized
    return [by_url[url] for url in sorted(by_url)]


def canonical(existing, new, first=False):
    """Return a stable union, replacing only URLs that were actually rescanned.

    ``first`` marks an initial scan for caller readability; it never authorizes
    deletion/reset of existing evidence. Baseline/delta semantics belong to
    ``plan_update``. Missing records always remain in this canonical union.
    """
    if type(first) is not bool:
        raise ValueError('first muss ein boolescher Wert sein.')
    old = normalize_records(existing)
    observed = normalize_records(new)
    merged = {record['url']: record for record in old}
    merged.update({record['url']: record for record in observed})
    return normalize_records(list(merged.values()))


def _state(value):
    if value is None:
        return {'version': VERSION, 'baseline_ready': False, 'records': []}
    if (not isinstance(value, dict)
            or set(value) != {'version', 'baseline_ready', 'records'}
            or type(value['version']) is not int or value['version'] != VERSION
            or type(value['baseline_ready']) is not bool):
        raise ValueError('Ungültiger gespeicherter Sammlungsindex.')
    return {'version': VERSION, 'baseline_ready': value['baseline_ready'],
            'records': normalize_records(value['records'])}


def plan_update(old_state, records, complete):
    """Return a JSON-serializable, side-effect-free update plan.

    ``complete`` means the caller confirmed the required bounded scan, e.g. the
    entire first year or a later prefix reaching a known entry. It does *not*
    mean the entire historical website was read. A partial/failed scan has
    ``next_state=None`` and produces no additions/corrections to commit.

    ``retained`` contains old records not visited in this scan; ``verified_urls``
    identifies actual fresh observations. ``updated`` contains ``before``,
    ``after`` and changed field names. ``removed`` is always empty.
    """
    if type(complete) is not bool:
        raise ValueError('complete muss ein boolescher Wert sein.')
    old = _state(old_state)
    observed = normalize_records(records)
    if not complete:
        return {'commit': False, 'next_state': None, 'baseline': False,
                'added': [], 'updated': [], 'retained': old['records'],
                'unchanged': [], 'removed': [], 'verified_urls': [],
                'observed_count': len(observed), 'total_count': len(old['records'])}
    baseline = not old['baseline_ready']
    old_by_url = {record['url']: record for record in old['records']}
    observed_by_url = {record['url']: record for record in observed}
    merged = canonical(old['records'], observed, first=baseline)
    added, updated, unchanged = [], [], []
    for record in observed:
        previous = old_by_url.get(record['url'])
        if previous is None:
            if not baseline:
                added.append(record)
        elif previous != record:
            if not baseline:
                updated.append({'url': record['url'], 'before': previous,
                                'after': record,
                                'fields': [key for key in FIELDS[1:] if previous[key] != record[key]]})
        else:
            unchanged.append(record)
    retained = [record for record in old['records'] if record['url'] not in observed_by_url]
    return {'commit': True,
            'next_state': {'version': VERSION, 'baseline_ready': True, 'records': merged},
            'baseline': baseline, 'added': added, 'updated': updated,
            'retained': retained, 'unchanged': unchanged, 'removed': [],
            'verified_urls': [record['url'] for record in observed],
            'observed_count': len(observed), 'total_count': len(merged)}


def scope_fingerprint(selectors, filter_identity):
    """Hash structural selectors and exact filter; no year/cutoff/clock input.

    Callers should pass semantic filter identity (e.g. ``Normen``), not a dynamic
    heading containing a result count. Selectors are a short string mapping.
    """
    if not isinstance(selectors, Mapping) or not selectors or len(selectors) > 20:
        raise ValueError('Eine begrenzte strukturelle Sammlungs-Auswahl ist erforderlich.')
    selected = {}
    for key, value in selectors.items():
        key = _string(key, 'selector key', 80)
        if key in selected:
            raise ValueError('Doppelte normalisierte Sammlungs-Auswahl.')
        selected[key] = _string(value, 'selector', 500)
    identity = _string(filter_identity, 'filter identity', 1000)
    raw = json.dumps({'version': VERSION, 'selectors': selected, 'filter': identity},
                     ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return sha256(raw.encode('utf-8')).hexdigest()


def canonical_text(records):
    """Stable JSON text for one aggregate archive snapshot and exact text diff."""
    return json.dumps({'version': VERSION, 'records': normalize_records(records)},
                      ensure_ascii=False, sort_keys=True, indent=2)


def index_html(records, scope):
    """Inert, escaped collection fragment with the engine's comparison marker."""
    scope = _string(scope, 'scope', 240)
    html = ['<div data-kiwm-collection-index="1" data-kiwm-visual-scope="collection-index-v1" data-kiwm-scope="' + escape(scope, quote=True) + '">']
    for record in normalize_records(records):
        html.append('<article><h3><a href="' + escape(record['url'], quote=True) + '">'
                    + escape(record['title']) + '</a></h3><p><time datetime="'
                    + escape(record['date'], quote=True) + '">' + escape(record['date'])
                    + '</time></p><p>' + escape(record['text']) + '</p></article>')
    html.append('</div>')
    return '\n'.join(html)
