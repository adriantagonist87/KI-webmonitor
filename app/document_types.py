"""Shared inert original-file types; no extraction, execution or archive unpacking.

Only bounded document/download suffixes are automatic link candidates. Images,
scripts and other inline web assets are deliberately absent. Unsupported files
reached through an explicit file response can still be retained as .bin.
"""
from pathlib import PurePosixPath
from urllib.parse import urlsplit


MEDIA = {
    '.pdf': 'application/pdf',
    '.doc': 'application/msword', '.dot': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.dotx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
    '.xls': 'application/vnd.ms-excel', '.xlt': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xlsm': 'application/vnd.ms-excel.sheet.macroenabled.12',
    '.xltx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.template',
    '.xltm': 'application/vnd.ms-excel.template.macroenabled.12',
    '.ppt': 'application/vnd.ms-powerpoint', '.pps': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.ppsx': 'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
    '.rtf': 'application/rtf',
    '.odt': 'application/vnd.oasis.opendocument.text',
    '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
    '.odp': 'application/vnd.oasis.opendocument.presentation',
    '.zip': 'application/zip',
    '.txt': 'text/plain', '.csv': 'text/csv', '.xml': 'application/xml',
    '.bin': 'application/octet-stream',
}
EXTENSIONS = frozenset(MEDIA)
DOC_EXT = tuple(extension for extension in MEDIA if extension != '.bin')
_MIME_EXTENSIONS = {}
for _extension, _media in MEDIA.items():
    _MIME_EXTENSIONS.setdefault(_media, []).append(_extension)
for _alias, _canonical in {'text/rtf': 'application/rtf', 'text/xml': 'application/xml',
                           'application/x-zip-compressed': 'application/zip'}.items():
    _MIME_EXTENSIONS[_alias] = _MIME_EXTENSIONS[_canonical]
DOC_MEDIA = {media: extensions[0] for media, extensions in _MIME_EXTENSIONS.items()}


class HTMLDocumentError(ValueError):
    """An apparent file response is an HTML page and needs normal navigation."""


def document_suffix(url, media, raw):
    """Classify a storage name from URL/MIME and existing PDF/HTML magic only.

    A shared generic Office MIME preserves a matching known URL suffix, e.g.
    .dot versus .doc or .xlt versus .xls. An extensionless endpoint uses the
    canonical MIME suffix. Unknown originals remain inert .bin files.
    """
    if not isinstance(raw, bytes):
        raise ValueError('Originaldatei muss aus vollständigen Bytes bestehen.')
    mime = media.split(';', 1)[0].strip().casefold() if isinstance(media, str) else ''
    head = raw[:1024].lstrip(b'\xef\xbb\xbf \t\r\n').lower()
    if mime in ('text/html', 'application/xhtml+xml') or head.startswith((b'<!doctype html', b'<html')):
        raise HTMLDocumentError('Link liefert eine HTML-Unterseite statt einer Datei')
    if raw.lstrip().startswith(b'%PDF'):
        return '.pdf'
    suffix = PurePosixPath(urlsplit(url).path).suffix.casefold()
    if mime == 'application/octet-stream':
        return suffix if suffix in DOC_EXT else '.bin'
    matching = _MIME_EXTENSIONS.get(mime)
    if matching:
        return suffix if suffix in matching else matching[0]
    return suffix if suffix in DOC_EXT else '.bin'
