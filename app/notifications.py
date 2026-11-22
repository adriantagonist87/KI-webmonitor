"""Durable Telegram outbox, independent of monitoring status and thirty-minute retries."""
import asyncio
import json
import logging
import os
import re
from datetime import datetime,timedelta,timezone
from urllib.parse import quote,urlsplit,unquote
import httpx
import attention
import article_headlines
import email_reports
import lan_access
import site_privacy
import store
import telegram_message
import vds_catalog

LOCK=asyncio.Lock()
MAX_FINDINGS = 3
FINDING_LIMIT = 260


def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS notifications(
        id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        created TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
        due_at TEXT NOT NULL,sent_at TEXT,error TEXT,message TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS notification_due ON notifications(status,due_at)')
    c.execute("UPDATE notifications SET status='uncertain',error='Neustart während des Versands; Zustellung unklar. Keine automatische Doppelmeldung.' WHERE status='sending'")


def enabled(project,s=None):
    mode=(s if s is not None else store.settings()).get('telegram_mode','off')
    return mode=='all' or mode=='projects' and bool(project.get('telegram_enabled'))


def relevant_changes(rid):
    result=[]
    for row in store.rows('SELECT * FROM changes WHERE run_id=? ORDER BY detected,id',(rid,)):
        if row['previous_id'] is None and row['kind'] in ('Neue Seite','Neues Dokument'):continue
        evidence=json.loads(row['evidence'])
        if attention.priority(row['kind'],evidence)=='attention':result.append((row,evidence))
    return result


def redact(text):
    text=site_privacy.redact_text(text)
    return re.sub(r'(?i)(password|passwort|api[_ -]?key|authorization|access_token|refresh_token|bot[_ -]?token)\s*[:=]\s*\S+',r'\1=[entfernt]',text)


def compact(text,limit):
    return redact(email_reports.plain(text,limit))


def report_url(run):
    origin = (lan_access.public_origin(os.environ.get('KIWM_PUBLIC_URL'))
              or lan_access.lan_origin(os.environ.get('KIWM_LAN_URL')))
    rid = run.get('id')
    return origin + '/runs/' + quote(rid, safe='') if origin and rid else None


def change_text(row, evidence):
    kind = row['kind']
    if 'Datei' in kind or 'Dokument' in kind:
        filename = compact(unquote(urlsplit(row['url']).path.rsplit('/', 1)[-1]), 110)
        return (filename + ' · ' if filename else '') + compact(kind, FINDING_LIMIT)
    assessment = evidence.get('ai_assessment') or {}
    text = compact(assessment.get('summary') or assessment.get('reason') or evidence.get('note') or kind,
                   FINDING_LIMIT)
    if text.endswith(' …'):
        sentence = re.match(r'.{100,}[.!?](?= [A-ZÄÖÜ])', text)
        if sentence:
            return sentence.group()
    return text


def change_findings(row,evidence,run,titles):
    catalog = vds_catalog.list_changes(row,evidence)
    if catalog:
        findings=[]
        for item in catalog:
            label='Aus der Startseitenliste verschwunden' if item['state']=='removed' else 'Neu gelistet'
            findings.append(redact(f'{label} · {item["number"]} „{item["title"]}“'))
        if len(catalog)<len(evidence.get('links_added',[]))+len(evidence.get('links_removed',[])):
            findings.append(change_text(row,evidence))
        return findings
    release=evidence.get('release') or {}
    features=[f['text'] for f in evidence.get('features',[]) if isinstance(f,dict) and isinstance(f.get('text'),str)]
    if release.get('tag') and features:
        return [compact('Neue stabile Version '+release['tag']+' · '+features[0],340)]
    source={**row,'run_id':row.get('run_id') or run.get('id'),'run_summary':run.get('summary')}
    details=email_reports.specific_details(source,evidence,titles,catalog_limit=None)
    findings=[]
    for detail in details:
        text=compact(detail,340)
        status=re.search(r'(?:VDE )?(?:Norm|Entwurf|Anwendungsregel|Vornorm) \([^)]+\)',detail)
        if status and status.group() not in text:
            text=compact(detail,280)+' · '+redact(status.group())
        findings.append(text)
    return findings or [change_text(row,evidence)]


def message(project,run,changes):
    lines=['KI-Webmonitor · '+compact(project['name'],180)]
    articles = article_headlines.from_changes([row for row,_ in changes]) if article_headlines.enabled(project.get('prompt')) else None
    if articles is not None:
        lines.extend('• ' + redact(item['title']) for item in articles)
    elif changes:
        titles={}
        findings=list(dict.fromkeys(
            finding for row,evidence in changes for finding in change_findings(row,evidence,run,titles)))
        lines.append('\n'.join('• ' + finding for finding in findings[:MAX_FINDINGS]))
        extra = max(0,len(findings)-MAX_FINDINGS)
        if extra:
            lines.append(f'+ {extra} weitere ' + ('Änderung' if extra == 1 else 'Änderungen') + ' in der Auswertung.')
    if run['status'] in ('failed','partial'):
        lines.append('Prüfung fehlgeschlagen' if run['status']=='failed' else 'Prüfung unvollständig')
    return telegram_message.compose(lines, project['url'], report_url(run))


def queue_run(rid):
    run=store.one('SELECT * FROM runs WHERE id=?',(rid,))
    if not run or not run['finished'] or run['status'] not in ('success','partial','failed'):return
    project=store.one('SELECT * FROM projects WHERE id=?',(run['project_id'],))
    if not project or not enabled(project):return
    changes=relevant_changes(rid)
    if run['status']=='success' and article_headlines.enabled(project.get('prompt')):
        articles=article_headlines.from_changes([row for row,_ in changes])
        if articles==[]:return
    if not changes and run['status']=='success':return
    stamp=store.now()
    store.execute('''INSERT OR IGNORE INTO notifications(id,run_id,project_id,created,status,due_at,message)
        VALUES(?,?,?,?,'queued',?,?)''',(store.uid(),rid,project['id'],stamp,stamp,message(project,run,changes)))


async def dispatch_one():
    async with LOCK:
        with store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM notifications WHERE status='queued' AND due_at<=? ORDER BY created LIMIT 1",(store.now(),)).fetchone()
            if not row:return
            row=dict(row);p=c.execute('SELECT * FROM projects WHERE id=?',(row['project_id'],)).fetchone()
            if not p or not enabled(dict(p)):
                c.execute("UPDATE notifications SET status='cancelled',error='Telegram-Versand deaktiviert.' WHERE id=?",(row['id'],));return
            c.execute("UPDATE notifications SET status='sending',attempts=attempts+1 WHERE id=?",(row['id'],))
        try:
            async with httpx.AsyncClient(timeout=30,trust_env=False) as client:
                response=await client.post('http://gateway:8000/telegram/send',json={'text':row['message']})
                result=response.json()
            if result.get('ok') is True:
                store.execute("UPDATE notifications SET status='sent',sent_at=?,error=NULL WHERE id=?",(store.now(),row['id']));return
            if result.get('uncertain'):status='uncertain'
            elif result.get('retry_after') and row['attempts']<2:status='queued'
            else:status='failed'
            delay=result.get('retry_after')
            delay=max(1,min(delay,86401)) if type(delay) is int else 60
            due=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
            store.execute('UPDATE notifications SET status=?,error=?,due_at=? WHERE id=?',(status,str(result.get('error') or 'Telegram-Versand fehlgeschlagen.')[:500],due,row['id']))
        except (httpx.ConnectError,httpx.ConnectTimeout):
            due=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
            store.execute('UPDATE notifications SET status=?,error=?,due_at=? WHERE id=?',
                ('queued' if row['attempts']<2 else 'failed','Kommunikationsdienst nicht erreichbar.',due,row['id']))
        except Exception:
            # No automatic retry after an ambiguous gateway response: avoid duplicates.
            store.execute("UPDATE notifications SET status='uncertain',error='Telegram-Zustellung unklar; keine automatische Doppelmeldung.' WHERE id=?",(row['id'],))


async def worker():
    while True:
        try:await dispatch_one()
        except asyncio.CancelledError:raise
        except Exception as exc:logging.warning('Telegram-Ausgang: %s',type(exc).__name__)
        await asyncio.sleep(3)
