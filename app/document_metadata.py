"""Small, untrusted document-date evidence; never a publication/change oracle.

Only the two PDF date fields and four response headers are inspected. Exact
instants require seconds and an explicit numeric/GMT offset. Original bytes,
content extraction and comparison remain the caller's source of truth.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import io
import re
from zoneinfo import ZoneInfo

from pypdf import PdfReader


HTTP_HEADER_NAMES = ('last-modified', 'etag', 'content-type', 'content-length')
MAX_DATE_LENGTH = 96
MAX_ETAG_LENGTH = 256
MAX_CONTENT_TYPE = 160
MAX_WARNINGS = 8
MAX_WARNING_LENGTH = 160
_BERLIN = ZoneInfo('Europe/Berlin')
_PDF_DATE = re.compile(r"(?:D:)?(?P<digits>\d{4}(?:\d{2}){0,5})(?P<offset>Z|[+-]\d{2}'?\d{2}'?)?\Z", re.ASCII)
_HTTP_OFFSET = re.compile(r'(?:\b(?:GMT|UTC|UT)|[+-]\d{4})\Z', re.ASCII)
_HTTP_SECONDS = re.compile(r'\b\d{1,2}:\d{2}:\d{2}\b', re.ASCII)
_PRECISION = {4:'year', 6:'month', 8:'day', 10:'hour', 12:'minute', 14:'second'}


def _warning(warnings, message):
    if message not in warnings and len(warnings) < MAX_WARNINGS:
        warnings.append(message[:MAX_WARNING_LENGTH])


def _safe_text(value, limit):
    if not isinstance(value, str):
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value[:limit]


def _record(value):
    result = {'raw':None, 'status':'missing', 'precision':'unknown', 'timezone_explicit':False}
    if value is None or value == '':
        return result
    if not isinstance(value, str) or _safe_text(value, MAX_DATE_LENGTH) is None:
        result['status'] = 'invalid'
        return result
    result['raw'] = value[:MAX_DATE_LENGTH]
    if len(value) > MAX_DATE_LENGTH:
        result['status'] = 'too_long'
    return result


def _exact(record, date):
    try:
        record.update(status='exact', precision='second', timezone_explicit=True,
                      iso8601=date.isoformat(), utc=date.astimezone(timezone.utc).isoformat(),
                      europe_berlin=date.astimezone(_BERLIN).isoformat())
    except (ValueError, OverflowError):
        record.update(status='invalid', timezone_explicit=False)
        for key in ('iso8601', 'utc', 'europe_berlin'):
            record.pop(key, None)
    return record


def pdf_date(value):
    """Preserve incomplete/naive values without inventing an absolute instant."""
    result = _record(value)
    if result['status'] != 'missing' or result['raw'] is None:
        return result
    match = _PDF_DATE.fullmatch(value)
    if match is None:
        result['status'] = 'invalid'
        return result
    digits, offset = match.group('digits', 'offset')
    result.update(precision=_PRECISION[len(digits)], timezone_explicit=bool(offset))
    # Defaults are used solely to validate supplied calendar components. They
    # are never emitted as a completed date or used to infer missing precision.
    parts = [int(digits[:4]), 1, 1, 0, 0, 0]
    for index, start in enumerate((4, 6, 8, 10, 12), 1):
        if len(digits) >= start + 2:
            parts[index] = int(digits[start:start+2])
    try:
        date = datetime(*parts)
        zone = None
        if offset:
            if offset == 'Z':
                zone = timezone.utc
            else:
                plain = offset.replace("'", '')
                hours, minutes = int(plain[1:3]), int(plain[3:5])
                if hours > 23 or minutes > 59:
                    raise ValueError('Invalid PDF offset')
                zone = timezone((1 if plain[0] == '+' else -1) * timedelta(hours=hours, minutes=minutes))
    except (ValueError, OverflowError):
        result['status'] = 'invalid'
        return result
    if len(digits) < 14:
        result['status'] = 'partial'
        return result
    if zone is None:
        result['status'] = 'timezone_missing'
        return result
    return _exact(result, date.replace(tzinfo=zone))


def http_date(value):
    """Parse complete HTTP dates conservatively, retaining unsupported raw data."""
    result = _record(value)
    if result['status'] != 'missing' or result['raw'] is None:
        return result
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        result['status'] = 'invalid'
        return result
    if not _HTTP_SECONDS.search(value):
        result.update(status='partial', precision='minute', timezone_explicit=bool(_HTTP_OFFSET.search(value)))
        return result
    result['precision'] = 'second'
    explicit = bool(_HTTP_OFFSET.search(value))
    if not explicit:
        # asctime/no timezone has no offset. Unknown named zones must not gain
        # an inferred Europe/Berlin or UTC timezone from the machine settings.
        tail = value.rsplit(' ', 1)[-1]
        result['status'] = 'unsupported_timezone' if tail.isalpha() else 'timezone_missing'
        return result
    if date.tzinfo is None or date.utcoffset() is None:
        result['status'] = 'timezone_missing'
        return result
    return _exact(result, date)


def _headers(headers):
    # Inspect header names only; values of authentication/cookie headers are
    # never read. Runtime HTTPX headers and ordinary dictionaries are both
    # case-insensitive here. Bound an abnormal mapping as well as every value.
    result = dict.fromkeys(HTTP_HEADER_NAMES)
    try:
        for index, key in enumerate(headers.keys()):
            if index >= 256:
                break
            if isinstance(key, str) and key.lower() in result:
                result[key.lower()] = headers.get(key)
    except (AttributeError, TypeError, ValueError):
        pass
    return result


def _date_warning(warnings, label, record):
    if record['status'] not in ('exact', 'missing'):
        _warning(warnings, label + ': kein belegter genauer Zeitpunkt mit Sekunden und Zeitzone (' + record['status'] + ').')


def _http_metadata(headers, warnings):
    selected = _headers(headers)
    last = http_date(selected['last-modified'])
    _date_warning(warnings, 'HTTP Last-Modified', last)
    result = {'last_modified':last, 'etag':None, 'content_type':None, 'content_length':None}
    for source, target, limit in (('etag','etag',MAX_ETAG_LENGTH), ('content-type','content_type',MAX_CONTENT_TYPE)):
        value = selected[source]
        if value is None or value == '':
            continue
        cleaned = _safe_text(value, limit)
        if cleaned is None:
            _warning(warnings, 'HTTP ' + source + ': ungültiger Metadatenwert verworfen.')
        else:
            result[target] = cleaned
            if len(value) > limit:
                _warning(warnings, 'HTTP ' + source + ': Metadatenwert gekürzt.')
    length = selected['content-length']
    if length is not None and length != '':
        if isinstance(length, str) and re.fullmatch('[0-9]{1,20}', length):
            result['content_length'] = int(length)
        else:
            _warning(warnings, 'HTTP Content-Length: ungültige Größenangabe verworfen.')
    return result


def extract_document_metadata(raw, suffix, http_headers):
    """Return bounded metadata; metadata errors never fail a valid download."""
    warnings = []
    result = {'version':1, 'pdf':{}, 'http':_http_metadata(http_headers, warnings), 'warnings':warnings}
    is_pdf = suffix == '.pdf' or isinstance(raw, bytes) and raw.lstrip().startswith(b'%PDF-')
    if not is_pdf:
        return result
    dates = {'creation_date':pdf_date(None), 'modification_date':pdf_date(None)}
    result['pdf'] = dates
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 30 * 1024**2:
            raise ValueError('Invalid bounded PDF')
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF reading password required')
        values = reader.metadata or {}
        dates['creation_date'] = pdf_date(values.get('/CreationDate'))
        dates['modification_date'] = pdf_date(values.get('/ModDate'))
    except Exception:
        _warning(warnings, 'PDF-Datumsmetadaten konnten nicht gelesen werden; Originalsicherung und SHA-256-Vergleich bleiben maßgeblich.')
    for name, label in (('creation_date', 'PDF CreationDate'), ('modification_date', 'PDF ModDate')):
        _date_warning(warnings, label, dates[name])
    return result


def unavailable_document_metadata():
    """Fixed safe fallback; exception/header bodies never enter model evidence."""
    return {'version':1, 'pdf':{}, 'http':{'last_modified':http_date(None), 'etag':None,
            'content_type':None, 'content_length':None},
            'warnings':['Technische Dokumentmetadaten waren nicht verfügbar; Originalsicherung und SHA-256-Vergleich bleiben maßgeblich.']}


def compact_document_metadata(metadata):
    """Whitelist/reparse a small view; never trust imported extra header fields."""
    if not isinstance(metadata, dict) or type(metadata.get('version')) is not int or metadata['version'] != 1:
        return None
    warnings = []
    for item in metadata.get('warnings', []) if isinstance(metadata.get('warnings'), list) else []:
        cleaned = _safe_text(item, MAX_WARNING_LENGTH)
        if cleaned:
            _warning(warnings, cleaned)
    source_http = metadata.get('http') if isinstance(metadata.get('http'), dict) else {}
    source_last = source_http.get('last_modified') if isinstance(source_http.get('last_modified'), dict) else {}
    length = source_http.get('content_length')
    headers = {'last-modified':source_last.get('raw'), 'etag':source_http.get('etag'), 'content-type':source_http.get('content_type'),
               'content-length':str(length) if type(length) is int and length >= 0 else None}
    result = {'version':1, 'pdf':{}, 'http':_http_metadata(headers, warnings), 'warnings':warnings}
    source_pdf = metadata.get('pdf') if isinstance(metadata.get('pdf'), dict) else {}
    if source_pdf:
        for name in ('creation_date', 'modification_date'):
            value = source_pdf.get(name) if isinstance(source_pdf.get(name), dict) else {}
            result['pdf'][name] = pdf_date(value.get('raw'))
            _date_warning(warnings, 'PDF ' + name, result['pdf'][name])
    return result
