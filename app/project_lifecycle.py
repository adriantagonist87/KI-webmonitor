"""Delete one confirmed, idle project, without deleting shared archive objects."""
import re
import store
import site_access
import retention
from functools import wraps


def storage_guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with retention.maintenance_lock():
            return function(*args, **kwargs)
    return guarded

@storage_guard
def remove(pid):
    candidates = set(); run_ids = []
    with site_access.secret_removal(pid), store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT id FROM projects WHERE id=?', (pid,)).fetchone(): raise ValueError('Projekt nicht gefunden.')
        if c.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('running','queued')", (pid,)).fetchone():
            raise ValueError('Die Prüfung dieses Projekts muss zuerst abgeschlossen sein.')
        run_ids = [r['id'] for r in c.execute('SELECT id FROM runs WHERE project_id=?', (pid,))]
        for r in c.execute('SELECT file,screenshot,html FROM snapshots WHERE project_id=?', (pid,)):
            candidates.update(v for v in r if v)
        # The outer vault transaction restores secrets if SQL or commit fails.
        c.execute('DELETE FROM report_changes WHERE change_id IN (SELECT id FROM changes WHERE project_id=?)', (pid,))
        c.execute('DELETE FROM change_reviews WHERE change_id IN (SELECT id FROM changes WHERE project_id=?)', (pid,))
        c.execute('DELETE FROM changes WHERE project_id=?', (pid,))
        c.execute('DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE project_id=?)', (pid,))
        c.execute('DELETE FROM snapshots WHERE project_id=?', (pid,))
        c.execute('DELETE FROM runs WHERE project_id=?', (pid,))
        c.execute('DELETE FROM state WHERE key IN (?,?)', ('content_scopes:' + pid, 'site_auth:' + pid))
        c.execute('DELETE FROM state WHERE key IN (?,?)', ('project_icon:'+pid,'project_icon_attempt:'+pid))
        c.execute('DELETE FROM state WHERE key=?',('crawl_pace:'+pid,))
        c.execute('DELETE FROM state WHERE key IN (?,?)',('browser_mode:'+pid,'browser_flow:'+pid))
        c.execute('DELETE FROM state WHERE key=?',('navigation_macros:'+pid,))
        index_prefix='collection_index:'+pid+':'
        c.execute('DELETE FROM state WHERE substr(key,1,?)=?', (len(index_prefix),index_prefix))
        c.execute('DELETE FROM projects WHERE id=?', (pid,))
        referenced = {v for r in c.execute('SELECT file,screenshot,html FROM snapshots') for v in r if v}
    base = (store.DATA / 'artifacts').resolve()
    folders = []
    for rid in run_ids:
        if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', rid) and rid != 'objects':
            folder = base / rid
            if folder.is_dir() and not folder.is_symlink():
                folders.append(folder)
                candidates.update(p.relative_to(store.DATA).as_posix() for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
    for relative in candidates - referenced:
        path = store.DATA / relative
        if path.is_symlink() or not path.resolve().is_relative_to(base): continue
        if any(parent.is_symlink() for parent in path.parents if parent != store.DATA.parent): continue
        if path.is_file(): path.unlink()
    for folder in folders:
        for path in sorted([p for p in folder.rglob('*') if p.is_dir() and not p.is_symlink()], key=lambda p: len(p.parts), reverse=True) + [folder]:
            try: path.rmdir()
            except OSError: pass
