"""One global tag registry; project assignments retain portable name lists."""
import hashlib
import json
import uuid
import store
from project_meta import normalize_color, normalize_tags

PALETTE = ['#1967d2', '#7c3aed', '#0f766e', '#b45309', '#be185d', '#0369a1']

def key(name): return name.casefold()
def default_color(name): return PALETTE[int(hashlib.sha256(key(name).encode()).hexdigest()[:8], 16) % len(PALETTE)]

def initialize(c):
    c.execute('CREATE TABLE IF NOT EXISTS tags(id TEXT PRIMARY KEY,name TEXT NOT NULL,key TEXT NOT NULL UNIQUE,color TEXT NOT NULL)')
    for row in c.execute('SELECT tags FROM projects').fetchall():
        try: names = normalize_tags(json.loads(row['tags']))
        except (ValueError, TypeError): continue
        for name in names:
            ident = uuid.uuid5(uuid.NAMESPACE_URL, 'ki-webmonitor-tag:' + key(name)).hex
            c.execute('INSERT OR IGNORE INTO tags VALUES(?,?,?,?)', (ident, name, key(name), default_color(name)))

def all_tags():
    return [{**r, 'label': r['name']} for r in store.rows('SELECT * FROM tags ORDER BY key')]

def assign(ids):
    if len(ids) > 12 or any(not isinstance(v, str) for v in ids): raise ValueError('Maximal 12 vorhandene Tags wählen.')
    lookup = {r['id']: r['name'] for r in all_tags()}
    if any(v not in lookup for v in ids): raise ValueError('Unbekannter Tag. Tags zuerst in der Seitenleiste verwalten.')
    return normalize_tags([lookup[v] for v in ids])

def existing_names(value):
    names = normalize_tags(value); lookup = {r['key']: r['name'] for r in all_tags()}
    if any(key(n) not in lookup for n in names): raise ValueError('Tags zuerst unter „Tags“ anlegen und anschließend auswählen.')
    return [lookup[key(n)] for n in names]

def decorate(project):
    try: names = normalize_tags(json.loads(project['tags']) if isinstance(project['tags'], str) else project['tags'])
    except (ValueError, TypeError): names = []
    lookup = {r['key']: r for r in all_tags()}
    return [lookup.get(key(n), {'id': '', 'name': n, 'label': n, 'key': key(n), 'color': default_color(n)}) for n in names]

def save(name, color, ident=''):
    names = normalize_tags([name])
    if len(names) != 1: raise ValueError('Bitte einen Tagnamen angeben.')
    name = names[0]; color = normalize_color(color)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM tags WHERE id=?', (ident,)).fetchone() if ident else None
        if ident and not old: raise ValueError('Tag nicht gefunden.')
        collision = c.execute('SELECT id FROM tags WHERE key=?', (key(name),)).fetchone()
        if collision and (not old or collision['id'] != old['id']): raise ValueError('Dieser Tag ist bereits vorhanden.')
        ident = ident or store.uid()
        if old:
            c.execute('UPDATE tags SET name=?,key=?,color=? WHERE id=?', (name, key(name), color, ident))
            for p in c.execute('SELECT id,tags FROM projects').fetchall():
                tags = json.loads(p['tags']); changed = [name if key(v) == old['key'] else v for v in tags]
                if changed != tags: c.execute('UPDATE projects SET tags=? WHERE id=?', (json.dumps(changed, ensure_ascii=False), p['id']))
        else: c.execute('INSERT INTO tags VALUES(?,?,?,?)', (ident, name, key(name), color))
    return ident

def remove(ident):
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        tag = c.execute('SELECT * FROM tags WHERE id=?', (ident,)).fetchone()
        if not tag: raise ValueError('Tag nicht gefunden.')
        for p in c.execute('SELECT id,tags FROM projects').fetchall():
            tags = [n for n in json.loads(p['tags']) if key(n) != tag['key']]
            c.execute('UPDATE projects SET tags=? WHERE id=?', (json.dumps(tags, ensure_ascii=False), p['id']))
        c.execute('DELETE FROM tags WHERE id=?', (ident,))
