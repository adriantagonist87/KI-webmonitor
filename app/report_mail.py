"""Durable app-owned report outbox. Gateway retains transport-only access."""
import json,smtplib,ssl
from datetime import datetime,timedelta,timezone
import store,distributions,email_reports
TERMINAL={'sent','preview','imported','skipped','cancelled','unknown'}
class TransportFailure(Exception):
    def __init__(self,definite=False):self.definite=definite

LABELS={'pending':'Versand ausstehend','sending':'Wird versandt','sent':'Versandt','preview':'Vorschau · nicht versandt','failed':'Versand fehlgeschlagen','unknown':'Zustellung unbestätigt','skipped':'Kein versandfähiger Inhalt','cancelled':'Versand aufgehoben','imported':'Importiertes Archiv'}

def event(connection,rid,status,note):
    connection.execute('INSERT INTO report_mail_events(report_id,created,status,note) VALUES(?,?,?,?)',(rid,store.now(),status,note))

def subject(report):
    text='KI-Webmonitor – Wochenbericht'
    if report.get('audience')=='distribution':text+=' · '+report['distribution_alias']
    if report['period_key'].startswith('test-'):text+=' [TEST]'
    return text

def recover():
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        for r in c.execute("SELECT id FROM reports WHERE mail_status='sending' AND next_attempt<=?",(store.now(),)).fetchall():
            c.execute("UPDATE reports SET mail_status='unknown',next_attempt=NULL,error=? WHERE id=?",('Versand unterbrochen; Annahme durch den Mailserver unbestätigt.',r['id']))
            event(c,r['id'],'unknown','Versand unterbrochen; zur Vermeidung doppelter Mails keine automatische Wiederholung.')

def retry(rid,confirm=False):
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
        if not row or row['mail_status'] not in ('failed','unknown'):raise ValueError('Dieser Bericht steht nicht zur Wiederholung bereit.')
        if row['mail_status']=='unknown' and not confirm:raise ValueError('Bitte die mögliche doppelte Zustellung ausdrücklich bestätigen.')
        if not distributions.grant_current(dict(row)):raise ValueError('Der Verteiler oder seine Freigabe hat sich geändert. Bitte eine neue Vorschau erstellen.')
        c.execute("UPDATE reports SET mail_status='pending',next_attempt=NULL,error=NULL,attempts=0 WHERE id=?",(rid,))
        event(c,rid,'pending','Admin hat einen erneuten Versand beauftragt.')

def due_ids(limit=20):
    return [r['id'] for r in store.rows("SELECT id FROM reports WHERE mail_status IN ('pending','failed') AND attempts<3 AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY created,id LIMIT ?",(store.now(),limit))]

async def deliver(rid,sender,force_test=False):
    stamp=store.now()
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
        if not row:raise ValueError('Bericht nicht gefunden.')
        report=dict(row);status=report['mail_status']
        if status in TERMINAL or status=='sending':return {'mode':status,'sent':status=='sent'}
        if status not in ('pending','failed') or report['attempts']>=3 or report['next_attempt'] and report['next_attempt']>stamp:return {'mode':'waiting','sent':False}
        if not distributions.grant_current(report):
            c.execute("UPDATE reports SET mail_status='cancelled',next_attempt=NULL,error=NULL WHERE id=?",(rid,));event(c,rid,'cancelled','Empfänger, Auswahl oder Verteilerstatus geändert; alte Fassung wird nicht versandt.')
            return {'mode':'cancelled','sent':False}
        deadline=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        c.execute("UPDATE reports SET mail_status='sending',attempts=attempts+1,next_attempt=?,error=NULL WHERE id=?",(deadline,rid))
        event(c,rid,'sending','Versandversuch '+str(report['attempts']+1)+' gestartet.')
    transport_started=False
    try:
        # Read the immutable archive, never accept HTML/recipient overrides from a caller.
        images=email_reports.saved_images(report);model=None
        if report.get('audience')=='distribution':
            recipient=distributions.email(report['recipient_email'])
            manifest=store.read_json(store.DATA/'reports'/(rid+'.images.json'))
            model=manifest.get('model',{});scope=json.loads(report['selection_snapshot'])
            if model.get('audience')!='distribution' or set(model.get('projects',{}))!=set(scope['included_project_ids']) or any(p['category'] not in ('changed','unchanged') or p.get('problem') or p.get('app_url') or p.get('notice_count') for p in model['projects'].values()):raise ValueError('Empfängerbericht hat eine ungültige Freigabe.')
        else:recipient=None
        text,html=email_reports.message_parts(report,model)
        if recipient:
            import report_images
            report_images.validate(images,html)
        transport_started=True
        mode=await sender(text,subject(report),force_test or report['period_key'].startswith('test-'),html,images,recipient)
        if mode not in ('preview','production','mailpit'):raise ValueError('Ungültige Versandbestätigung.')
        result='preview' if mode=='preview' else 'sent'
        with store.db() as c:
            c.execute('UPDATE reports SET mail_status=?,sent_at=?,next_attempt=NULL,error=NULL WHERE id=?',(result,store.now() if result=='sent' else None,rid))
            event(c,rid,result,'Vom lokalen Test-Mailserver angenommen.' if mode=='mailpit' else 'Vom SMTP-Server angenommen.' if result=='sent' else 'Vorschau gespeichert; keine E-Mail versandt.')
        return {'mode':mode,'sent':result=='sent'}
    except Exception as exc:
        definite=not transport_started or getattr(exc,'definite',False) or isinstance(exc,(ValueError,smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError,smtplib.SMTPConnectError,ConnectionRefusedError,ssl.SSLError))
        result='failed' if definite else 'unknown'
        # Error classes are sufficient for diagnostics; exception bodies can contain secrets.
        note=('SMTP hat den Versand nicht angenommen' if definite else 'SMTP-Annahme unbestätigt; keine automatische Wiederholung')+' ('+type(exc).__name__+').'
        due=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat() if definite and report['attempts']+1<3 else None
        with store.db() as c:
            c.execute('UPDATE reports SET mail_status=?,next_attempt=?,error=? WHERE id=?',(result,due,note,rid));event(c,rid,result,note)
        return {'mode':result,'sent':False}
