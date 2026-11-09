"""Current scoped document links and one shared per-run attempt budget."""
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urldefrag, urlsplit
from document_types import DOC_MEDIA

DOCUMENT_LIMIT = 128


def document_identity(url: str) -> str:
    """Comparison key only: encode path spaces, discard a fragment and epoch `ts`.

    All other query pairs, including blanks, duplicates and version fields, keep
    their order and values. Callers must retain the original URL for downloading,
    provenance and stored snapshots; this key is not a replacement download URL.
    """
    parts = urlsplit(url)
    pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not (key == 'ts' and value.isascii() and value.isdigit() and 9 <= len(value) <= 13)]
    return parts._replace(path=parts.path.replace(' ', '%20'), query=urlencode(pairs), fragment='').geturl()


def _url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(character.isspace() or ord(character) < 32 for character in value):
        return None
    try:
        result = urldefrag(value)[0]
        parsed = urlsplit(result)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        # Accessing the port also rejects malformed URL port strings.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        return result
    except ValueError:
        return None


def document_mime(value):
    """Return only a declared known document MIME, never arbitrary page types."""
    if (not isinstance(value,str) or len(value)>160
            or any(ord(character)<32 or ord(character)==127 for character in value)):
        return None
    mime=value.split(';',1)[0].strip().casefold()
    return mime if mime in DOC_MEDIA else None


def declared_file(link):
    """An explicit download attribute or whitelisted MIME is a file declaration."""
    if not isinstance(link,Mapping):return False
    download=link.get('download')
    if download is True:return True
    if (isinstance(download,str) and 0<len(download)<=200 and download.strip()
            and not any(ord(character)<32 or ord(character)==127 for character in download)):
        return True
    return document_mime(link.get('type')) is not None


def collect_documents(captured, allowed_callback, extensions, known_documents=()):
    """Collect only the latest captured scope, never navigation or historical URLs.

    A present metadata.links list is authoritative, including an empty list.
    String-only item.links is a fallback for captures without that metadata.
    First discovery order is retained; a later nonempty label can improve a URL
    used as its own initial label without changing its position.
    A known downloaded URL is counted only while still linked in a current
    scope; it never adds navigation, superseded scopes or arbitrary travel URLs.
    """
    result = {}
    if not isinstance(captured, Mapping):
        return result
    suffixes = tuple(extension.casefold() for extension in extensions
                     if isinstance(extension, str) and extension.startswith('.'))
    if not suffixes:
        return result
    values=(known_documents,) if isinstance(known_documents,str) else (known_documents or ())
    known={target for value in values if (target:=_url(value)) is not None}
    for item in captured.values():
        if not isinstance(item, Mapping):
            continue
        metadata = item.get('metadata')
        links = metadata.get('links') if isinstance(metadata, Mapping) else None
        authoritative = isinstance(links, (list, tuple))
        if not authoritative:
            links = item.get('links', ())
        if not isinstance(links, (list, tuple)):
            continue
        for link in links:
            label = '';declared=False
            if authoritative and isinstance(link, Mapping):
                target = _url(link.get('url'))
                declared=declared_file(link)
                if isinstance(link.get('label'), str):
                    label = ' '.join(link['label'].split())
            elif isinstance(link, str):
                target = _url(link)
            else:
                continue
            if not target or not (declared or target in known or urlsplit(target).path.casefold().endswith(suffixes)):
                continue
            try:
                if not allowed_callback(target):
                    continue
            except (ValueError, TypeError):
                continue
            if target not in result:
                result[target] = label or target
            elif result[target] == target and label:
                result[target] = label
    return result


def plan_documents(candidates, already_attempted, limit=DOCUMENT_LIMIT):
    """Return ordered (todo_urls, overflow_urls) for one global attempt budget.

    Successful and failed attempts consume the same budget. No attempted URL is
    returned again, even when rediscovered with a different fragment. The caller
    records attempts before downloading and explicitly reports every overflow.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError('Dokumentgrenze muss eine nichtnegative ganze Zahl sein.')
    values = (already_attempted,) if isinstance(already_attempted, str) else (already_attempted or ())
    attempted = {urldefrag(value)[0] for value in values if isinstance(value, str) and value}
    remaining = max(0, limit - len(attempted))
    todo, overflow, seen = [], [], set()
    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        url = urldefrag(value)[0]
        if url in attempted or url in seen:
            continue
        seen.add(url)
        if len(todo) < remaining:
            todo.append(url)
        else:
            overflow.append(url)
    return todo, overflow
