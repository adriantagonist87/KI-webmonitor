"""Versioned, validated project exports. No executable files or browser profiles."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import store
import configuration
import communication_config
import scheduling
import tag_registry
import site_access
import project_icons
import collection_index
import retention
import retry_jobs
import site_safety
import browser_mode
import distributions
from functools import wraps
from project_meta import normalize_color, normalize_tags

FORMAT = 'ki-webmonitor'
VERSION = 8
SUPPORTED_VERSIONS = (1, 2, 3, 4, 5, 6, 7, VERSION)
MAX_UPLOAD = 512 * 1024**2
MAX_JSON = 64 * 1024**2
MAX_EXPANDED = 2 * 1024**3
MAX_FILES = 50000
LEGACY_PROJECT_KEYS = ('id','name','url','prompt','domains','cron','enabled','created','color','tags')
SCHEDULE_PROJECT_KEYS = (*LEGACY_PROJECT_KEYS, 'use_global_schedule', 'schedule_config')
PROJECT_KEYS = (*SCHEDULE_PROJECT_KEYS, 'telegram_enabled')
LEGACY_TABLES = ('runs','snapshots','changes','events','reports','report_changes','change_reviews')
PRE_DISTRIBUTION_TABLES = (*LEGACY_TABLES, 'run_retries')
TABLES = (*PRE_DISTRIBUTION_TABLES, 'report_mail_events')
SETTING_KEYS = {'omlx_url','agent_model','analysis_model','vision_model','smtp_host','smtp_port','smtp_user','smtp_from','smtp_to','smtp_tls','smtp_enabled','global_schedule','retention'}
SETTING_KEYS |= set(communication_config.FIELDS)
SECRET_KEYS = {'omlx_key','smtp_password','telegram_token'}
SITE_AUTH_PROVIDER = 'elektro_subscription'
SITE_AUTH_ORIGIN = 'https://www.elektro.net'
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')

class Invalid(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise Invalid(message)

def identifier(value):
    require(isinstance(value,str) and bool(IDENTIFIER.fullmatch(value)), 'Ungültige Datensatz-ID.')
    return value

def relative_file(value):
    require(isinstance(value,str) and 0 < len(value) <= 500 and '\\' not in value and ':' not in value and '\x00' not in value, 'Ungültiger Archivpfad.')
    p = PurePosixPath(value)
    require(not p.is_absolute() and value == p.as_posix() and all(x not in ('.','..') for x in p.parts) and len(p.parts)>1 and p.parts[0] in ('artifacts','reports'), 'Archivpfad außerhalb der erlaubten Datenbereiche.')
    return value

def safe_target(relative):
    relative_file(relative)
    target = store.DATA / relative
    require(not any(p.is_symlink() for p in (target,*target.parents) if p != store.DATA.parent), 'Symbolische Links sind im Importziel nicht erlaubt.')
    require(target.resolve().is_relative_to(store.DATA.resolve()), 'Ungültiges Importziel.')
    return target

def file_hash(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def dump(value):
    return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')

def record_key(table, record):
    return 'source_run_id' if table == 'run_retries' else 'id' if 'id' in record else 'change_id'

def retry_timestamp(value):
    require(isinstance(value,str) and 0 < len(value) <= 96 and not any(ord(char)<32 for char in value),
            'Ungültiger Wiederholungszeitpunkt.')
    try:
        stamp = datetime.fromisoformat(value)
        require(stamp.tzinfo is not None and stamp.utcoffset() is not None, 'Wiederholungszeitpunkt benötigt eine Zeitzone.')
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise Invalid('Ungültiger Wiederholungszeitpunkt.') from None

def validate_retries(records, runs, projects):
    """Portable lineage is archival data; imported plans are never replayed."""
    seen=set(); children=set()
    for row in records:
        source=identifier(row['source_run_id']);identifier(row['project_id'])
        require(source not in seen, 'Doppelte Wiederholung.');seen.add(source)
        parent=runs.get(source)
        require(parent is not None and parent['project_id']==row['project_id'] and row['project_id'] in projects
                and parent['status'] in ('failed','partial') and isinstance(parent['origin'],str) and parent['origin']!='retry',
                'Ungültiger Ursprung einer Wiederholung.')
        require(row['state'] in ('pending','queued','running','finished','cancelled'), 'Ungültiger Wiederholungsstatus.')
        try:earliest=retry_timestamp(parent['finished']) + timedelta(minutes=30)
        except OverflowError:raise Invalid('Ungültiger Wiederholungszeitpunkt.') from None
        require(retry_timestamp(row['due_at']) >= earliest,
                'Eine Wiederholung darf nicht vor Ablauf von 30 Minuten geplant sein.')
        child=row['retry_run_id']
        if child is not None:
            identifier(child)
            require(child!=source and child not in children, 'Ungültige oder doppelte Wiederholungszuordnung.');children.add(child)
            attempt=runs.get(child)
            require(attempt is not None or row['state'] in ('finished','cancelled'), 'Die geplante Wiederholungsprüfung fehlt.')
            if attempt:
                require(attempt['project_id']==row['project_id'] and attempt['origin']=='retry', 'Wiederholungsprüfung gehört zu einem anderen Auftrag.')
        require((child is None) if row['state']=='pending' else (child is not None) if row['state'] in ('queued','running','finished') else True,
                'Wiederholungsstatus und Prüfungszuordnung widersprechen sich.')

def validate_site_auth(payload):
    """Only public, origin-bound access metadata is portable. Never read a vault."""
    if payload['version']==1:
        require('site_auth' not in payload,'Formatversion 1 darf keine Website-Zugänge enthalten.')
        return
    profiles=payload.setdefault('site_auth',{})
    projects={p['id']:p for p in payload['projects']}
    require(isinstance(profiles,dict) and len(profiles)<=len(projects),'Ungültige Website-Zugangskonfiguration.')
    for pid,profile in profiles.items():
        identifier(pid)
        require(pid in projects,'Website-Zugang gehört zu keinem enthaltenen Projekt.')
        try: profiles[pid]=site_access.validate_public_config(projects[pid]['url'], profile)
        except (site_access.AccessError, ValueError, TypeError): raise Invalid('Ungültige öffentliche, projektgebundene Website-Zugangskonfiguration.') from None
        if payload['version']==2:
            require(profile.get('provider')==SITE_AUTH_PROVIDER,'Allgemeine Website-Zugänge benötigen Exportversion 3.')

def storage_guard(exclusive):
    """A complete transfer sees one consistent set of retained archive files."""
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            with retention.maintenance_lock(exclusive=exclusive):
                return function(*args, **kwargs)
        return guarded
    return decorate


@storage_guard(exclusive=False)
def export_to(destination, history=False, include_secrets=False):
    with store.db() as c:
        c.execute('BEGIN')
        tag_registry.initialize(c)
        projects = [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY name')]
        site_auth={}
        for project in projects:
            saved=c.execute('SELECT value FROM state WHERE key=?',('site_auth:'+project['id'],)).fetchone()
            if saved:
                try:site_auth[project['id']]=json.loads(saved['value'])
                except (ValueError,TypeError):raise Invalid('Gespeicherte Website-Zugangskonfiguration ist ungültig.')
        payload = {'format':FORMAT,'version':VERSION,'created_at':store.now(),'kind':'archive' if history else 'configuration',
                   'projects':[{k:r[k] for k in PROJECT_KEYS} for r in projects], 'settings':store.persisted_settings(),
                   'credentials':{k:v for k,v in store.persisted_credentials().items() if k in SECRET_KEYS} if include_secrets else None,
                   'tag_registry':[{k:r[k] for k in ('id','name','key','color')} for r in c.execute('SELECT * FROM tags ORDER BY key')],
                   'project_icons':{p['id']:project_icons.get(p['id']) for p in projects if project_icons.get(p['id'])},
                   'site_auth':site_auth,
                   'distributions':[dict(r) for r in c.execute('SELECT * FROM distributions'+('' if history else ' WHERE deleted_at IS NULL'))],
                   'crawl_profiles':{p['id']:site_safety.profile(p['id'],c) for p in projects},
                   'browser_modes':{p['id']:browser_mode.get(p,c) for p in projects},
                   'collection_indexes':[dict(r) for r in c.execute("SELECT * FROM state WHERE key LIKE 'collection_index:%'")] if history else [],
                   'history':{t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in TABLES} if history else None,
                   'content_scopes':[dict(r) for r in c.execute("SELECT * FROM state WHERE key LIKE 'content_scopes:%'")] if history else [],'files':{}}
        validate_site_auth(payload)
    if not history:
        destination.write_bytes(dump(payload))
        os.chmod(destination,0o600)
        return payload
    entries = {}
    # Include all run evidence and presentation revisions, not just the current snapshot.
    for row in payload['history']['runs']:
        folder = store.DATA/'artifacts'/identifier(row['id'])
        if folder.exists():
            for path in folder.rglob('*'):
                require(not path.is_symlink(), 'Archiv enthält einen symbolischen Link.')
                if path.is_file():entries[path.relative_to(store.DATA).as_posix()] = path
    for snap in payload['history']['snapshots']:
        for key in ('file','screenshot','html'):
            if snap.get(key):
                path = safe_target(snap[key]);require(path.is_file(),'Ein archivierter Beleg fehlt: '+snap[key])
                entries[snap[key]] = path
    report_dir=store.DATA/'reports'
    if report_dir.exists():
        for path in report_dir.rglob('*'):
            require(not path.is_symlink(),'Berichtsarchiv enthält einen symbolischen Link.')
            if path.is_file():entries[path.relative_to(store.DATA).as_posix()]=path
    require(len(entries)<=MAX_FILES,'Zu viele Archivdateien; administratives Backup verwenden.')
    require(sum(p.stat().st_size for p in entries.values())<=MAX_EXPANDED,'Archiv zu groß; administratives Backup verwenden.')
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
        for relative,path in sorted(entries.items()):
            relative_file(relative)
            payload['files'][relative]={'size':path.stat().st_size,'sha256':file_hash(path)}
            z.write(path,'files/'+relative)
        raw=dump(payload);require(len(raw)<=MAX_JSON,'JSON-Verlauf zu groß für den portablen Export.')
        z.writestr('webmonitor.json',raw)
    os.chmod(destination,0o600)
    require(destination.stat().st_size<=MAX_UPLOAD,'Export übersteigt das Importlimit; administratives Backup verwenden.')
    return payload

def validate_settings(value):
    require(isinstance(value,dict) and not set(value)-SETTING_KEYS,'Unbekannte oder ungültige Einstellungen.')
    for key,val in value.items():
        if key=='retention':
            try:retention.validate_policy(val)
            except (ValueError,TypeError):raise Invalid('Ungültige Speicheraufbewahrung.') from None
        elif key=='global_schedule':
            try:scheduling.validate(val)
            except ValueError:raise Invalid('Ungültiger globaler Prüfzeitplan.') from None
        elif key=='smtp_enabled':require(type(val) is bool,'Ungültiger Versandstatus.')
        elif key=='smtp_port':require(type(val) is int and 1<=val<=65535,'Ungültiger SMTP-Port.')
        else:require(isinstance(val,str) and len(val)<=2000 and '\x00' not in val and '\r' not in val and '\n' not in val,'Ungültiger Einstellungswert.')
    url=value.get('omlx_url','')
    if url:
        u=urlsplit(url)
        require(u.scheme in ('http','https') and u.hostname and not u.username and not u.password and not u.query and not u.fragment,'Ungültige oMLX-Adresse.')
    require(value.get('smtp_tls','starttls') in ('ssl','starttls'),'Ungültige SMTP-Verschlüsselung.')
    try:
        configuration.resolve_settings(value,environ={})
        communication_config.resolve(value,communication_config.FIELDS,environ={})
    except configuration.ConfigurationError as error:raise Invalid(str(error)) from None

def validate_distributions(payload):
    records=payload.get('distributions',[])
    require(isinstance(records,list) and len(records)<=1000,'Ungültige Verteilerliste.')
    require(payload['version']>=8 or not records,'Verteiler benötigen Exportversion 8.')
    seen=set();aliases=set()
    expected={'id','alias','alias_key','emails','project_ids','tag_ids','enabled','created','updated','deleted_at'}
    for r in records:
        require(isinstance(r,dict) and set(r)==expected,'Ungültiger Verteilerdatensatz.')
        identifier(r['id']);require(r['id'] not in seen,'Doppelte Verteiler-ID.');seen.add(r['id'])
        try:
            decoded=distributions.decode(r);clean=distributions.validate_record(decoded,check_targets=False)
            require(clean['alias']==r['alias'] and clean['alias_key']==r['alias_key'],'Ungültiger Verteileralias.')
            require(decoded['emails']==clean['emails'] and decoded['project_ids']==clean['project_ids'] and decoded['tag_ids']==clean['tag_ids'],'Ungültige Verteilerauswahl.')
        except (ValueError,TypeError,KeyError):raise Invalid('Ungültige Verteilerkonfiguration.') from None
        require(all(isinstance(r[k],str) and len(r[k])<=96 for k in ('created','updated')) and (r['deleted_at'] is None or isinstance(r['deleted_at'],str) and len(r['deleted_at'])<=96),'Ungültiger Verteilerzeitpunkt.')
        if r['deleted_at'] is None:require(r['alias_key'] not in aliases,'Doppelter Verteileralias.');aliases.add(r['alias_key'])

def validate(payload):
    require(isinstance(payload,dict) and payload.get('format')==FORMAT and type(payload.get('version')) is int and payload['version'] in SUPPORTED_VERSIONS,'Unbekanntes Exportformat oder nicht unterstützte Version.')
    require(payload.get('kind') in ('configuration','archive'),'Ungültige Exportart.')
    validate_distributions(payload)
    projects=payload.get('projects')
    require(isinstance(projects,list) and len(projects)<=10000,'Ungültige Projektliste.')
    seen=set()
    for p in projects:
        expected=PROJECT_KEYS if payload['version']>=7 else SCHEDULE_PROJECT_KEYS if payload['version']>=3 else LEGACY_PROJECT_KEYS
        require(isinstance(p,dict) and set(p)==set(expected),'Ungültige Projektfelder.')
        if payload['version']>=7:require(type(p['telegram_enabled']) is int and p['telegram_enabled'] in (0,1),'Ungültige Telegram-Projektvorgabe.')
        pid=identifier(p['id']);require(pid not in seen,'Doppelte Projekt-ID.');seen.add(pid)
        require(isinstance(p['name'],str) and 0<len(p['name'])<=500 and isinstance(p['prompt'],str) and 0<len(p['prompt'])<=40000,'Ungültiger Alias oder Prompt.')
        require(isinstance(p['url'],str) and len(p['url'])<=8000,'Ungültige Projekt-URL.')
        u=urlsplit(p['url']);require(u.scheme in ('http','https') and u.hostname and not u.username and not u.password,'Ungültige Projekt-URL oder Zugangsdaten in URL.')
        require(isinstance(p['domains'],str) and len(p['domains'])<=8000,'Ungültige Domainliste.')
        domains=p['domains'].split(',');domains=[d.strip().lower() for d in domains]
        require(u.hostname in domains and all(d and not any(x in d for x in ('/',':','*','\\',' ')) for d in domains),'Ungültige erlaubte Domains.')
        p['domains']=','.join(domains)
        require(type(p['enabled']) is int and p['enabled'] in (0,1),'Ungültiger Aktivierungsstatus.')
        require(isinstance(p['cron'],str) and len(p['cron'].split())==5 and len(p['cron'])<100,'Ungültiger Zeitplan.')
        try:store.next_due(p['cron']);p['color']=normalize_color(p['color']);p['tags']=json.dumps(normalize_tags(json.loads(p['tags'])),ensure_ascii=False)
        except (ValueError,TypeError,KeyError):raise Invalid('Ungültiger Zeitplan, Farbe oder Tags.')
        require(isinstance(p['created'],str) and len(p['created'])<=80,'Ungültiger Erstellungszeitpunkt.')
        if payload['version']>=3:
            require(type(p['use_global_schedule']) is int and p['use_global_schedule'] in (0,1),'Ungültige globale Zeitplanzuordnung.')
            try:
                require(isinstance(p['schedule_config'],str) and len(p['schedule_config'])<=1000,'Ungültiger Prüfzeitplan.')
                schedule=json.loads(p['schedule_config'])
                require(isinstance(schedule,dict),'Ungültiger Prüfzeitplan.')
                if schedule:scheduling.validate(schedule)
            except (ValueError,TypeError):raise Invalid('Ungültiger Prüfzeitplan.') from None
    profiles=payload.get('crawl_profiles',{})
    modes=payload.get('browser_modes',{})
    require(isinstance(modes,dict) and set(modes)<=seen,'Ungültige Browserbedienung.')
    for p in projects:
        if p['id'] in modes:
            try:browser_mode.validate(modes[p['id']],p)
            except (ValueError,TypeError,KeyError):raise Invalid('Ungültige Browserbedienung oder Startadresse.') from None
    require(isinstance(profiles,dict) and set(profiles)<=seen and
            all(isinstance(value,str) and value in site_safety.PROFILES for value in profiles.values()),'Ungültiges Prüftempo.')
    if payload['version']>=3:
        tags=payload.get('tag_registry');require(isinstance(tags,list) and len(tags)<=10000,'Ungültige Tagverwaltung.')
        ids=set();keys=set()
        for tag in tags:
            require(isinstance(tag,dict) and set(tag)=={'id','name','key','color'},'Ungültige Tagfelder.')
            identifier(tag['id']);require(tag['id'] not in ids,'Doppelte Tag-ID.');ids.add(tag['id'])
            try:normalized=normalize_tags([tag['name']]);normalize_color(tag['color'])
            except (ValueError,TypeError):raise Invalid('Ungültiger Tagname oder Tagfarbe.') from None
            require(len(normalized)==1 and normalized[0]==tag['name'] and tag['key']==tag['name'].casefold() and tag['key'] not in keys,'Ungültiger oder doppelter Tagname.');keys.add(tag['key'])
        require(all(n.casefold() in keys for p in projects for n in json.loads(p['tags'])),'Ein Projekt verweist auf einen fehlenden globalen Tag.')
    else:require('tag_registry' not in payload,'Globale Tags benötigen Exportversion 3.')
    icons=payload.get('project_icons',{})
    require(isinstance(icons,dict) and len(icons)<=len(projects),'Ungültige Projekticons.')
    for pid,icon in icons.items():
        require(pid in seen,'Icon gehört zu keinem enthaltenen Projekt.')
        try:project_icons.validate(icon)
        except (ValueError,TypeError):raise Invalid('Ungültiges oder unsicheres Projekticon.') from None
    require(payload['version']>=4 or not icons,'Projekticons benötigen Exportversion 4.')
    validate_site_auth(payload)
    validate_settings(payload.get('settings'))
    indexes=payload.get('collection_indexes',[])
    require(isinstance(indexes,list) and len(indexes)<=10000,'Ungültige Sammlungsindizes.')
    require(payload['version']>=5 or not indexes,'Sammlungsindizes benötigen Exportversion 5.')
    for item in indexes:
        require(isinstance(item,dict) and set(item)=={'key','value'} and isinstance(item['key'],str) and isinstance(item['value'],str),'Ungültiger Sammlungsindex.')
        parts=item['key'].split(':')
        require(len(parts)==3 and parts[0]=='collection_index' and parts[1] in seen and re.fullmatch('[a-f0-9]{64}',parts[2]),'Ungültige Sammlungszuordnung.')
        try:collection_index.plan_update(json.loads(item['value']),[],False)
        except (ValueError,TypeError):raise Invalid('Ungültiger Sammlungsinhalt.') from None
    creds=payload.get('credentials')
    require(creds is None or isinstance(creds,dict) and not set(creds)-SECRET_KEYS and all(isinstance(v,str) and len(v)<=8000 and '\x00' not in v for v in creds.values()),'Ungültige Zugangsdatenfelder.')
    if creds is not None:
        try:
            configuration.resolve_credentials(creds,environ={})
            communication_config.resolve(creds,communication_config.SECRETS,environ={})
        except configuration.ConfigurationError as error:raise Invalid(str(error)) from None
    manifest=payload.get('files')
    require(isinstance(manifest,dict) and len(manifest)<=MAX_FILES,'Ungültiges Dateiverzeichnis.')
    for relative,entry in manifest.items():
        relative_file(relative)
        require(isinstance(entry,dict) and set(entry)=={'size','sha256'} and type(entry['size']) is int and 0<=entry['size']<=MAX_EXPANDED and isinstance(entry['sha256'],str) and re.fullmatch('[a-f0-9]{64}',entry['sha256']),'Ungültige Dateiprüfsumme oder Größe.')
    require(sum(e['size'] for e in manifest.values())<=MAX_EXPANDED,'Entpacktes Archiv zu groß.')
    h=payload.get('history')
    require(isinstance(payload.get('content_scopes'),list),'Ungültige Inhaltsbereiche.')
    if payload['kind']=='configuration':
        require(h is None and not manifest and not payload['content_scopes'] and not indexes,'Ein Konfigurationsexport darf keinen Verlauf enthalten.')
        return
    expected=TABLES if payload['version']>=8 else PRE_DISTRIBUTION_TABLES if payload['version']>=6 else LEGACY_TABLES
    effective=set(h)-{'report_mail_events'} if isinstance(h,dict) and payload['version']<8 and not h.get('report_mail_events') else set(h) if isinstance(h,dict) else set()
    require(isinstance(h,dict) and effective==set(expected),'Ungültige Verlaufstabellen.')
    if payload['version']<8:
        defaults={'audience':'admin','parent_id':None,'distribution_id':None,'distribution_alias':None,'recipient_email':None,'selection_snapshot':'{}','sent_at':None,'attempts':0,'next_attempt':None}
        for r in h['reports']:
            require(isinstance(r,dict),'Ungültiger Bericht.')
            for key,value in defaults.items():
                require(key not in r or r[key]==value,'Verteilerdaten benötigen Exportversion 8.')
                r.setdefault(key,value)

    with store.db() as c:
        for table,records in h.items():
            columns={r[1]:r[2] for r in c.execute('PRAGMA table_info('+table+')')}
            require(isinstance(records,list) and len(records)<=250000,'Zu viele Verlaufsdatensätze.')
            keys=set()
            for r in records:
                require(isinstance(r,dict) and set(r)==set(columns),'Ungültige Felder in '+table+'.')
                for key,val in r.items():
                    require(val is None or (type(val) is int if columns[key]=='INTEGER' else isinstance(val,str)),'Ungültiger Datentyp in '+table+'.')
                if ('id' in r and table not in ('events','report_mail_events')) or table=='run_retries':
                    value=r[record_key(table,r)]
                    identifier(value);require(value not in keys,'Doppelte Datensatz-ID.');keys.add(value)
    runs={r['id']:r for r in h['runs']};snaps={s['id']:s for s in h['snapshots']};changes={r['id']:r for r in h['changes']};reports={r['id']:r for r in h['reports']}
    for r in runs.values():
        require(r['project_id'] in seen and r['status'] in ('success','partial','failed','cancelled') and r['finished'],'Nur abgeschlossene Prüfungen können importiert werden.')
        if r['status']=='cancelled':
            require(r['origin']=='retry' and not any(s['run_id']==r['id'] for s in snaps.values())
                    and not any(change['run_id']==r['id'] for change in changes.values()),
                    'Nur nicht gestartete Wiederholungsprüfungen dürfen verworfen sein.')
    validate_retries(h.get('run_retries',[]),runs,seen)
    for s in snaps.values():
        require(s['project_id'] in seen and s['run_id'] in runs and runs[s['run_id']]['project_id']==s['project_id'],'Ungültige Quellenzuordnung.')
        try:require(isinstance(json.loads(s['links']),list),'Ungültige Linkliste.')
        except (ValueError,TypeError):raise Invalid('Ungültige Linkliste.')
        for key in ('file','screenshot','html'):
            if s.get(key):require(s[key] in manifest,'Ein referenzierter Quellenbeleg fehlt im Archiv.')
        require(hashlib.sha256(s['text'].encode()).hexdigest()==s['text_sha256'],'Quellentext-Prüfsumme stimmt nicht.')
        if s['file']:require(manifest[s['file']]['sha256']==s['sha256'],'Dokument-Prüfsumme stimmt nicht.')
    for r in changes.values():
        require(r['run_id'] in runs and runs[r['run_id']]['project_id']==r['project_id'] and r['current_id'] in snaps and snaps[r['current_id']]['project_id']==r['project_id'] and (r['previous_id'] is None or r['previous_id'] in snaps and snaps[r['previous_id']]['project_id']==r['project_id']),'Ungültige Änderungszuordnung.')
        try:require(isinstance(json.loads(r['evidence']),dict),'Ungültiger Änderungsbeleg.')
        except (ValueError,TypeError):raise Invalid('Ungültiger Änderungsbeleg.')
    for r in reports.values():
        require(r['audience'] in ('admin','distribution') and type(r['attempts']) is int and 0<=r['attempts']<=10000,'Ungültige Berichtsklasse.')
        if r['audience']=='distribution':
            identifier(r['distribution_id'])
            require(r['parent_id'] in reports and reports[r['parent_id']]['audience']=='admin' and r['parent_id']!=r['id'],'Ungültiger globaler Ursprungsbericht.')
            require(r['distribution_id'] in {d['id'] for d in payload.get('distributions',[])},'Verteiler des historischen Berichts fehlt.')
            try:distributions.email(r['recipient_email'])
            except (ValueError,TypeError):raise Invalid('Ungültiger Berichtsempfänger.') from None
            require(isinstance(r['distribution_alias'],str) and 0<len(r['distribution_alias'])<=100 and not any(ord(x)<32 for x in r['distribution_alias']),'Ungültiger Verteileralias im Bericht.')
            try:
                snapshot=json.loads(r['selection_snapshot']);require(isinstance(snapshot,dict),'Ungültige Berichtsauswahl.')
                for key in ('selected_project_ids','selected_tag_ids','included_project_ids','withheld_project_ids'):distributions.ids(snapshot[key])
                for key in ('selected_tags','selected_projects'):require(isinstance(snapshot.get(key,[]),list) and all(isinstance(x,str) and len(x)<=500 for x in snapshot.get(key,[])),'Ungültige Auswahlbezeichnung.')
            except (ValueError,TypeError,KeyError):raise Invalid('Ungültige gespeicherte Berichtsauswahl.') from None
        else:require(r['parent_id'] is None and r['recipient_email'] is None,'Ungültiger Adminbericht.')
    for r in h.get('report_mail_events',[]):
        require(r['report_id'] in reports and isinstance(r['created'],str) and len(r['created'])<=96 and isinstance(r['note'],str) and len(r['note'])<=2000,'Ungültiger Versandprotokolleintrag.')
    for r in h['events']:require(r['run_id'] in runs,'Ungültige Aktionszuordnung.')
    for r in h['report_changes']:require(r['change_id'] in changes and r['report_id'] in reports,'Ungültige Berichtszuordnung.')
    for r in h['change_reviews']:require(r['change_id'] in changes,'Ungültige Änderungsbestätigung.')
    for r in payload['content_scopes']:
        require(isinstance(r,dict) and set(r)=={'key','value'} and r['key'].startswith('content_scopes:') and r['key'].split(':',1)[1] in seen,'Ungültiger gespeicherter Inhaltsbereich.')
        try:require(isinstance(json.loads(r['value']),dict),'Ungültiger Inhaltsbereich.')
        except (ValueError,TypeError):raise Invalid('Ungültiger Inhaltsbereich.')

def read_package(path):
    require(path.stat().st_size<=MAX_UPLOAD,'Datei übersteigt 512 MB.')
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as z:
                entries=z.infolist();names=[e.filename for e in entries]
                require(len(entries)<=MAX_FILES+1 and len(names)==len(set(names)),'Doppelte oder zu viele ZIP-Einträge.')
                require('webmonitor.json' in names and z.getinfo('webmonitor.json').file_size<=MAX_JSON,'Manifest fehlt oder ist zu groß.')
                require(sum(e.file_size for e in entries)<=MAX_EXPANDED+MAX_JSON,'Entpacktes Archiv übersteigt 2 GB.')
                for e in entries:require(not e.flag_bits&1 and not stat.S_ISLNK(e.external_attr>>16) and not e.is_dir(),'Verschlüsselte Archive, Links oder Verzeichniseinträge sind nicht erlaubt.')
                payload=json.loads(z.read('webmonitor.json'));validate(payload)
                require(payload['kind']=='archive' and set(names)=={'webmonitor.json'}|{'files/'+r for r in payload['files']},'ZIP-Inhalt stimmt nicht mit dem Dateiverzeichnis überein.')
                for rel,entry in payload['files'].items():
                    info=z.getinfo('files/'+rel);require(info.file_size==entry['size'],'Dateigröße stimmt nicht.')
                    with z.open(info) as f:require(hashlib.file_digest(f,'sha256').hexdigest()==entry['sha256'],'Dateiprüfsumme stimmt nicht: '+rel)
                return payload
        except (zipfile.BadZipFile,RuntimeError,UnicodeError,json.JSONDecodeError):raise Invalid('ZIP-Datei ist beschädigt oder unlesbar.')
    require(path.stat().st_size<=MAX_JSON,'JSON-Datei übersteigt 64 MB.')
    try:payload=json.loads(path.read_bytes());validate(payload)
    except (UnicodeError,json.JSONDecodeError):raise Invalid('Keine gültige JSON-Datei.')
    require(payload['kind']=='configuration','Verlauf muss als ZIP einschließlich Belegen importiert werden.')
    return payload

def preview(payload):
    existing={r['id'] for r in store.rows('SELECT id FROM projects')}
    h=payload.get('history') or {}
    affected={p['id'] for p in payload['projects']}
    cancelled={r['source_run_id'] for r in h.get('run_retries',[]) if r['state'] in ('pending','queued','running')}
    cancelled.update(r['source_run_id'] for r in store.rows("SELECT source_run_id,project_id FROM run_retries WHERE state IN ('pending','queued')") if r['project_id'] in affected)
    return {'kind':payload['kind'],'projects':[{'id':p['id'],'name':p['name'],'existing':p['id'] in existing,'enabled':bool(p['enabled'])} for p in payload['projects']],
            'new':sum(p['id'] not in existing for p in payload['projects']),'updated':sum(p['id'] in existing for p in payload['projects']),
            'counts':{t:len(h.get(t,[])) for t in ('runs','snapshots','changes','reports')},'files':len(payload['files']),
            'settings':len(payload['settings']),'credentials':bool(payload.get('credentials')),'site_auth':len(payload.get('site_auth',{})), 'tags':len(payload.get('tag_registry',[])), 'distributions':len(payload.get('distributions',[])),
            'retry_plans_cancelled':len(cancelled)}

def preflight(c,payload):
    for tag in payload.get('tag_registry',[]):
        old=c.execute('SELECT key FROM tags WHERE id=?',(tag['id'],)).fetchone()
        require(not old or old['key']==tag['key'],'Tag-ID gehört zu einem anderen Tag.')
    h=payload.get('history') or {}
    for table in TABLES:
        for r in h.get(table,[]):
            if table in ('events','report_mail_events'):continue
            key=record_key(table,r)
            old=c.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(r[key],)).fetchone()
            if old:
                # Existing evidence is never silently replaced by another version.
                if table in ('snapshots','changes'):require(dict(old)==r,'Abweichender bestehender Beleg: '+table+'/'+r[key])
                elif table=='runs':require(old['project_id']==r['project_id'],'Prüfungs-ID gehört zu einem anderen Projekt.')
                elif table=='run_retries':
                    require(old['project_id']==r['project_id'] and old['due_at']==r['due_at']
                            and (old['retry_run_id'] is None or r['retry_run_id'] is None or old['retry_run_id']==r['retry_run_id']),
                            'Abweichende bestehende Wiederholungszuordnung.')
            elif table=='changes':
                require(not c.execute('SELECT 1 FROM changes WHERE project_id=? AND fingerprint=?',(r['project_id'],r['fingerprint'])).fetchone(),'Änderungs-Fingerprint bereits unter anderer ID vorhanden.')
            elif table=='reports':
                require(not c.execute('SELECT 1 FROM reports WHERE period_key=?',(r['period_key'],)).fetchone(),'Berichtszeitraum bereits unter anderer ID vorhanden.')
    for d in payload.get('distributions',[]):
        clash=c.execute('SELECT id FROM distributions WHERE alias_key=? AND deleted_at IS NULL AND id<>?',(d['alias_key'],d['id'])).fetchone()
        require(d['deleted_at'] is not None or clash is None,'Verteileralias bereits unter anderer ID vorhanden.')
    for relative,entry in payload['files'].items():
        target=safe_target(relative)
        require(not target.exists() or target.is_file() and file_hash(target)==entry['sha256'],'Abweichende vorhandene Archivdatei: '+relative)

@storage_guard(exclusive=True)
def import_package(path,payload,apply_settings=True,apply_secrets=False,activate=False):
    """One DB transaction; files/settings roll back on failure, recovery copy retained."""
    validate(payload)
    require(not store.one("SELECT id FROM runs WHERE status IN ('running','queued')"),'Erst laufende oder wartende Prüfungen beenden lassen.')
    required=sum(e['size'] for e in payload['files'].values())
    require(shutil.disk_usage(store.DATA).free>required+128*1024**2,'Nicht genug freier Speicher für Import und Sicherung.')
    recovery=store.DATA/'import-backups'/store.uid();recovery.mkdir(parents=True,mode=0o700)
    old_settings=store.CONFIG.read_bytes() if store.CONFIG.exists() else None
    old_secrets=store.SECRETS.read_bytes() if store.SECRETS.exists() else None
    created=[];config_written=False;secrets_written=False
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            preflight(c,payload)
            # Backup from a separate reader; backing up this write transaction would hang.
            with sqlite3.connect(store.DATA/'monitor.db') as source, sqlite3.connect(recovery/'monitor.db') as backup:source.backup(backup)
            if old_settings is not None:(recovery/'settings.json').write_bytes(old_settings)
            if old_secrets is not None:(recovery/'credentials.json').write_bytes(old_secrets)
            for tag in payload.get('tag_registry',[]):
                old=c.execute('SELECT id FROM tags WHERE key=?',(tag['key'],)).fetchone()
                if old:c.execute('UPDATE tags SET name=?,color=? WHERE id=?',(tag['name'],tag['color'],old['id']))
                else:c.execute('INSERT INTO tags(id,name,key,color) VALUES(?,?,?,?)',(tag['id'],tag['name'],tag['key'],tag['color']))
            for p in recovery.iterdir():os.chmod(p,0o600)
            if payload['files']:
                with zipfile.ZipFile(path) as z:
                    for relative in payload['files']:
                        target=safe_target(relative)
                        if target.exists():continue
                        target.parent.mkdir(parents=True,exist_ok=True)
                        created.append(target)
                        with z.open('files/'+relative) as source,target.open('xb') as out:shutil.copyfileobj(source,out,1024**2)
                        os.chmod(target,0o600)
                        require(file_hash(target)==payload['files'][relative]['sha256'],'Datei während Import verändert.')
            for p in payload['projects']:
                data=dict(p);data['enabled']=data['enabled'] if activate else 0
                if payload['version']<3:data.update(use_global_schedule=0,schedule_config='{}')
                if payload['version']<7:data['telegram_enabled']=0
                old=c.execute('SELECT * FROM projects WHERE id=?',(p['id'],)).fetchone()
                # Replacing a project definition supersedes an old pending
                # task, even when the imported project is activated at once.
                if old:retry_jobs.cancel_project(p['id'],connection=c)
                incoming_global=payload['settings'].get('global_schedule',scheduling.global_schedule()) if apply_settings else scheduling.global_schedule()
                data['next_due']=scheduling.next_for(incoming_global) if data['use_global_schedule'] else scheduling.next_project_due(data)
                if old:
                    keys=[k for k in data if k not in ('id','created')]
                    c.execute('UPDATE projects SET '+','.join(k+'=?' for k in keys)+' WHERE id=?',[data[k] for k in keys]+[p['id']])
                else:
                    keys=list(data);c.execute('INSERT INTO projects('+','.join(keys)+') VALUES('+','.join('?' for k in keys)+')',[data[k] for k in keys])
            for pid,profile in payload.get('crawl_profiles',{}).items():
                site_safety.save_profile(pid,profile,c)
            for p in payload['projects']:
                if p['id'] in payload.get('browser_modes',{}):
                    browser_mode.save(p,payload['browser_modes'][p['id']],c)
            tag_registry.initialize(c)
            if payload['version']>=2:
                # Version 2 is the complete public access configuration for the
                # imported projects. Legacy files leave existing bindings intact.
                # Website secrets are neither read nor changed by this import.
                for p in payload['projects']:
                    key='site_auth:'+p['id']
                    profile=payload['site_auth'].get(p['id'])
                    if profile is None:
                        c.execute('DELETE FROM state WHERE key=?',(key,))
                    else:
                        value={**profile,'enabled':profile['enabled'] if activate else False}
                        c.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,ensure_ascii=False)))
            if payload['version']>=4:
                for p in payload['projects']:
                    icon=payload.get('project_icons',{}).get(p['id'])
                    if icon:project_icons.save(p['id'],icon,connection=c)
                    else:c.execute('DELETE FROM state WHERE key=?',(project_icons.KEY+p['id'],))
            for d in payload.get('distributions',[]):
                item=dict(d);item['enabled']=item['enabled'] if activate and not item['deleted_at'] else 0
                keys=list(item)
                c.execute('INSERT INTO distributions('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+') ON CONFLICT(id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in keys if k!='id'),[item[k] for k in keys])
            added={}
            for table in TABLES:
                added[table]=0
                for r in (payload.get('history') or {}).get(table,[]):
                    item=dict(r)
                    if table=='report_mail_events':
                        item.pop('id')
                        if c.execute('SELECT 1 FROM report_mail_events WHERE report_id=? AND created=? AND status=? AND note=?',(r['report_id'],r['created'],r['status'],r['note'])).fetchone():continue
                    elif table=='events':
                        item.pop('id')
                        if c.execute('SELECT 1 FROM events WHERE run_id=? AND time=? AND message=?',(r['run_id'],r['time'],r['message'])).fetchone():continue
                    else:
                        key=record_key(table,item)
                        old=c.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(item[key],)).fetchone()
                        if old:
                            # Keep an already consumed attempt consumed. A
                            # merged archive may add its historical child ID,
                            # but can never resurrect the pending timer.
                            if table=='run_retries' and old['retry_run_id'] is None and item['retry_run_id'] is not None:
                                c.execute("UPDATE run_retries SET retry_run_id=?,state='cancelled' WHERE source_run_id=?",
                                          (item['retry_run_id'],item['source_run_id']))
                            continue
                    # Imported reports are archival records; never resend historical failed mail.
                    if table=='reports' and item['mail_status']!='sent':item.update(mail_status='imported',next_attempt=None)
                    if table=='run_retries' and item['state'] in ('pending','queued','running'):item['state']='cancelled'
                    keys=list(item);c.execute('INSERT INTO '+table+'('+','.join(keys)+') VALUES('+','.join('?' for k in keys)+')',[item[k] for k in keys]);added[table]+=1
            for r in payload['content_scopes']:
                c.execute('INSERT OR IGNORE INTO state(key,value) VALUES(?,?)',(r['key'],r['value']))
            for r in payload.get('collection_indexes',[]):
                c.execute('INSERT OR IGNORE INTO state(key,value) VALUES(?,?)',(r['key'],r['value']))
            if payload['history']:
                for p in payload['projects']:
                    c.execute("UPDATE projects SET last_success=(SELECT max(finished) FROM runs WHERE project_id=? AND status='success') WHERE id=?",(p['id'],p['id']))
            if apply_settings:
                merged=store.persisted_settings();merged.update(payload['settings'])
                if not activate:merged.update(smtp_enabled=False,telegram_mode='off')
                config_written=True;store.save_json(store.CONFIG,merged)
                if 'global_schedule' in payload['settings']:
                    c.execute('UPDATE projects SET next_due=? WHERE use_global_schedule=1',(scheduling.next_for(merged['global_schedule']),))
            if apply_secrets and payload.get('credentials') is not None:
                merged=store.persisted_credentials();merged.update(payload['credentials'])
                secrets_written=True;store.save_json(store.SECRETS,merged)
            require(not c.execute('PRAGMA foreign_key_check').fetchone(),'Ungültige Datenbankverknüpfung.')
            c.commit()
        except Exception:
            c.rollback()
            for target in created:target.unlink(missing_ok=True)
            for written,target,old in ((config_written,store.CONFIG,old_settings),(secrets_written,store.SECRETS,old_secrets)):
                if written:
                    if old is None:target.unlink(missing_ok=True)
                    else:
                        tmp=target.with_suffix('.restore');tmp.write_bytes(old);os.chmod(tmp,0o600);tmp.replace(target)
            raise
    return {'projects':len(payload['projects']),'added':added,'files':len(created),'recovery':str(recovery.relative_to(store.DATA))}

def workspace():
    root=store.DATA/'.transfers';root.mkdir(mode=0o700,exist_ok=True)
    cutoff=datetime.now(timezone.utc).timestamp()-3600
    for path in root.iterdir():
        if path.is_dir() and not path.is_symlink() and path.stat().st_mtime<cutoff and IDENTIFIER.fullmatch(path.name):shutil.rmtree(path)
    return root

def new_upload(session):
    token=store.uid();folder=workspace()/token;folder.mkdir(mode=0o700)
    (folder/'session').write_text(hashlib.sha256(session.encode()).hexdigest());os.chmod(folder/'session',0o600)
    return token,folder

def pending(token,session):
    identifier(token);folder=workspace()/token
    require(folder.is_dir() and not folder.is_symlink() and (folder/'session').is_file() and (folder/'session').read_text()==hashlib.sha256(session.encode()).hexdigest(),'Importvorschau abgelaufen oder gehört zu einer anderen Sitzung.')
    return folder
