"""Read-only global catalog of retained original documents and bounded downloads.

Only snapshot-referenced original files are exposed. Page HTML, screenshots,
browser profiles, prompts and extracted document text are never catalog inputs.
Streaming downloads own a plain flock descriptor: unlike the normal reentrant
maintenance context, that descriptor may cross Starlette worker threads safely.
"""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader
import retention
import store
import document_names
from document_inventory import document_identity
from document_types import EXTENSIONS, MEDIA


MAX_FILE_BYTES = 30 * 1024**2
MAX_CORE_XML_BYTES = 256 * 1024
MAX_ZIP_BYTES = 2 * 1024**3
MAX_ZIP_FILES = 5000
CHUNK_SIZE = 128 * 1024
MAX_QUERY = 512
SORTS = {'tags', 'project', 'type', 'name', 'document_name', 'changed_at'}
_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_RESERVED = re.compile(r'(?i)(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9]|lpt[1-9])(?:\..*)?\Z')
ZIP_FOLDER_BYTES = 80
ZIP_FILENAME_BYTES = 120


class FileUnavailable(FileNotFoundError):
    pass


class InvalidQuery(ValueError):
    pass


class ArchiveTooLarge(ValueError):
    pass


class IntegrityError(ValueError):
    pass


def _normal(value):
    return unicodedata.normalize('NFKC', str(value)).casefold()


def _date(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _iso(value):
    parsed = _date(value)
    return parsed.isoformat() if parsed else ''


def _root():
    root = Path(store.DATA)
    if not root.is_absolute() or root.resolve() != root or any(p.is_symlink() for p in (root, *root.parents)):
        raise FileUnavailable('Die Dokumentablage ist nicht verfügbar.')
    return root


def _relative(value):
    if not isinstance(value, str) or len(value) > 500 or '\\' in value or ':' in value or '\x00' in value:
        raise FileUnavailable('Ungültiger Dokumentverweis.')
    path = PurePosixPath(value)
    if (path.is_absolute() or value != path.as_posix() or len(path.parts) != 3
            or path.parts[0] != 'artifacts' or any(part in ('.', '..') for part in path.parts)
            or Path(path.name).suffix.lower() not in EXTENSIONS):
        raise FileUnavailable('Ungültiger Dokumentverweis.')
    if path.parts[1] != 'objects' and not _ID.fullmatch(path.parts[1]):
        raise FileUnavailable('Ungültiger Dokumentverweis.')
    return path


@contextmanager
def _open_original(relative):
    """Resolve each directory through openat/O_NOFOLLOW, never through a link."""
    root = _root()
    rel = _relative(relative)
    handles = []
    file_handle = None
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(descriptor)
        for part in rel.parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            handles.append(descriptor)
        descriptor = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
            os.close(descriptor)
            raise FileUnavailable('Die Originaldatei fehlt oder überschreitet das Dateilimit.')
        file_handle = os.fdopen(descriptor, 'rb')
        yield file_handle, info
    except (OSError, ValueError) as error:
        if isinstance(error, (FileUnavailable, IntegrityError)):
            raise
        raise FileUnavailable('Die Originaldatei ist nicht mehr verfügbar.') from None
    finally:
        if file_handle is not None:
            file_handle.close()
        for descriptor in reversed(handles):
            os.close(descriptor)


def _clean_text(value, maximum=1024):
    if not isinstance(value, str):
        return ''
    value = ''.join(c for c in value if ord(c) >= 32 or c in '\t\n\r')
    return ' '.join(value.split())[:maximum]


def safe_filename(value, extension='.bin'):
    """One portable basename, also safe as a ZIP member and HTTP filename."""
    value = unicodedata.normalize('NFKC', str(value or ''))
    value = ''.join(c for c in value if unicodedata.category(c) not in ('Cc','Cf','Cs'))
    value = re.sub(r'[<>:"/\\|?*]', '_', value).strip(' .')
    value = ' '.join(value.split())
    if not value:
        value = 'Dokument'
    extension = extension if extension in EXTENSIONS else '.bin'
    if Path(value).suffix.lower() != extension:
        value = (str(Path(value).with_suffix('')) if Path(value).suffix else value) + extension
    stem = value[:-len(extension)].rstrip(' .')[:150].rstrip(' .') or 'Dokument'
    value = stem + extension
    if _RESERVED.fullmatch(value):
        value = '_' + value
    return value


def _name(url, extension):
    try:
        name = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    except ValueError:
        name = ''
    return safe_filename(name, extension)


def _metadata(raw, extension):
    result = {}
    if extension == '.pdf' or raw.lstrip().startswith(b'%PDF-'):
        try:
            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted and not reader.decrypt(''):
                return result
            metadata = reader.metadata or {}
            for key, label in [('/Title','Titel'),('/Author','Autor'),('/Subject','Thema'),('/Keywords','Schlagwörter'),
                               ('/CreationDate','Erstellt'),('/ModDate','Dokumentdatum')]:
                value = _clean_text(metadata.get(key))
                if value:
                    result[label] = value
        except Exception:
            return {}
    elif extension in ('.xlsx', '.docx', '.dotx', '.pptx'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                matches = [info for info in archive.infolist() if info.filename == 'docProps/core.xml']
                if len(matches) != 1 or matches[0].file_size > MAX_CORE_XML_BYTES or matches[0].flag_bits & 1:
                    return {}
                with archive.open(matches[0]) as source:
                    core = source.read(MAX_CORE_XML_BYTES + 1)
                if len(core) > MAX_CORE_XML_BYTES:
                    return {}
            # XML may be UTF-16/32. NUL-separated DTD/entity declarations must
            # be rejected before ElementTree can expand internal entities.
            lowered = core.replace(b'\x00',b'').lower()
            if b'<!doctype' in lowered or b'<!entity' in lowered:
                return {}
            parsed = ET.fromstring(core)
            labels = {'title':'Titel','creator':'Autor','subject':'Thema','keywords':'Schlagwörter',
                      'created':'Erstellt','modified':'Dokumentdatum','lastModifiedBy':'Zuletzt bearbeitet von'}
            for child in parsed:
                key = child.tag.rsplit('}', 1)[-1]
                if key in labels:
                    value = _clean_text(child.text)
                    if value:
                        result[labels[key]] = value
        except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile, RuntimeError):
            return {}
    return result


@lru_cache(maxsize=512)
def _cached_metadata(root, relative, sha, size, modified_ns):
    # Cache is bounded and volatile. Every catalog request still verifies that
    # the original exists; a cached title never resurrects an expired file.
    with _open_original(relative) as (source, info):
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        return ()
    return tuple(_metadata(raw, Path(relative).suffix.lower()).items())


def _query(value):
    if not isinstance(value, str) or len(value) > MAX_QUERY or '\x00' in value:
        raise InvalidQuery('Die Suche darf höchstens 512 Zeichen enthalten.')
    terms = _normal(value).split()
    if len(terms) > 32:
        raise InvalidQuery('Die Suche enthält zu viele Suchbausteine.')
    patterns = []
    for term in terms:
        # Only * and ? are special. Brackets retain their literal spelling.
        escaped = ''.join('[[]' if c == '[' else '[]]' if c == ']' else c for c in term)
        patterns.append('*' + escaped + '*')
    return value.strip(), patterns


def _matches(row, patterns):
    corpus = _normal(' '.join([row['filename'], row['document_name'], row['project_name'], row['file_type'], row['url'],
                              row['sha256'], row['changed_at'], *(tag['name'] for tag in row['tags']), *row['metadata'].values()]))
    return all(fnmatch.fnmatchcase(corpus, pattern) for pattern in patterns)


def _catalog():
    with store.db() as database:
        database.execute('BEGIN')
        records = [dict(row) for row in database.execute('''SELECT s.id,s.run_id,s.project_id,s.url,s.captured,s.sha256,s.file,
            p.name AS project_name,p.tags,r.status FROM snapshots s
            JOIN projects p ON p.id=s.project_id JOIN runs r ON r.id=s.run_id
            WHERE s.kind='document' ORDER BY s.captured,s.id''')]
        tag_rows = {row['key']: dict(row) for row in database.execute('SELECT * FROM tags')}
        transitions = [dict(row) for row in database.execute('''SELECT c.project_id,c.url,c.detected,n.sha256
            FROM changes c JOIN snapshots n ON n.id=c.current_id
            LEFT JOIN snapshots p ON p.id=c.previous_id
            WHERE n.kind='document' AND (p.id IS NULL OR p.sha256<>n.sha256)''')]
    dates, groups, current, changed_dates = {}, {}, {}, {}
    for row in records:
        try:
            source_key = document_identity(row['url'])
        except ValueError:
            continue
        group = (row['project_id'], source_key)
        version = (*group, row['sha256'])
        observed = _iso(row['captured'])
        if version not in dates or (observed and observed < dates[version]):
            dates[version] = observed
        if row['status'] in ('success','partial'):
            current[group] = row['sha256']
        if row.get('file') and isinstance(row['sha256'], str) and _HASH.fullmatch(row['sha256']):
            groups[version] = row
    # A returning old binary is a new observed transition, not its old PDF date.
    for transition in transitions:
        try:
            version = (transition['project_id'], document_identity(transition['url']), transition['sha256'])
        except ValueError:
            continue
        observed = _iso(transition['detected'])
        if observed and observed > changed_dates.get(version, ''):
            changed_dates[version] = observed
    rows, unavailable = [], 0
    root = _root()
    for version, row in groups.items():
        relative = row['file']
        try:
            with _open_original(relative) as (source, info):
                size, modified_ns = info.st_size, info.st_mtime_ns
                file_saved = datetime.fromtimestamp(info.st_mtime,timezone.utc)
            metadata = dict(_cached_metadata(str(root), relative, row['sha256'], size, modified_ns))
        except FileUnavailable:
            unavailable += 1
            continue
        try:
            tags = json.loads(row['tags'])
            if not isinstance(tags, list):
                tags = []
        except (ValueError, TypeError):
            tags = []
        chips = []
        for tag in tags:
            if not isinstance(tag, str):
                continue
            known = tag_rows.get(tag.casefold(), {})
            chips.append({'name':tag,'label':tag,'key':tag.casefold(),'color':known.get('color','#64748b')})
        extension = Path(relative).suffix.lower()
        filename = _name(row['url'], extension)
        document_name = document_names.display(row)
        is_current = current.get(version[:2], row['sha256']) == row['sha256']
        observed = dates[version]
        if version in changed_dates:
            observed = changed_dates[version]
            date_title = 'Zeitpunkt, an dem eine Änderung zu dieser Dateiversion erkannt wurde.'
        else:
            # Unchanged snapshot rows may be compacted by retention. Original
            # objects keep their first save time; imports can only supply the
            # earliest still-known snapshot timestamp, never a remote claim.
            saved = file_saved.isoformat()
            if 946684800 <= file_saved.timestamp() <= datetime.now(timezone.utc).timestamp()+86400:
                observed = min(observed,saved) if observed else saved
            date_title = 'Früheste verfügbare Erfassung dieser Dateiversion; kein Änderungsdatum des Servers.'
        rows.append({'id':row['id'],'snapshot_id':row['id'],'project_id':row['project_id'],
                     'project_name':row['project_name'],'tags':chips,'name':filename,'filename':filename,
                     'document_name':document_name['text'],'document_name_source':document_name['source'],
                     'document_name_origin':document_names.SOURCES[document_name['source']],
                     'file_type':extension[1:].upper(),'extension':extension,'changed_at':observed,
                     'changed_origin':'observed','changed_label':'Inhaltsstand erstmals beobachtet',
                     'changed_title':date_title,
                     'date_source':'Beobachteter Dateistand; kein behauptetes Änderungsdatum des Servers.',
                     'metadata':metadata,'version_label':'Aktuell' if is_current else 'Vorversion',
                     'is_current':is_current,'size':size,'sha256':row['sha256'],'url':row['url'],
                     'file':relative})
    return rows, unavailable


def list_files(query='', sort='changed_at', direction='desc'):
    query, patterns = _query(query)
    if sort not in SORTS or direction not in ('asc','desc'):
        raise InvalidQuery('Ungültige Tabellensortierung.')
    with retention.maintenance_lock(exclusive=False):
        rows, unavailable = _catalog()
    total = len(rows)
    selected = [row for row in rows if _matches(row, patterns)]
    key = {'tags':lambda row:_normal(' '.join(tag['name'] for tag in row['tags'])),
           'project':lambda row:_normal(row['project_name']), 'type':lambda row:row['file_type'],
           'name':lambda row:_normal(row['filename']), 'document_name':lambda row:_normal(row['document_name']),
           'changed_at':lambda row:row['changed_at']}[sort]
    selected.sort(key=lambda row:(_normal(row['filename']),_normal(row['project_name']),row['sha256']))
    selected.sort(key=key, reverse=direction=='desc')
    unique = {row['sha256']:row['size'] for row in rows}
    return {'files':selected,'total':total,'matched':len(selected),'unique_count':len(unique),
            'archive_count':len({(row['project_id'],row['sha256']) for row in rows}),
            'total_bytes':sum(unique.values()),'query':query,'sort':sort,'direction':direction,
            'unavailable':unavailable}


def _snapshot(sid):
    if not isinstance(sid, str) or not _ID.fullmatch(sid):
        raise FileUnavailable('Dokument nicht gefunden.')
    row = store.one('''SELECT s.id,s.file,s.sha256,s.url FROM snapshots s
        JOIN projects p ON p.id=s.project_id WHERE s.id=? AND s.kind='document' AND s.file IS NOT NULL''', (sid,))
    if not row or not _HASH.fullmatch(row['sha256']):
        raise FileUnavailable('Die Originaldatei wurde bereinigt oder ist nicht verfügbar.')
    return row


def _verified(row):
    with _open_original(row['file']) as (source, info):
        if hashlib.file_digest(source, 'sha256').hexdigest() != row['sha256']:
            raise IntegrityError('Die gespeicherte Originaldatei stimmt nicht mit ihrer Prüfsumme überein.')
        return info.st_size


def get_file(snapshot_id):
    """Validate one retained snapshot and return its public download descriptor."""
    with retention.maintenance_lock(exclusive=False):
        row = _snapshot(snapshot_id)
        size = _verified(row)
        extension = Path(row['file']).suffix.lower()
        return dict(row, filename=_name(row['url'],extension), media_type=MEDIA[extension],size=size)


def _stream_lock():
    import fcntl
    root = _root()
    descriptor = os.open(root/'.storage.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FileUnavailable('Archivzugriff ist nicht verfügbar.')
        os.fchmod(descriptor,0o600)
        fcntl.flock(descriptor,fcntl.LOCK_SH)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class Download:
    """A prepared, verified response; close is safe after cancel or on any thread."""
    def __init__(self, filename, media_type, descriptor, producer, size=None):
        self.filename, self.media_type, self.size = filename, media_type, size
        self._lock_fd, self._producer = descriptor, producer
        self._mutex = threading.Lock()
        self._used = False

    def iterator(self):
        with self._mutex:
            if self._used:
                raise RuntimeError('Download wurde bereits geöffnet.')
            self._used = True
        try:
            yield from self._producer()
        finally:
            self.close()

    def close(self):
        import fcntl
        with self._mutex:
            descriptor, self._lock_fd = self._lock_fd, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor,fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def prepare_download(snapshot_id):
    descriptor = _stream_lock()
    try:
        row = _snapshot(snapshot_id)
        size = _verified(row)
        extension = Path(row['file']).suffix.lower()
        def produce():
            with _open_original(row['file']) as (source, info):
                while chunk := source.read(CHUNK_SIZE):
                    yield chunk
        return Download(_name(row['url'],extension),MEDIA[extension],descriptor,produce,size)
    except BaseException:
        os.close(descriptor)
        raise


class _ZipSink:
    def __init__(self):
        self.chunks = deque()
        self.position = 0
    def write(self, value):
        if value:
            self.chunks.append(value)
            self.position += len(value)
        return len(value)
    def tell(self):
        return self.position
    def flush(self):
        pass
    def take(self):
        while self.chunks:
            yield self.chunks.popleft()


def _short_utf8(value, maximum):
    return value.encode('utf-8')[:maximum].decode('utf-8',errors='ignore').rstrip(' .')


def _zip_folders(rows):
    """Portable single components; collision-safe on case-insensitive filesystems."""
    projects = {row['project_id']:row['project_name'] for row in rows}
    folders, used = {}, {'dateien.json'}  # The manifest is a file at ZIP root.
    for pid in sorted(projects):
        value = unicodedata.normalize('NFKC',str(projects[pid] or ''))
        value = ''.join(c for c in value if unicodedata.category(c) not in ('Cc','Cf','Cs'))
        value = re.sub(r'[<>:"/\\|?*]','_',value).strip(' .') or 'Projekt'
        if _RESERVED.fullmatch(value):
            value = '_' + value
        base = _short_utf8(value,ZIP_FOLDER_BYTES) or 'Projekt'
        if _RESERVED.fullmatch(base):
            base = '_' + base
        token = hashlib.sha256(pid.encode('utf-8')).hexdigest()[:10]
        for counter in range(len(used)+2):
            suffix = '' if counter == 0 else '-'+token+('' if counter == 1 else '-'+str(counter))
            candidate = _short_utf8(base,ZIP_FOLDER_BYTES-len(suffix))+suffix
            if _normal(candidate) not in used:
                folders[pid] = candidate
                used.add(_normal(candidate))
                break
        else:
            raise ArchiveTooLarge('Projektordner konnten nicht eindeutig zugeordnet werden.')
    return folders


def _zip_name(filename, extension, sha, used):
    stem = filename[:-len(extension)]
    for counter in range(len(used)+2):
        suffix = '' if counter == 0 else '-'+sha[:12]+('' if counter == 1 else '-'+str(counter))
        short = _short_utf8(stem,ZIP_FILENAME_BYTES-len(suffix)-len(extension)) or 'Dokument'
        candidate = safe_filename(short+suffix+extension,extension)
        if candidate.casefold() not in used and candidate.casefold() != 'dateien.json':
            return candidate
    raise ArchiveTooLarge('Dateinamen im Gesamtdownload konnten nicht eindeutig zugeordnet werden.')


def prepare_zip():
    """Stream originals in project folders, deduplicating only within each project."""
    descriptor = _stream_lock()
    try:
        rows, unavailable = _catalog()
        if not rows:
            raise FileUnavailable('Keine offline gespeicherten Dokumente vorhanden.')
        folders = _zip_folders(rows)
        unique = {}
        for row in sorted(rows,key=lambda value:(value['project_id'],value['filename'].casefold(),value['sha256'])):
            unique.setdefault((row['project_id'],row['sha256']),row)
        if len(unique) > MAX_ZIP_FILES or sum(row['size'] for row in unique.values()) > MAX_ZIP_BYTES:
            raise ArchiveTooLarge('Der Gesamtdownload überschreitet 5.000 Dateien oder 2 GiB.')
        entries, used = [], {}
        for (pid,sha), row in unique.items():
            _verified(row)
            project_used = used.setdefault(pid,set())
            name = _zip_name(row['filename'],row['extension'],sha,project_used)
            project_used.add(name.casefold())
            entries.append((folders[pid]+'/'+name,row))
        names = {(row['project_id'],row['sha256']):name for name,row in entries}
        manifest = {'format':'ki-webmonitor-offline-documents','created_at':store.now(),
                    'format_version':2,'layout':'project-folders','project_folders':folders,
                    'unique_files':len({row['sha256'] for row in rows}),
                    'archive_files':len(entries),'unavailable':unavailable,
                    'documents':[{key:row[key] for key in ('project_id','project_name','snapshot_id','url','sha256','changed_at','version_label')}
                                 | {'filename':names[(row['project_id'],row['sha256'])],'tags':[tag['name'] for tag in row['tags']]}
                                 for row in rows]}
        index = json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
        def produce():
            sink = _ZipSink()
            with zipfile.ZipFile(sink,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                for name,row in entries:
                    with archive.open(name,'w',force_zip64=True) as target:
                        yield from sink.take()
                        with _open_original(row['file']) as (source, info):
                            while chunk := source.read(CHUNK_SIZE):
                                target.write(chunk)
                                yield from sink.take()
                    yield from sink.take()
                archive.writestr('dateien.json',index)
                yield from sink.take()
            yield from sink.take()
        return Download('Webmonitor-Dokumente-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.zip',
                        'application/zip',descriptor,produce)
    except BaseException:
        os.close(descriptor)
        raise
