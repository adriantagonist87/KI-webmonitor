"""Admin-managed mail scopes. Recipients never acquire a web login."""
import json,re
from email.message import EmailMessage
import store,tag_registry

REPORT_DEFAULTS={'audience':"TEXT NOT NULL DEFAULT 'admin'",'parent_id':'TEXT','distribution_id':'TEXT','distribution_alias':'TEXT','recipient_email':'TEXT','selection_snapshot':"TEXT NOT NULL DEFAULT '{}'",'sent_at':'TEXT','attempts':'INTEGER NOT NULL DEFAULT 0','next_attempt':'TEXT'}

def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS distributions(
      id TEXT PRIMARY KEY,alias TEXT NOT NULL,alias_key TEXT NOT NULL,emails TEXT NOT NULL,
      project_ids TEXT NOT NULL,tag_ids TEXT NOT NULL,enabled INTEGER NOT NULL,
      created TEXT NOT NULL,updated TEXT NOT NULL,deleted_at TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS distributions_alias ON distributions(alias_key) WHERE deleted_at IS NULL;
    CREATE TABLE IF NOT EXISTS report_mail_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
      created TEXT NOT NULL,status TEXT NOT NULL,note TEXT NOT NULL);
    ''')
    columns={r[1] for r in c.execute('PRAGMA table_info(reports)')}
    for key,kind in REPORT_DEFAULTS.items():
        if key not in columns:c.execute('ALTER TABLE reports ADD COLUMN '+key+' '+kind)
    c.execute('CREATE INDEX IF NOT EXISTS report_parent ON reports(parent_id,created)')
    c.execute('CREATE INDEX IF NOT EXISTS report_mail_event_time ON report_mail_events(report_id,created,id)')

def email(value):
    if not isinstance(value,str) or not 3<=len(value)<=254 or not value.isascii() or re.search(r'[\s<>(),;\\\x00-\x1f\x7f]',value):raise ValueError('Bitte gültige einzelne E-Mail-Adressen ohne Anzeigenamen eingeben.')
    m=EmailMessage();m['To']=value;header=m['To']
    if header.defects or len(header.addresses)!=1 or header.addresses[0].addr_spec!=value or value.count('@')!=1:raise ValueError('Ungültige E-Mail-Adresse.')
    local,domain=value.rsplit('@',1)
    if len(local)>64 or '.' not in domain or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',label) for label in domain.split('.')):raise ValueError('Ungültige E-Mail-Adresse.')
    return local+'@'+domain.lower()

def addresses(value):
    raw=[item.strip() for item in re.split(r'[,;\r\n]+',value) if item.strip()] if isinstance(value,str) else value
    if not isinstance(raw,list) or len(raw)>100:raise ValueError('Maximal 100 E-Mail-Adressen je Verteiler.')
    result=[];seen=set()
    for item in raw:
        if not isinstance(item,str):raise ValueError('Ungültige E-Mail-Adresse.')
        item=item.strip()
        if not item:continue
        clean=email(item)
        if clean.casefold() not in seen:result.append(clean);seen.add(clean.casefold())
    if not result:raise ValueError('Mindestens eine E-Mail-Adresse angeben.')
    return result

def ids(value):
    if not isinstance(value,list) or len(value)>2000 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',x) for x in value):raise ValueError('Ungültige Projektauswahl oder Tag-Auswahl.')
    return sorted(set(value))

def decode(row):
    if not row:return None
    out=dict(row)
    for k in ('emails','project_ids','tag_ids'):out[k]=json.loads(out[k]) if isinstance(out[k],str) else out[k]
    return out

def all_lists(include_deleted=False):
    return [decode(r) for r in store.rows('SELECT * FROM distributions'+('' if include_deleted else ' WHERE deleted_at IS NULL')+' ORDER BY alias_key')]

def get(did):return decode(store.one('SELECT * FROM distributions WHERE id=?',(did,)))

def validate_record(row,*,check_targets=True):
    alias=row.get('alias','')
    if not isinstance(alias,str) or not 1<=len(alias.strip())<=100 or any(ord(c)<32 or ord(c)==127 for c in alias):raise ValueError('Alias mit 1 bis 100 Zeichen angeben.')
    alias=' '.join(alias.split());emails=addresses(row.get('emails',[]));projects=ids(row.get('project_ids',[]));tags=ids(row.get('tag_ids',[]))
    if not projects and not tags:raise ValueError('Mindestens ein Projekt oder einen Tag auswählen.')
    if check_targets:
        if set(projects)-{p['id'] for p in store.rows('SELECT id FROM projects')}:raise ValueError('Ein ausgewähltes Projekt existiert nicht mehr.')
        if set(tags)-{p['id'] for p in tag_registry.all_tags()}:raise ValueError('Ein ausgewählter Tag existiert nicht mehr.')
    if type(row.get('enabled',0)) is not int or row.get('enabled') not in (0,1):raise ValueError('Ungültiger Verteilerstatus.')
    return {'alias':alias,'alias_key':alias.casefold(),'emails':emails,'project_ids':projects,'tag_ids':tags,'enabled':row['enabled']}

def save(row,did=''):
    value=validate_record(row);stamp=store.now()
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM distributions WHERE id=? AND deleted_at IS NULL',(did,)).fetchone() if did else None
        if did and not old:raise ValueError('Verteiler nicht gefunden.')
        if c.execute('SELECT 1 FROM distributions WHERE alias_key=? AND deleted_at IS NULL AND id<>?',(value['alias_key'],did)).fetchone():raise ValueError('Dieser Alias wird bereits verwendet.')
        did=did or store.uid()
        c.execute('INSERT INTO distributions VALUES(?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET alias=excluded.alias,alias_key=excluded.alias_key,emails=excluded.emails,project_ids=excluded.project_ids,tag_ids=excluded.tag_ids,enabled=excluded.enabled,updated=excluded.updated',
          (did,value['alias'],value['alias_key'],json.dumps(value['emails']),json.dumps(value['project_ids']),json.dumps(value['tag_ids']),value['enabled'],old['created'] if old else stamp,stamp))
    return did

def toggle(did):
    old=get(did)
    if not old or old['deleted_at']:raise ValueError('Verteiler nicht gefunden.')
    store.execute('UPDATE distributions SET enabled=?,updated=? WHERE id=?',(int(not old['enabled']),store.now(),did))

def remove(did):
    store.execute('UPDATE distributions SET enabled=0,deleted_at=?,updated=? WHERE id=? AND deleted_at IS NULL',(store.now(),store.now(),did))

def allowed_projects(distribution):
    selected=set(distribution['project_ids']);tags={t['id']:t['name'].casefold() for t in tag_registry.all_tags()}
    names={tags[t] for t in distribution['tag_ids'] if t in tags};existing=set()
    for p in store.rows('SELECT id,tags FROM projects'):
        existing.add(p['id'])
        try:matches={str(t).casefold() for t in json.loads(p['tags'])}&names
        except (ValueError,TypeError):matches=set()
        if matches:selected.add(p['id'])
    return selected&existing

def grant_current(report):
    if report.get('audience','admin')!='distribution':return True
    d=get(report['distribution_id'])
    if not d or not d['enabled'] or d['deleted_at'] or report['recipient_email'].casefold() not in {x.casefold() for x in d['emails']}:return False
    try:snapshot=json.loads(report['selection_snapshot'])
    except (ValueError,TypeError):return False
    if snapshot.get('selected_project_ids')!=d['project_ids'] or snapshot.get('selected_tag_ids')!=d['tag_ids']:return False
    return set(snapshot.get('included_project_ids',[]))<=allowed_projects(d)
