import asyncio
import hmac
import json
import math
import secrets
import os
import browser_mode
import navigation_macros
import vde_norms
from navigation_recording import Recorder
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlencode, quote
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from report_render import render_report
from markupsafe import Markup, escape
from starlette.middleware.trustedhost import TrustedHostMiddleware
from store import *
import engine
from github_releases import API_HOST as GITHUB_API_HOST, repository as github_repository
import attention
from prompt_examples import EXAMPLES
from project_meta import DEFAULT_COLOR, normalize_color, normalize_tags, appearance
import transfer
import site_access
import scheduling
import site_safety
import tag_registry
import project_lifecycle
import history_cleanup
import maintenance
import project_icons
import email_reports
import distributions
import report_mail
import mail_message
import report_images
from project_urls import PROJECT_DETAIL_ROUTE, detail_path
import retention
import document_files
import document_names
import configuration
import communication_config
import notifications
import dashboard_controls
import lan_access
import admin_auth
from document_metadata import compact_document_metadata
import retry_jobs
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

macro_recorder = Recorder()

@asynccontextmanager
async def lifespan(app):
    init()
    task = asyncio.create_task(engine.scheduler())
    delivery = asyncio.create_task(notifications.worker())
    names = asyncio.create_task(document_names.worker(engine.llm))
    try:
        yield
    finally:
        for worker in (task, delivery, names):
            worker.cancel()
        await asyncio.gather(task, delivery, names, return_exceptions=True)
        await macro_recorder.shutdown(engine.BUSY)

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
LAN_URL = lan_access.lan_origin(os.environ.get('KIWM_LAN_URL'))
PUBLIC_URL = lan_access.public_origin(os.environ.get('KIWM_PUBLIC_URL'))
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'app', 'testserver']
                   + ([urlsplit(LAN_URL).hostname] if LAN_URL else [])
                   + ([urlsplit(PUBLIC_URL).hostname] if PUBLIC_URL else []))
FAVICON_FILE = Path(__file__).parent / 'static/favicon.ico'
APP_FAVICON_URL = '/favicon-' + digest(FAVICON_FILE.read_bytes())[:12] + '.ico'
FAVICON_PATHS = {'/favicon.ico', '/static/favicon.ico', APP_FAVICON_URL}


@app.api_route(APP_FAVICON_URL, methods=['GET', 'HEAD'], include_in_schema=False)
@app.api_route('/favicon.ico', methods=['GET', 'HEAD'], include_in_schema=False)
async def app_favicon():
    return FileResponse(FAVICON_FILE, media_type='image/vnd.microsoft.icon')

env = Environment(loader=FileSystemLoader('/app/templates'), autoescape=select_autoescape(['html']))
env.globals['app_favicon_url'] = APP_FAVICON_URL
env.filters['markdown'] = render_report

STATUS_LABELS = {'queued':'Wartet','running':'Läuft','success':'Erfolgreich','partial':'Unvollständig','failed':'Fehler','cancelled':'Abgebrochen','protection':'Bot-Schutz'}
env.filters['status_label'] = lambda value: STATUS_LABELS.get(value, 'Ungeprüft')

def localtime(value, pattern='%d.%m.%Y · %H:%M'):
    if not value:
        return 'Noch keine Prüfung'
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime(pattern)
    except (ValueError, TypeError):
        return str(value)

def relative_age(value):
    try:
        dt=datetime.fromisoformat(value)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        seconds=max(0,int((datetime.now(timezone.utc)-dt).total_seconds()))
        if seconds>=86400:
            n=seconds//86400;return f'vor {n} Tag' + ('en' if n!=1 else '')
        if seconds>=3600:
            n=seconds//3600;return f'vor {n} Stunde' + ('n' if n!=1 else '')
        n=seconds//60
        return f'vor {n} Minute' + ('n' if n!=1 else '') if n else 'gerade eben'
    except (TypeError,ValueError): return ''

def retry_label(retry):
    if not retry:
        return ''
    if retry['state'] == 'running':
        return 'Wiederholung läuft'
    if retry['state'] == 'queued':
        return 'Wiederholung wartet'
    try:
        due = datetime.fromisoformat(retry['due_at'])
        if due.tzinfo is None:
            return 'Wiederholung wartet'
        seconds = max(0, math.ceil((due - datetime.now(timezone.utc)).total_seconds()))
        if seconds:
            return f'Wird wiederholt in {seconds // 60:02}:{seconds % 60:02}'
    except (ValueError, TypeError, KeyError):
        pass
    return 'Wiederholung wartet'

env.filters['relative_age'] = relative_age
def audit_time(value):
    try:return datetime.fromisoformat(value).astimezone(TZ).strftime('%d.%m.%Y · %H:%M:%S')
    except (ValueError,TypeError):return value or '—'

env.filters['audit_time']=audit_time
env.filters['localtime'] = localtime
env.filters['hostname'] = lambda value: urlsplit(value).hostname or value

def storage_size(value):
    size = max(0, int(value or 0))
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{size} B'
        size /= 1024

env.filters['storage_size'] = storage_size

def file_size_mb(size):
    if 0 < size < 10_000:
        return '<0,01 MB'
    return f'{size / 1_000_000:.2f} MB'.replace('.', ',')

env.filters['file_size_mb'] = file_size_mb
env.filters['schedule_label'] = scheduling.label
NOTICES = {
    'distribution_saved':'Verteiler gespeichert',
    'distribution_deleted':'Verteiler gelöscht; Berichte bleiben archiviert',
    'report_retry':'Versand erneut vorgemerkt','project_saved':'Projekt gespeichert.','ki_saved':'KI-Konfiguration gespeichert.','smtp_saved':'E-Mail-Einstellungen gespeichert.','reviewed':'Angezeigte Änderungen als geprüft markiert.','manual_on':'Manueller Browsermodus aktiviert.','manual_off':'Manueller Browsermodus beendet.','models_loaded':'Modellliste aktualisiert.','test_done':'Verbindungstest abgeschlossen.','report_created':'Testbericht erstellt. Versandstatus im Bericht prüfen.'}
NOTICES.update(project_deleted='Projekt gelöscht.', tag_saved='Tag gespeichert.', tag_deleted='Tag entfernt.', schedule_saved='Globale Prüfzeit gespeichert.')
NOTICES.update(storage_saved='Speicherregeln gespeichert.', storage_cleaned='Speicherbereinigung abgeschlossen.')
NOTICES.update(history_cleaned='Prüfverlauf bereinigt.',
               history_files_pending='Prüfverlauf bereinigt. Verbliebene Dateien können über eine neue Vorschau erneut bereinigt werden.')
NOTICES.update(telegram_saved='Telegram-Einstellungen gespeichert.')

def diff_markup(text):
    result = []
    for line in (text or '').splitlines():
        cls = 'diff-added' if line.startswith('+') and not line.startswith('+++') else 'diff-removed' if line.startswith('-') and not line.startswith('---') else ''
        result.append(Markup('<span class="{}">{}</span>').format(cls, escape(line)) if cls else escape(line))
    return Markup('\n').join(result)

env.filters['diff_markup'] = diff_markup

@app.middleware('http')
async def protect(request, call_next):
    public_request=lan_access.is_lan_request(request,PUBLIC_URL)
    if PUBLIC_URL and request.headers.get('host','').split(':',1)[0]==urlsplit(PUBLIC_URL).hostname and not public_request:
        return JSONResponse({'error':'Nicht freigegebene externe Adresse'},400)
    if public_request:
        # The configured external origin terminates HTTPS at the reverse proxy.
        # Do not trust client-supplied Forwarded or X-Forwarded-* headers.
        request.scope['scheme']='https'
    token = request.cookies.get('kiwm_csrf') or secrets.token_urlsafe(32)
    request.state.csrf = token
    auth = admin_auth.current()
    request.state.admin_username = ''
    request.state.auth_enabled = auth.enabled
    public = request.url.path in ('/login', '/auth/check', '/health') or request.url.path in FAVICON_PATHS or request.url.path.startswith('/static/')
    if not public:
        if auth.error or (not auth.enabled and (lan_access.is_lan_request(request, LAN_URL) or public_request)):
            return secure_response(request, login_page(request, 'Administratorzugang ist noch nicht eingerichtet.', 503))
        if auth.enabled:
            valid = await asyncio.to_thread(auth.authorized, request.cookies.get(admin_auth.COOKIE))
            if not valid:
                target = admin_auth.safe_next(request.url.path + ('?' + request.url.query if request.url.query else ''))
                response = (JSONResponse({'error': 'Bitte erneut anmelden.', 'login_url': '/login?next=' + quote(target, safe='')}, 401)
                            if request.method != 'GET' or request.url.path.startswith('/api/')
                            else RedirectResponse('/login?next=' + quote(target, safe=''), 303))
                return secure_response(request, response)
            request.state.admin_username = auth.username
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if request.url.path == '/login' and (not request.headers.get('content-length', '').isdigit()
                or int(request.headers['content-length']) > 4096):
            return secure_response(request, login_page(request, 'Die Anmeldung konnte nicht verarbeitet werden.', 400))
        upload_csrf = False
        origin = request.headers.get('origin')
        if origin and ((public_request and origin!=PUBLIC_URL) or urlsplit(origin).netloc != request.headers.get('host')):
            if request.url.path == '/login':
                return secure_response(request, login_page(request, 'Bitte die Anmeldung auf dieser Seite erneut öffnen.', 403))
            return JSONResponse({'error': 'Fremder Ursprung abgewiesen'}, 403)
        if request.headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
            await request.body()
            form = await request.form()
            supplied = form.get('csrf', '')
        elif request.headers.get('content-type', '').startswith('multipart/form-data'):
            # Reject foreign origins before parsing the bounded upload.
            length=request.headers.get('content-length')
            if length and (not length.isdigit() or int(length)>transfer.MAX_UPLOAD+65536):
                return JSONResponse({'error':'Datei übersteigt 512 MB'},413)
            if request.url.path!='/transfer/preview' or request.method!='POST':
                return JSONResponse({'error':'Multipart nur für Importdateien erlaubt'},403)
            # Do not consume this stream in BaseHTTPMiddleware. The upload route
            # validates its form token before storing or processing any package.
            upload_csrf=True
            supplied=''
        else:
            supplied = request.headers.get('x-csrf-token', '')
        if not request.cookies.get('kiwm_csrf') or (not upload_csrf and not hmac.compare_digest(str(supplied), token)):
            if request.url.path == '/login':
                return secure_response(request, login_page(request, 'Bitte erneut anmelden. Die Formularsitzung ist abgelaufen.', 403))
            return JSONResponse({'error': 'CSRF-Prüfung fehlgeschlagen'}, 403)
    response = await call_next(request)
    return secure_response(request, response)


def secure_cookie(request):
    return (lan_access.is_lan_request(request,PUBLIC_URL) or request.url.scheme == 'https'
            or bool(LAN_URL and LAN_URL.startswith('https:') and lan_access.is_lan_request(request, LAN_URL)))


def secure_response(request, response):
    public_icon = (request.url.path in FAVICON_PATHS and request.method in ('GET', 'HEAD')
                   and response.status_code in (200, 206, 304))
    if not public_icon and not request.cookies.get('kiwm_csrf'):
        response.set_cookie('kiwm_csrf', request.state.csrf, httponly=True, samesite='strict', secure=secure_cookie(request))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers['Referrer-Policy'] = 'same-origin'
    if public_icon:
        response.headers['Cache-Control'] = ('public, max-age=31536000, immutable'
                                             if request.url.path == APP_FAVICON_URL else 'public, max-age=0, must-revalidate')
    else:
        response.headers['Cache-Control'] = 'no-store'
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    return response


def login_page(request, error='', status=200, username='', target=None):
    auth = admin_auth.current()
    configured = bool(auth.enabled and not auth.error)
    response = HTMLResponse(env.get_template('login.html').render(
        csrf=request.state.csrf, error=error or auth.error or ('' if configured else 'Administratorzugang ist noch nicht eingerichtet.'),
        configured=configured, username=username, next_url=admin_auth.safe_next(target or request.query_params.get('next'))), status)
    return response


@app.get('/login')
async def login_form(request: Request):
    auth = admin_auth.current()
    if auth.enabled and await asyncio.to_thread(auth.authorized, request.cookies.get(admin_auth.COOKIE)):
        return RedirectResponse(admin_auth.safe_next(request.query_params.get('next')), 303)
    return login_page(request, status=503 if auth.error or not auth.enabled else 200)


@app.post('/login')
async def login_submit(request: Request):
    auth = admin_auth.current()
    form = await request.form()
    username, password = str(form.get('username', '')), str(form.get('password', ''))
    target = admin_auth.safe_next(form.get('next'))
    # nginx overwrites this header; arbitrary external Forwarded headers are ignored.
    client = request.headers.get('x-kiwm-client-ip') or (request.client.host if request.client else 'unknown')
    session, error = await asyncio.to_thread(auth.login, username, password, client, request.cookies.get(admin_auth.COOKIE))
    if error:
        response = login_page(request, {'configuration': 'Administratorzugang ist noch nicht eingerichtet.',
            'rate': 'Zu viele Anmeldeversuche. Bitte in einer Minute erneut versuchen.',
            'credentials': 'Benutzername oder Passwort ist nicht korrekt.'}[error],
            {'configuration': 503, 'rate': 429, 'credentials': 401}[error], username[:80], target)
        if error == 'rate':
            response.headers['Retry-After'] = '60'
        return response
    response = RedirectResponse(target, 303)
    response.set_cookie(admin_auth.COOKIE, session, max_age=auth.hours * 3600, httponly=True,
                        secure=secure_cookie(request), samesite='lax', path='/')
    response.set_cookie('kiwm_csrf', secrets.token_urlsafe(32), httponly=True, secure=secure_cookie(request), samesite='strict', path='/')
    return response


@app.post('/logout')
async def logout(request: Request):
    await asyncio.to_thread(admin_auth.current().logout, request.cookies.get(admin_auth.COOKIE))
    response = RedirectResponse('/login', 303)
    response.delete_cookie(admin_auth.COOKIE, path='/')
    response.delete_cookie('kiwm_csrf', path='/')
    return response


@app.get('/auth/check')
async def auth_check(request: Request):
    auth = admin_auth.current()
    if auth.error or (not auth.enabled and (lan_access.is_lan_request(request, LAN_URL) or lan_access.is_lan_request(request,PUBLIC_URL))):
        return Response(status_code=401)
    valid = not auth.enabled or await asyncio.to_thread(auth.authorized, request.cookies.get(admin_auth.COOKIE))
    return Response(status_code=204 if valid else 401)

def breadcrumb_path(page, ctx):
    if page == 'home':
        return []
    path = [('/', 'Projekte')]
    sections = {
        'files': ('/files', 'Globales Dateiverzeichnis'),
        'reports': ('/reports', 'Wochenberichte'),
        'distributions': ('/distributions', 'Verteilerlisten'),
        'browser': ('/browser', 'Brave-Browser'),
        'tags': ('/tags', 'Tags'),
        'telegram': ('/telegram', 'Telegram'),
        'smtp': ('/smtp', 'SMTP-Mailserver'),
        'settings': ('/settings', 'Einstellungen'),
        'maintenance': ('/maintenance', 'Wartung & Bereinigung'),
        'transfer': ('/transfer', 'Export / Import'),
        'prompt_help': ('/help/prompts', 'Prompt-Beispiele'),
    }
    if page in sections:
        return path + [sections[page]]
    if page in ('project', 'edit', 'site_access', 'macros'):
        project_context = ctx.get('project') or {}
        if not project_context.get('id'):
            return path + [('/projects/new', 'Neues Projekt')]
        url = detail_path(project_context['id'])
        path.append((url, project_context['name'] or 'Projekt'))
        if page == 'edit':
            path.append((url + '/edit', 'Bearbeiten'))
        if page == 'site_access':
            path.append((url + '/access', 'Webseitenzugang'))
        if page == 'macros':
            path.append((url + '/macros', 'Browser-Makros'))
    elif page == 'run':
        run = ctx['run']
        started = run.get('started')
        path.extend([(detail_path(run['project_id']), run['name']),
                     ('/runs/' + run['id'], 'Prüfung vom ' + localtime(started) if started else 'Prüfung')])
    elif page == 'document':
        document = ctx['document']
        url = '/documents/' + document['id']
        if ctx.get('from_files'):
            path.append(sections['files'])
            url += '?from=files'
        else:
            path.extend([(detail_path(document['project_id']), document['project_name']),
                         ('/runs/' + document['run_id'], 'Prüfung')])
        path.append((url, 'Archiviertes Dokument'))
    elif page == 'report':
        report = ctx['report']
        path.append(sections['reports'])
        if report.get('parent_id'):
            path.append(('/reports/' + report['parent_id'], 'Globaler Wochenbericht'))
        path.append(('/reports/' + report['id'], report.get('distribution_alias') or 'Wochenbericht'))
    elif page == 'distribution_edit':
        distribution = ctx['distribution']
        path.append(sections['distributions'])
        if distribution.get('id'):
            path.append(('/distributions/' + quote(distribution['id'], safe='') + '/edit',
                         distribution.get('alias') or 'Verteiler bearbeiten'))
        else:
            path.append(('/distributions/new', 'Neue Verteilerliste'))
    return path


def render(request, page, **ctx):
    project_context = ctx.get('project') or {}
    alias = project_context.get('name', '')
    titles = {
        'project': alias,
        'edit': alias + ' – Bearbeiten' if project_context.get('id') else 'Neues Projekt',
        'site_access': alias + ' – Webseitenzugang' if alias else 'Webseitenzugang',
        'macros': alias + ' – Browser-Makros',
        'run': (ctx.get('run') or {}).get('name', '') + ' – Prüfung',
    }
    ctx.setdefault('page_title', titles.get(page))
    ctx.setdefault('breadcrumbs', breadcrumb_path(page, ctx))
    ctx.setdefault('tag_options', known_tags())
    ctx.setdefault('global_schedule', scheduling.global_schedule())
    ctx.setdefault('global_schedule_label', scheduling.label(ctx['global_schedule']))
    ctx.setdefault('server_now', now())
    ctx.setdefault('vde_norms_supported', vde_norms.matches(project_context.get('url')))
    ctx.setdefault('browser_ui_url', lan_access.browser_url(request, LAN_URL,
                   companion_url('BROWSER_UI_URL', 'http://localhost:8766'), PUBLIC_URL))
    ctx.setdefault('mailpit_ui_url', companion_url('MAILPIT_UI_URL', 'http://localhost:8767'))
    ctx.setdefault('test_tools', os.environ.get('KIWM_TEST_TOOLS')=='1')
    ctx.setdefault('auth_enabled', getattr(request.state, 'auth_enabled', False))
    ctx.setdefault('admin_username', getattr(request.state, 'admin_username', ''))
    return HTMLResponse(env.get_template('index.html').render(page=page, csrf=request.state.csrf, manual=manual(), busy=engine.BUSY.locked(), settings=settings(), notice=NOTICES.get(request.query_params.get('notice')), **ctx))


def companion_url(name, default):
    """Companion interfaces stay on the protected local forwarding path."""
    value = os.environ.get(name, default).strip().rstrip('/')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname in ('localhost', '127.0.0.1', '::1')
                 and parsed.port and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    return value if valid else default


def project_form_values(form):
    """Redisplay rejected project input without writing to the archive."""
    pid = str(form.get('id', ''))
    current = project(pid) if pid and one('SELECT id FROM projects WHERE id=?', (pid,)) else {}
    value = dict(current)
    for field in ('name', 'url', 'prompt', 'domains', 'color'):
        value[field] = str(form.get(field, current.get(field, '')))
    value['crawl_profile'] = str(form.get('crawl_profile',current.get('crawl_profile','gentle')))
    previous_input = current.get('browser_input', {})
    value['browser_input'] = {'mode':str(form.get('browser_mode',previous_input.get('mode','standard'))),
                              'entry_url':str(form.get('browser_entry_url',previous_input.get('entry_url',''))),
                              'flow':str(form.get('browser_flow',previous_input.get('flow','')))}
    value.update(id=pid, enabled=form.get('enabled') == 'on',
                 use_global_schedule=form.get('use_global_schedule') == 'on')
    chosen = set(form.getlist('tag_ids'))
    value['tags'] = [tag['name'] for tag in known_tags() if tag['id'] in chosen]
    selected = dict(current.get('schedule') or scheduling.global_schedule())
    for field, key in (('schedule_mode', 'mode'), ('schedule_time', 'time'),
                       ('schedule_weekday', 'weekday'), ('schedule_monthday', 'monthday'),
                       ('schedule_interval', 'interval')):
        if field in form:
            selected[key] = str(form[field])
    return value, selected


@app.exception_handler(StarletteHTTPException)
async def browser_error(request: Request, error):
    # Machine endpoints retain JSON errors. Native navigation keeps the app shell
    # and rejected project values instead of replacing the form with raw JSON.
    if 'text/html' not in request.headers.get('accept', '') or request.url.path.startswith('/api/'):
        return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=error.headers)
    message = str(error.detail) if isinstance(error.detail, str) else 'Die Anfrage konnte nicht verarbeitet werden.'
    if error.status_code == 404:
        message = 'Diese Seite oder Datei ist nicht mehr vorhanden.'
    if request.method == 'POST' and request.url.path == '/projects/save' and error.status_code == 400:
        value, selected = project_form_values(await request.form())
        response = render(request, 'edit', project=value, schedule=selected, form_error=message)
    else:
        path = request.url.path
        back = '/browser' if path == '/manual' or path.endswith('/run') else '/settings' if path.startswith('/settings/') else '/'
        response = render(request, 'error', error_message=message, back_url=back)
    response.status_code = error.status_code
    if error.headers:
        response.headers.update(error.headers)
    return response

def redirect(path, notice=None):
    return RedirectResponse(path + ('?' + urlencode({'notice': notice}) if notice in NOTICES else ''), 303)

site_access.register(app,render,redirect)

def project(pid):
    p = one('SELECT * FROM projects WHERE id=?', (pid,))
    if not p:
        raise HTTPException(404)
    p.update(appearance(p))
    decorate_project(p)
    return p

def decorate_project(p):
    p['crawl_profile'] = site_safety.profile(p['id'])
    p['browser_input'] = browser_mode.get(p)
    p['tag_chips'] = tag_registry.decorate(p)
    saved_icon=project_icons.get(p['id'])
    p['icon_url'] = project_icons.image_url(p['id'], saved_icon)
    p['icon_mode']=saved_icon['mode'] if saved_icon else 'auto'
    p['schedule'] = scheduling.project_schedule(p)
    p['schedule_label'] = scheduling.label(p['schedule'])
    p['effective_schedule_label'] = ('Global · ' if p.get('use_global_schedule') else '') + p['schedule_label']
    p['active_run'] = bool(one("SELECT id FROM runs WHERE project_id=? AND status IN ('running','queued')", (p['id'],)))
    p['provider_protection'] = site_safety.latest_protection(p['id']) if not p['active_run'] else None
    if p['provider_protection']:
        p['status'] = 'protection'
    p['retry'] = retry_jobs.project_retry(p['id'])
    p['retry_label'] = retry_label(p['retry'])
    return p

def run_evidence(rid):
    try:
        return read_json(DATA/'artifacts'/rid/'evidence.json')
    except (OSError,ValueError):
        return {}


def finding_entries(changes, finished=None):
    """Present exactly the findings that drive attention, without another model."""
    entries=[]
    for change in changes:
        if change.get('priority')!='attention' or (change.get('previous_id') is None
                and change.get('kind') in ('Neue Seite','Neues Dokument')):
            continue
        evidence=change.get('evidence') or {}
        assessment=evidence.get('ai_assessment') if isinstance(evidence,dict) else None
        reason=assessment.get('reason') if isinstance(assessment,dict) else None
        if not isinstance(reason,str) or not reason.strip():
            reason='Ein archivierter Unterschied ist noch offen. Eine fachliche Einordnung liegt nicht vor.'
        url=change.get('url','');href=''
        try:
            parsed=urlsplit(url)
            if (parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password
                    and not any(ord(character)<32 for character in url)):
                href=url
        except (TypeError,ValueError):pass
        entries.append({'id':change['id'],'reason':reason,'url':url,'href':href,
                        'time':change.get('finished') or finished or change.get('detected'),
                        'run_id':change['run_id']})
    return entries


def project_history(pid):
    history = rows('SELECT * FROM runs WHERE project_id=? ORDER BY started DESC,rowid DESC LIMIT 100', (pid,))
    if not history:
        return history
    placeholders = ','.join('?' for _ in history)
    candidates = rows('''SELECT run_id,kind,evidence FROM changes
        WHERE project_id=? AND run_id IN (''' + placeholders + ''')
        AND COALESCE(json_extract(evidence,'$.ai_assessment.relevance'),'uncertain')<>'irrelevant'
        AND NOT (previous_id IS NULL AND kind IN ('Neue Seite','Neues Dokument'))''',
        (pid, *(run['id'] for run in history)))
    changed = {change['run_id'] for change in candidates
               if attention.priority(change['kind'], json.loads(change['evidence'])) == 'attention'}
    uncertain = {change['run_id'] for change in candidates
                 if (json.loads(change['evidence']).get('ai_assessment') or {}).get('relevance') == 'uncertain'}
    unfinished = {'queued': 'Prüfung ausstehend', 'running': 'Prüfung läuft',
                  'failed': 'Prüfung fehlgeschlagen', 'partial': 'Prüfung unvollständig',
                  'cancelled': 'Nicht ausgeführt'}
    for run in history:
        completed = bool(run['finished']) and run['status'] in ('success', 'partial')
        if completed and run['id'] in changed:
            run['history_result'] = 'Änderungen erkannt'
        elif completed and run['id'] in uncertain:
            run['history_result'] = 'Änderung noch unklar'
        elif completed and run['status'] == 'success':
            run['history_result'] = 'Keine Änderung erkannt'
        else:
            run['history_result'] = unfinished.get(run['status'], 'Prüfung nicht abgeschlossen')
    return history

@app.get('/health')
async def health():
    return {'status': 'ok', 'manual': manual(), 'busy': engine.BUSY.locked()}

DASHBOARD_RUN_COLUMNS = '''
    (SELECT status FROM runs WHERE project_id=p.id AND status<>'cancelled'
     ORDER BY started DESC,rowid DESC LIMIT 1) AS status,
    COALESCE((SELECT finished FROM runs WHERE project_id=p.id
      AND status IN ('success','partial','failed') AND finished IS NOT NULL
      ORDER BY julianday(finished) DESC,rowid DESC LIMIT 1), p.last_success) AS last_checked
'''
DASHBOARD_ERRORS = ('failed', 'partial', 'protection')


def dashboard_check_time(project):
    try:
        stamp = datetime.fromisoformat(project['last_checked'])
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return -math.inf


@app.get('/')
async def home(request: Request):
    projects = rows(f'SELECT p.*, {DASHBOARD_RUN_COLUMNS} FROM projects p ORDER BY name,id')
    for p in projects:
        p.update(appearance(p))
        p.update(attention.summary(p['id']))
        decorate_project(p)
        p['last_change'] = attention.latest_relevant_change(p['id'])
    projects.sort(key=lambda p: (p['status'] == 'running', p['status'] in DASHBOARD_ERRORS,
                                 dashboard_check_time(p)), reverse=True)
    return render(request, 'home', projects=projects, tag_options=known_tags())

@app.get('/api/project-status')
async def project_retry_status():
    # Only presentation data; no run creation or model call on this GET.
    projects = rows(f'SELECT p.id,p.enabled,p.last_success, {DASHBOARD_RUN_COLUMNS} FROM projects p ORDER BY name,id')
    for p in projects:
        p['retry'] = retry_jobs.project_retry(p['id'])
        p['last_change'] = attention.latest_relevant_change(p['id'])
        if p['status'] not in ('running','queued') and site_safety.latest_protection(p['id']):
            p['status'] = 'protection'
    return {'server_now': now(), 'projects': projects}

def known_tags():
    return tag_registry.all_tags()

@app.get('/projects/new')
async def new_project(request: Request):
    return render(request, 'edit', project=None, tag_options=known_tags(), schedule=scheduling.global_schedule())


@app.post('/projects/{pid}/automation')
async def project_automation(pid:str,request:Request):
    form=await request.form()
    if len(form.getlist('enabled'))!=1 or form.get('enabled') not in ('0','1'):
        raise HTTPException(400,'Automatik muss ein- oder ausgeschaltet sein.')
    try:result=dashboard_controls.set_automation(pid,int(form['enabled']))
    except LookupError:raise HTTPException(404,'Projekt nicht gefunden.') from None
    if 'application/json' in request.headers.get('accept',''):return result
    return redirect('/')

@app.get('/help/prompts')
async def prompt_help(request: Request):
    return render(request, 'prompt_help', examples=EXAMPLES)

@app.get('/projects/{pid}/edit')
async def edit_project(pid: str, request: Request):
    p = project(pid)
    return render(request, 'edit', project=p, tag_options=known_tags(), schedule=p['schedule'])

@app.post('/projects/save')
async def save_project(request: Request):
    f = await request.form()
    name, url, prompt = (str(f.get(k, '')).strip() for k in ('name', 'url', 'prompt'))
    try:
        u = urlsplit(url)
    except ValueError:
        raise HTTPException(400, 'Bitte eine gültige HTTP(S)-Adresse angeben.') from None
    if not name or not prompt or len(prompt) > 40000 or u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
        raise HTTPException(400, 'Name, HTTP(S)-URL und Prompt sind erforderlich; keine Zugangsdaten in der URL')
    entered_domains = str(f.get('domains', '')).strip().lower()
    domains = entered_domains or u.hostname
    if not entered_domains and github_repository(url):
        domains += ', ' + GITHUB_API_HOST
    domain_list = [x.strip() for x in domains.split(',')]
    if any(not x or '/' in x or ':' in x or '*' in x for x in domain_list) or u.hostname not in domain_list:
        raise HTTPException(400, 'Erlaubte Domains als genaue Hostnamen angeben, einschließlich Start-URL-Hostname')
    if github_repository(url) and GITHUB_API_HOST not in domain_list:
        raise HTTPException(400, 'Für GitHub-Releases bitte auch api.github.com als Hostname erlauben.')
    pid = str(f.get('id', '')) or uid()
    enabled = int(f.get('enabled') == 'on')
    existing=one('SELECT * FROM projects WHERE id=?',(pid,))
    try:
        crawl_profile = str(f.get('crawl_profile',site_safety.profile(pid) if existing else 'gentle'))
        if crawl_profile not in site_safety.PROFILES:
            raise ValueError('Bitte ein gültiges Prüftempo wählen.')
        previous_input = browser_mode.get(existing) if existing else browser_mode.DEFAULT
        input_config=browser_mode.validate({'mode':str(f.get('browser_mode',previous_input['mode'])),
                                            'entry_url':str(f.get('browser_entry_url',previous_input['entry_url'])),
                                            'flow':str(f.get('browser_flow',previous_input.get('flow','')))},
                                           {'domains':domains,'url':url})
        use_global = int(f.get('use_global_schedule') == 'on') if 'schedule_mode' in f or 'use_global_schedule' in f else int(existing.get('use_global_schedule', 0) if existing else 0)
        saved_schedule = existing.get('schedule_config', '{}') if existing else '{}'
        if use_global:
            cron = existing['cron'] if existing else scheduling.to_cron(scheduling.global_schedule())
        elif 'schedule_mode' in f and f['schedule_mode'] != 'custom':
            selected = scheduling.from_form(f); saved_schedule = json.dumps(selected)
            cron = scheduling.to_cron(selected)
        elif f.get('schedule_mode') == 'custom':
            if not existing: raise ValueError('Bitte einen Prüfzeitplan wählen.')
            cron = existing['cron']
        else:
            cron = str(f.get('cron', existing['cron'] if existing else '0 6 * * *')).strip()
            if len(cron.split()) != 5: raise ValueError('Ungültiger bisheriger Zeitplan.')
            next_due(cron)
            if existing and cron != existing['cron']: saved_schedule = '{}'
        due = scheduling.next_project_due({'cron':cron,'use_global_schedule':use_global,'schedule_config':saved_schedule})
        color=normalize_color(f.get('color',existing['color'] if existing else DEFAULT_COLOR))
        if 'tag_ids' in f or 'tags_present' in f: tags = tag_registry.assign(f.getlist('tag_ids'))
        elif 'tags' in f: tags = tag_registry.existing_names(f['tags'])
        else: tags = json.loads(existing['tags']) if existing else []
    except (ValueError,TypeError) as error:raise HTTPException(400,str(error))
    icon=None
    try:
        if f.get('icon_payload'): icon=project_icons.validate(json.loads(str(f['icon_payload'])))
    except (ValueError,TypeError): raise HTTPException(400,'Ungültiges Projekticon. Bitte erneut auswählen.') from None
    stored_tags=json.dumps(tags,ensure_ascii=False)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if existing:
            c.execute('UPDATE projects SET name=?,url=?,prompt=?,domains=?,cron=?,enabled=?,next_due=?,color=?,tags=?,use_global_schedule=?,schedule_config=? WHERE id=?', (name, url, prompt, domains, cron, enabled, due, color, stored_tags, use_global, saved_schedule, pid))
        else:
            c.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,color,tags,use_global_schedule,schedule_config) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (pid, name, url, prompt, domains, cron, enabled, due, now(), color, stored_tags, use_global, saved_schedule))
        if 'telegram_present' in f or not existing:
            c.execute('UPDATE projects SET telegram_enabled=? WHERE id=?',(int(f.get('telegram_enabled')=='on'),pid))
        if not enabled or (existing and existing['url'] != url):
            retry_jobs.cancel_project(pid, connection=c)
        site_safety.save_profile(pid,crawl_profile,c)
        browser_mode.save({'id':pid,'domains':domains,'url':url},input_config,c)
    if f.get('icon_action')=='clear': execute('DELETE FROM state WHERE key=?',(project_icons.KEY+pid,))
    elif icon: project_icons.save(pid,icon)
    elif existing and existing['url']!=url:
        previous_icon=project_icons.get(pid)
        if previous_icon and previous_icon['mode']=='auto': execute('DELETE FROM state WHERE key=?',(project_icons.KEY+pid,))
    response=redirect('/projects/' + pid, 'project_saved')
    if not existing or existing['url']!=url or f.get('icon_action')=='clear': response.background=BackgroundTask(project_icons.refresh,{'id':pid,'url':url})
    return response

@app.get('/projects/{pid}/icon')
async def project_icon(pid: str):
    project(pid)
    icon=project_icons.get(pid)
    if not icon: raise HTTPException(404)
    import base64
    return Response(base64.b64decode(icon['data']),media_type=icon['media'],headers={'Content-Security-Policy':"default-src 'none'; sandbox",'X-Content-Type-Options':'nosniff'})

@app.post('/icons/discover')
async def discover_icon(request: Request):
    data=await request.json()
    try: return await project_icons.discover(str(data.get('url','')))
    except ValueError as exc: raise HTTPException(400,str(exc)) from None

@app.post('/icons/upload')
async def upload_icon(request: Request):
    name=request.headers.get('x-icon-name','').lower()
    if not name.endswith(('.ico','.svg')): raise HTTPException(400,'Bitte eine ICO- oder SVG-Datei auswählen.')
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>project_icons.MAX_ICON: raise HTTPException(413,'Icon-Datei übersteigt 256 KB.')
    try: return project_icons.record(bytes(raw),filename=name)
    except ValueError as exc: raise HTTPException(400,str(exc)) from None

@app.post('/projects/{pid}/delete')
async def delete_project(pid: str, request: Request):
    f = await request.form()
    if f.get('confirm') != 'on': raise HTTPException(400, 'Löschen zuerst ausdrücklich bestätigen.')
    if engine.BUSY.locked(): raise HTTPException(409, 'Das Archiv wird gerade verwendet. Nach Abschluss der Prüfung erneut löschen.')
    try:
        async with engine.BUSY: project_lifecycle.remove(pid)
    except ValueError as error: raise HTTPException(409, str(error))
    return redirect('/', 'project_deleted')

@app.get('/tags')
async def tags_page(request: Request):
    return render(request, 'tags', tags=known_tags())

@app.post('/tags/save')
async def save_tag(request: Request):
    f = await request.form()
    try: tag_registry.save(str(f.get('name','')), str(f.get('color',DEFAULT_COLOR)), str(f.get('id','')))
    except ValueError as error: raise HTTPException(400, str(error))
    return redirect('/tags','tag_saved')

@app.post('/tags/{tid}/delete')
async def delete_tag(tid: str, request: Request):
    f = await request.form()
    if f.get('confirm') != 'on': raise HTTPException(400, 'Entfernen zuerst bestätigen.')
    try: tag_registry.remove(tid)
    except ValueError as error: raise HTTPException(400, str(error))
    return redirect('/tags','tag_deleted')

@app.post('/settings/schedule')
async def save_global_schedule(request: Request):
    f = await request.form()
    try: selected = scheduling.from_form(f)
    except ValueError as error: raise HTTPException(400,str(error))
    previous = CONFIG.read_bytes() if CONFIG.exists() else None
    s = persisted_settings(); s['global_schedule'] = selected
    try:
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            save_json(CONFIG,s)
            if f.get('apply_all') == 'on': c.execute('UPDATE projects SET use_global_schedule=1')
            c.execute('UPDATE projects SET next_due=? WHERE use_global_schedule=1',(scheduling.next_for(selected),))
    except Exception:
        if previous is None: CONFIG.unlink(missing_ok=True)
        else: save_json(CONFIG,json.loads(previous))
        raise
    return redirect('/settings','schedule_saved')

@app.get(PROJECT_DETAIL_ROUTE)
async def detail(pid: str, request: Request):
    p = project(pid)
    p.update(attention.summary(pid))
    p['attention_findings']=finding_entries(p['pending_changes'])
    p['notice_changes']=[change for change in p['pending_changes'] if change['priority']!='attention']
    runs=project_history(pid)
    latest=next((r for r in runs if r.get('finished') and r['status']!='cancelled'),None)
    evidence=run_evidence(latest['id']) if latest else {}
    return render(request,'project',project=p,runs=runs,latest=latest,coverage=evidence.get('coverage',{}))

@app.post('/projects/{pid}/review')
async def review(pid: str, request: Request):
    project(pid)
    f = await request.form()
    through = str(f.get('through', ''))
    try:
        dt = datetime.fromisoformat(through)
        if dt.tzinfo is None or dt > datetime.now(timezone.utc):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Ungültiger Prüfstand; Übersicht neu laden')
    attention.acknowledge(pid, dt.astimezone(timezone.utc).isoformat(), now())
    if f.get('dashboard') == '1':
        if 'application/json' in request.headers.get('accept', ''):
            state = attention.summary(pid)
            return {'id': pid, **{key: state[key] for key in ('attention_count', 'notice_count', 'review_through')}}
        return redirect('/', 'reviewed')
    return redirect('/projects/' + pid, 'reviewed')

@app.post('/projects/{pid}/run')
async def run(pid: str):
    project(pid)
    if manual():
        raise HTTPException(409, 'Zuerst den manuellen Modus beenden')
    rid = enqueue(pid)
    if not rid:
        r = one("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (pid,))
        rid = r['id']
    return redirect('/runs/' + rid)

@app.get('/runs/{rid}')
async def run_detail(rid: str, request: Request):
    r = one('SELECT r.*,p.name FROM runs r JOIN projects p ON p.id=r.project_id WHERE r.id=?', (rid,))
    if not r:
        raise HTTPException(404)
    snapshots = rows('SELECT * FROM snapshots WHERE run_id=? ORDER BY captured', (rid,))
    changes = rows('SELECT * FROM changes WHERE run_id=? ORDER BY detected', (rid,))
    for c in changes:
        c['evidence'] = json.loads(c['evidence'])
        c['priority'] = attention.priority(c['kind'], c['evidence'])
        c['number_changed'] = attention.number_changed(c['evidence'])
        c['previous'] = one('SELECT * FROM snapshots WHERE id=?', (c['previous_id'],)) if c['previous_id'] else None
        c['current'] = one('SELECT * FROM snapshots WHERE id=?', (c['current_id'],))
    evidence=run_evidence(rid)
    findings=finding_entries(changes,r['finished']) if r.get('finished') and r['status'] in ('success','partial') else []
    return render(request, 'run', run=r, snapshots=snapshots, changes=changes, findings=findings, events=rows('SELECT * FROM events WHERE run_id=? ORDER BY id', (rid,)), evidence_available=bool(evidence),coverage=evidence.get('coverage',{}),article_catalog=evidence.get('article_catalog',[]))

@app.get('/artifacts/{path:path}')
async def artifact(path: str, download: bool = False):
    target = (DATA / 'artifacts' / path).resolve()
    if not target.is_relative_to((DATA / 'artifacts').resolve()) or not target.is_file():
        raise HTTPException(404)
    # HTML is delivered as plain text to keep untrusted source scripts inert.
    media = 'image/png' if target.suffix == '.png' else 'application/pdf' if target.suffix == '.pdf' else 'text/plain'
    return FileResponse(target, media_type=media, filename=target.name if download else None, headers={'Content-Security-Policy': "sandbox; default-src 'none'"})

def document_metadata_display(metadata):
    """Format bounded date evidence without inferring missing times or zones."""
    groups = {}
    if not metadata:
        return groups

    def date_value(value):
        if not isinstance(value, dict) or not value.get('raw'):
            return None
        if value.get('status') == 'exact' and value.get('europe_berlin'):
            try:
                parsed = datetime.fromisoformat(value['europe_berlin'])
                return parsed.strftime('%d.%m.%Y · %H:%M:%S') + ' · Europe/Berlin'
            except (ValueError, TypeError):
                pass
        return value['raw'] + ' (kein eindeutiger Zeitpunkt mit Sekunden und Zeitzone)'

    for source, fields in (
            ('pdf', [('creation_date', 'Erstellt · PDF CreationDate'),
                     ('modification_date', 'Geändert · PDF ModDate')]),
            ('http', [('last_modified', 'Serverangabe · HTTP Last-Modified')])):
        rows = []
        for key, label in fields:
            value = date_value(metadata.get(source, {}).get(key))
            if value:
                rows.append((label, value))
        if rows:
            groups[source] = rows
    http = metadata.get('http', {})
    for key, label in (('etag', 'HTTP ETag'), ('content_type', 'HTTP Content-Type'),
                       ('content_length', 'HTTP Content-Length · Bytes')):
        value = http.get(key)
        if value is not None:
            groups.setdefault('http', []).append((label, str(value)))
    return groups


@app.get('/documents/{sid}')
async def document_view(sid: str, request: Request):
    snap = one("SELECT s.*,p.name AS project_name FROM snapshots s JOIN projects p ON p.id=s.project_id WHERE s.id=? AND s.kind='document'", (sid,))
    if not snap:
        raise HTTPException(404)
    try:
        record = read_json(DATA / 'artifacts' / snap['run_id'] / (sid + '.extraction.json'))
    except (OSError, ValueError):
        record = {}
    # The marker belongs to this exact original, rather than to a legacy text
    # extraction with a similar URL. Missing records never imply verified bytes.
    hash_only = (isinstance(record, dict) and record.get('method') == 'hash_only'
                 and record.get('source_sha256') == snap['sha256']
                 and record.get('content_analysis_performed') is False
                 and record.get('original_sha256_verified') is True)
    comparison = record.get('comparison_status') if hash_only else None
    if comparison not in ('baseline', 'unchanged', 'changed'):
        comparison = None
    previous = None
    if hash_only and record.get('previous_snapshot_id'):
        previous = one("SELECT id FROM snapshots WHERE id=? AND project_id=? AND kind='document'",
                       (record['previous_snapshot_id'], snap['project_id']))
    file_info = {'hash_only': hash_only, 'comparison': comparison,
                 'previous_sha256': record.get('previous_sha256') if hash_only else None,
                 'previous_id': previous['id'] if previous else None}
    metadata = compact_document_metadata(record.get('document_metadata')) if hash_only else None
    file_info['metadata'] = document_metadata_display(metadata)
    source = urlsplit(snap['url'])
    file_info['source_url'] = snap['url'] if source.scheme in ('https', 'http') and source.netloc else None
    return render(request, 'document', document=snap, file_info=file_info,
                  from_files=request.query_params.get('from') == 'files')


class DocumentDownloadResponse(StreamingResponse):
    """Release archive read locks even when a client disconnects mid-download."""
    def __init__(self, download):
        self.download = download
        fallback = 'Webmonitor' + Path(download.filename).suffix
        disposition = ('attachment; filename="' + fallback + '"; filename*=UTF-8\'\''
                       + quote(download.filename, safe=''))
        super().__init__(download.iterator(), media_type=download.media_type,
                         headers={'Content-Disposition': disposition},
                         background=BackgroundTask(download.close))

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            self.download.close()


@app.get('/files')
async def global_files(request: Request, q: str = '', sort: str = 'changed_at', direction: str = 'desc'):
    if len(q) > 512:
        raise HTTPException(400, 'Die Suche darf höchstens 512 Zeichen enthalten.')
    try:
        catalog = await asyncio.to_thread(document_files.list_files, q, sort, direction)
    except FileNotFoundError:
        raise HTTPException(503, 'Die Dokumentablage ist vorübergehend nicht verfügbar.') from None
    except (ValueError, TypeError):
        raise HTTPException(400, 'Ungültige Suche oder Sortierung.') from None
    links = {}
    for key in ('tags', 'project', 'type', 'document_name', 'name', 'size', 'changed_at'):
        target_direction = ('asc' if catalog['direction'] == 'desc' else 'desc') if key == catalog['sort'] else ('desc' if key == 'changed_at' else 'asc')
        links[key] = '/files?' + urlencode({'q': q, 'sort': key, 'direction': target_direction})
    links['icon'] = links['type']
    project_ids = {row['project_id'] for row in catalog['files']}
    file_project_icons = {pid: project_icons.image_url(pid, project_icons.get(pid)) for pid in project_ids}
    return render(request, 'files', file_rows=catalog['files'], files_total=catalog['total'],
                  file_project_icons=file_project_icons,
                  files_matched=catalog['matched'], files_query=catalog['query'],
                  files_sort=catalog['sort'], files_direction=catalog['direction'],
                  file_sort_links=links, files_zip_count=catalog['archive_count'],
                  files_unavailable=catalog.get('unavailable', 0))


@app.get('/files/download-all.zip')
async def download_all_files():
    try:
        download = await asyncio.to_thread(document_files.prepare_zip)
    except document_files.ArchiveTooLarge as error:
        raise HTTPException(413, str(error)) from None
    except FileNotFoundError:
        raise HTTPException(404, 'Keine offline gespeicherten Dateien vorhanden.') from None
    except ValueError:
        raise HTTPException(409, 'Das Dokumentenarchiv ist nicht vollständig verfügbar.') from None
    return DocumentDownloadResponse(download)


@app.get('/files/{sid}/download')
async def download_stored_file(sid: str):
    try:
        download = await asyncio.to_thread(document_files.prepare_download, sid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, 'Diese Originaldatei ist nicht mehr offline verfügbar.') from None
    return DocumentDownloadResponse(download)

@app.get('/browser')
async def browser_help(request: Request, macro_project: str | None = None):
    if macro_project:
        return redirect(detail_path(project(macro_project)['id']) + '/macros')
    return render(request, 'browser', macro_projects=rows('SELECT id,name FROM projects ORDER BY name'))


def macro_page(request, p, error=''):
    recording = macro_recorder.active
    return render(request, 'macros', project=p, macro_error=error,
                  macro_state=navigation_macros.get(p), macro_draft=macro_recorder.draft_for(p['id']),
                  recording=recording if recording and recording.project['id'] == p['id'] else None,
                  macro_supported=not (github_repository(p['url']) or engine.dke_standards.matches(p['url'])
                                       or engine.obsidian_changelog.matches(p['url'])))


@app.get('/projects/{pid}/macros')
async def project_macros(pid: str, request: Request):
    return macro_page(request, project(pid))


@app.post('/projects/{pid}/macros')
async def change_project_macros(pid: str, request: Request):
    p = project(pid)
    form = await request.form()
    action = form.get('action')
    try:
        if action == 'start':
            if github_repository(p['url']) or engine.dke_standards.matches(p['url']) or engine.obsidian_changelog.matches(p['url']):
                raise ValueError('Diese Quelle verwendet einen direkten Datenabruf.')
            await macro_recorder.start(p, engine.BROWSER_CDP_URL, engine.BUSY)
        elif action in ('stop', 'cancel'):
            await macro_recorder.stop(pid, engine.BUSY, keep=action == 'stop')
        elif action == 'discard':
            draft = macro_recorder.draft_for(pid)
            if draft and form.get('draft') == draft['id']:
                macro_recorder.draft = None
        else:
            value = navigation_macros.get(p)
            if action == 'save':
                draft = macro_recorder.draft_for(pid)
                if not draft or form.get('draft') != draft['id']:
                    raise ValueError('Die Aufnahme ist nicht mehr verfügbar.')
                optional = set(form.getlist('optional'))
                steps = [{**item, 'optional': str(index) in optional}
                         for index, item in enumerate(draft['steps'])]
                value['macros'].append({**draft, 'name': str(form.get('name', '')), 'steps': steps})
                value['active'] = draft['id']
            elif action == 'select':
                value['active'] = str(form.get('macro', ''))
            elif action == 'delete':
                ident = form.get('macro')
                value['macros'] = [macro for macro in value['macros'] if macro['id'] != ident]
                if value['active'] == ident:
                    value['active'] = ''
            else:
                raise ValueError('Unbekannte Makro-Aktion.')
            navigation_macros.save(p, value)
            if action == 'save':
                macro_recorder.draft = None
    except (ValueError, site_safety.ProtectionDetected) as error:
        return macro_page(request, p, str(error))
    except Exception:
        return macro_page(request, p, 'Brave konnte die Aufnahme nicht öffnen oder abschließen.')
    return redirect(detail_path(pid) + '/macros')

@app.post('/manual')
async def manual_mode(request: Request):
    f = await request.form()
    value = f.get('mode') == 'start'
    if value and engine.BUSY.locked():
        raise HTTPException(409, 'Aktive Prüfung zuerst beenden lassen')
    execute("UPDATE state SET value=? WHERE key='manual'", ('1' if value else '0',))
    return redirect('/browser', 'manual_on' if value else 'manual_off')

@app.get('/settings')
async def config_page(request: Request):
    return await render_settings(request)

async def render_settings(request, history_preview=None):
    return render(request, 'settings', models=read_json(Path('/config/models.json')).get('models', []), capabilities=read_json(Path('/config/capabilities.json')), config_sources=configuration_sources(), history_preview=history_preview)

@app.post('/maintenance/storage')
@app.post('/settings/storage')
async def save_storage(request: Request):
    form = await request.form()
    keys = ('document_versions', 'detail_days', 'cache_days')
    if any(len(form.getlist(key)) != 1 for key in keys):
        raise HTTPException(400, 'Alle Speicherregeln müssen genau einmal angegeben sein.')
    values = {}
    try:
        for key in keys:
            value = str(form[key])
            if not value or not value.isascii() or not value.isdecimal():
                raise ValueError()
            values[key] = int(value)
        selected = retention.validate_policy(values)
    except (ValueError, TypeError):
        raise HTTPException(400, 'Ungültige Speicherregeln.') from None
    config = persisted_settings()
    config['retention'] = selected
    save_json(CONFIG, config)
    return RedirectResponse(request.url.path.rsplit('/', 1)[0] + '?notice=storage_saved#speicher', 303)


async def render_maintenance(request, *, options=None, planned=None, result=None, error=None, status=200):
    try:
        storage = await asyncio.to_thread(maintenance.overview)
    except (ValueError, OSError):
        storage = None
        error = 'Die Speicherübersicht konnte nicht vollständig ermittelt werden.'
        status = 409
    response = render(request, 'maintenance', storage=storage, storage_policy=retention.policy(),
                      options=options, planned=planned, result=result, maintenance_error=error)
    response.status_code = status
    return response


@app.get('/maintenance')
async def maintenance_page(request: Request):
    return await render_maintenance(request)


@app.post('/maintenance/preview')
async def maintenance_preview(request: Request):
    try:
        options = maintenance.selection(await request.form())
    except ValueError as error:
        return await render_maintenance(request, error=str(error), status=400)
    try:
        planned = await asyncio.to_thread(maintenance.preview, options)
    except ValueError as error:
        return await render_maintenance(request, options=options, error=str(error), status=409)
    except OSError:
        return await render_maintenance(request, options=options, error='Die Ablage ist derzeit nicht lesbar.', status=409)
    return await render_maintenance(request, options=options, planned=planned)


@app.post('/maintenance/cleanup')
async def maintenance_cleanup(request: Request):
    form = await request.form()
    try:
        options = maintenance.selection(form)
        if form.get('confirm') != 'on' or (options['kind'] == 'reset' and form.get('reset_confirmation') != 'NEUSTART'):
            raise ValueError('Bestätige die endgültige Bereinigung wie in der Vorschau angegeben.')
    except ValueError as error:
        return await render_maintenance(request, error=str(error), status=400)
    try:
        if engine.BUSY.locked() or engine.REPORT_LOCK.locked() or notifications.LOCK.locked():
            raise ValueError('Laufende Prüfungen, Berichte und den Telegram-Versand zuerst abschließen lassen.')
        async with engine.BUSY, engine.REPORT_LOCK, notifications.LOCK:
            result = await asyncio.to_thread(maintenance.clean, options, form.get('fingerprint'))
    except ValueError as error:
        return await render_maintenance(request, options=options, error=str(error), status=409)
    except OSError:
        return await render_maintenance(request, options=options, error='Die Ablage ist derzeit nicht zugänglich.', status=409)
    return await render_maintenance(request, result=result)

@app.post('/settings/storage/cleanup')
async def cleanup_storage():
    if engine.BUSY.locked():
        raise HTTPException(409, 'Speicherbereinigung nach Abschluss der laufenden Prüfung starten.')
    async with engine.BUSY:
        result = await asyncio.to_thread(retention.cleanup)
    if result.get('status') == 'skipped':
        raise HTTPException(409, 'Speicherbereinigung derzeit nicht möglich. Laufende Prüfungen zuerst abschließen lassen.')
    return RedirectResponse('/settings?notice=storage_cleaned#speicher', 303)

@app.post('/settings/history/preview')
async def preview_history(request: Request):
    form = await request.form()
    try:
        mode, amount = history_cleanup.rule(form.get('mode'), form.get('amount'))
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    try:
        result = await asyncio.to_thread(history_cleanup.preview, mode, amount)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return await render_settings(request, history_preview=result)

@app.post('/settings/history/cleanup')
async def cleanup_history(request: Request):
    form = await request.form()
    if form.get('confirm') != 'on':
        raise HTTPException(400, 'Löschen zuerst ausdrücklich bestätigen.')
    try:
        mode, amount = history_cleanup.rule(form.get('mode'), form.get('amount'))
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    if engine.BUSY.locked() or engine.REPORT_LOCK.locked() or notifications.LOCK.locked():
        raise HTTPException(409, 'Laufende Prüfungen, Berichte und Telegram-Versand zuerst abschließen lassen.')
    try:
        async with engine.BUSY, engine.REPORT_LOCK, notifications.LOCK:
            result = await asyncio.to_thread(history_cleanup.clean, mode, amount, form.get('fingerprint'))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    notice = 'history_files_pending' if result['pending'] else 'history_cleaned'
    return RedirectResponse('/settings?notice='+notice+'#pruefverlauf', 303)

@app.post('/settings/omlx')
async def save_omlx(request: Request):
    f = await request.form()
    s, secret = persisted_settings(), persisted_credentials()
    sources = configuration_sources()
    try:
        for field in ('omlx_url', 'agent_model', 'vision_model', 'analysis_model'):
            if not sources[field]['readonly']:
                value = str(f.get(field, '')).strip()
                s[field] = (configuration.validate_url if field == 'omlx_url'
                            else configuration.validate_model)(value, field)
        if not sources['omlx_key']['readonly']:
            key = str(f.get('omlx_key', '')).strip()
            if key:
                secret['omlx_key'] = configuration.validate_api_key(key)
            if f.get('clear_key') == 'on':
                secret.pop('omlx_key', None)
    except configuration.ConfigurationError as error:
        raise HTTPException(400, str(error)) from None
    save_json(CONFIG, s); save_json(SECRETS, secret)
    return redirect('/settings', 'ki_saved')

@app.post('/settings/models')
async def models():
    try:
        models = await engine.discover_models()
        save_json(Path('/config/models.json'), {'models': models, 'fetched': now()})
        if len(models) == 1:
            s = persisted_settings()
            sources = configuration_sources()
            for k in ('agent_model', 'vision_model', 'analysis_model'):
                if sources[k]['readonly']:
                    continue
                if not s.get(k):
                    s[k] = configuration.validate_model(models[0]['id'], k)
            save_json(CONFIG, s)
    except Exception as e:
        save_json(Path('/config/capabilities.json'), {'error': 'Modellabruf fehlgeschlagen: ' + type(e).__name__})
    return redirect('/settings')

@app.post('/settings/test')
async def test_models():
    await engine.capability_test()
    return redirect('/settings', 'test_done')

@app.post('/settings/smtp')
async def save_smtp(request: Request):
    return await save_communication(request,'smtp')


@app.get('/smtp')
async def smtp_page(request:Request):
    return render(request,'smtp',config_sources=configuration_sources(),test_result=communication_test_result('smtp'))


@app.get('/telegram')
async def telegram_page(request:Request):
    history=rows('''SELECT n.id,n.status,n.created,n.sent_at,n.error,p.name,p.id AS project_id
        FROM notifications n JOIN projects p ON p.id=n.project_id ORDER BY n.created DESC LIMIT 30''')
    return render(request,'telegram',config_sources=configuration_sources(),test_result=communication_test_result('telegram'),deliveries=history)


def communication_test_result(channel):
    row=one('SELECT value FROM state WHERE key=?',('communication_test:'+channel,))
    return json.loads(row['value']) if row else None


async def save_communication(request,channel):
    form=await request.form();s=persisted_settings();secret=persisted_credentials()
    fields=tuple(k for k in communication_config.FIELDS if k.startswith(channel+'_'))
    private='smtp_password' if channel=='smtp' else 'telegram_token'
    try:
        for field in fields:
            default={'smtp_port':'587','smtp_tls':'starttls','telegram_mode':'off'}.get(field,'')
            value=form.get(field,default)
            if field=='smtp_enabled':value=form.get(field)=='on'
            s[field]=communication_config.validate(field,str(value).strip() if isinstance(value,str) else value)
        if form.get(private):secret[private]=communication_config.validate(private,str(form[private]))
        if form.get('clear_secret')=='on':secret.pop(private,None)
    except configuration.ConfigurationError as exc:raise HTTPException(400,str(exc)) from None
    save_json(CONFIG,s);save_json(SECRETS,secret)
    return redirect('/'+channel,'smtp_saved' if channel=='smtp' else 'telegram_saved')


@app.post('/telegram/save')
async def save_telegram(request:Request):
    return await save_communication(request,'telegram')


@app.post('/settings/telegram')
async def save_global_telegram(request:Request):
    form=await request.form();s=persisted_settings()
    try:s['telegram_mode']=communication_config.validate('telegram_mode',str(form.get('telegram_mode','off')))
    except configuration.ConfigurationError as exc:raise HTTPException(400,str(exc)) from None
    save_json(CONFIG,s)
    return redirect('/settings','telegram_saved')


@app.post('/smtp/test')
@app.post('/telegram/test')
async def test_communication(request:Request):
    channel=request.url.path.split('/')[1]
    form=await request.form();action=str(form.get('action','connection'))
    if action not in ('connection','message'):raise HTTPException(400,'Ungültiger Verbindungstest.')
    try:
        async with httpx.AsyncClient(timeout=50,trust_env=False) as client:
            response=await client.post('http://gateway:8000/'+channel+'/test',json={'action':action})
            result=response.json()
        # The gateway returns sanitized messages only; never persist upstream responses.
        result={'ok':result.get('ok') is True,'message':str(result.get('message') or result.get('error') or 'Verbindungstest fehlgeschlagen.')[:500]}
    except Exception:result={'ok':False,'message':'Kommunikationsdienst nicht erreichbar.'}
    result.update(time=now(),action=action)
    execute('INSERT OR REPLACE INTO state VALUES(?,?)',('communication_test:'+channel,json.dumps(result)))
    return redirect('/'+channel)

def distribution_form(request,value=None,error=''):
    value=value or {'id':'','alias':'','emails':[],'project_ids':[],'tag_ids':[],'enabled':1}
    projects=rows('SELECT id,name,tags FROM projects ORDER BY name')
    for p in projects:p['tag_chips']=tag_registry.decorate(p)
    return render(request,'distribution_edit',distribution=value,distribution_error=error,distribution_projects=projects,distribution_tags=tag_registry.all_tags())

@app.get('/distributions')
async def distribution_index(request: Request):
    items=distributions.all_lists()
    for item in items:item['project_count']=len(distributions.allowed_projects(item))
    return render(request,'distributions',distributions=items)

@app.get('/distributions/new')
async def distribution_new(request: Request):return distribution_form(request)

@app.get('/distributions/{did}/edit')
async def distribution_edit(did: str,request: Request):
    item=distributions.get(did)
    if not item or item['deleted_at']:raise HTTPException(404)
    return distribution_form(request,item)

@app.post('/distributions/save')
async def distribution_save(request: Request):
    form=await request.form();did=str(form.get('id',''))
    value={'id':did,'alias':str(form.get('alias','')),'emails':str(form.get('emails','')),
        'project_ids':form.getlist('project_ids'),'tag_ids':form.getlist('tag_ids'),'enabled':int(form.get('enabled')=='on')}
    try:distributions.save(value,did)
    except ValueError as exc:
        response=distribution_form(request,value,str(exc));response.status_code=400;return response
    return redirect('/distributions',notice='distribution_saved')

@app.post('/distributions/{did}/toggle')
async def distribution_toggle(did: str):
    try:distributions.toggle(did)
    except ValueError:raise HTTPException(404)
    return redirect('/distributions')

@app.post('/distributions/{did}/delete')
async def distribution_delete(did: str,request: Request):
    form=await request.form()
    if form.get('confirm')!='on':raise HTTPException(400,'Löschen bitte bestätigen.')
    distributions.remove(did)
    return redirect('/distributions',notice='distribution_deleted')

@app.post('/distributions/{did}/preview')
async def distribution_preview(did: str):
    item=distributions.get(did)
    if not item or item['deleted_at']:raise HTTPException(404)
    parent=await engine.make_report(test=True,distribution_id=did)
    child=one('SELECT id FROM reports WHERE parent_id=? AND distribution_id=? ORDER BY recipient_email LIMIT 1',(parent,did))
    return redirect('/reports/'+(child['id'] if child else parent))

@app.get('/reports')
async def reports(request: Request):
    scope=request.query_params.get('scope','all');did=request.query_params.get('distribution','')
    regular_only = request.query_params.get('regular') == '1'
    try:page=max(1,int(request.query_params.get('page','1')))
    except ValueError:page=1
    where=['mail_status<>?'];args=[maintenance.PURGED]
    if scope in ('admin','distribution'):where.append('audience=?');args.append(scope)
    if did:where.append('distribution_id=?');args.append(did)
    if regular_only:where.append("period_key NOT LIKE 'test-%'")
    clause=' WHERE '+' AND '.join(where) if where else ''
    total=one('SELECT count(*) AS n FROM reports'+clause,args)['n'];page=min(page,max(1,(total+49)//50))
    records=rows('SELECT * FROM reports'+clause+' ORDER BY created DESC,audience,distribution_alias,recipient_email,id DESC LIMIT 50 OFFSET ?',(*args,(page-1)*50))
    return render(request,'reports',reports=records,report_scope=scope,report_distribution=did,report_regular_only=regular_only,report_page=page,report_pages=max(1,(total+49)//50),report_distributions=distributions.all_lists(True),mail_labels=report_mail.LABELS)

@app.post('/reports/test')
async def report_test():
    rid=await engine.make_report(test=True)
    return redirect('/reports/'+rid)

def get_report(rid):
    r=one('SELECT * FROM reports WHERE id=? AND mail_status<>?',(rid,maintenance.PURGED))
    if not r:raise HTTPException(404)
    return r

@app.get('/reports/{rid}/html')
def report_html(rid: str):
    with retention.maintenance_lock(exclusive=False):
        html = email_reports.preview_html(get_report(rid))
    return HTMLResponse(html,headers={
        'X-Frame-Options':'SAMEORIGIN',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; frame-ancestors 'self'; form-action 'none'; base-uri 'none'"})

@app.get('/reports/{rid}/assets/{cid}')
def report_asset(rid: str,cid: str):
    with retention.maintenance_lock(exclusive=False):
        r=get_report(rid)
        images=report_images.validate(email_reports.saved_images(r),email_reports.saved_html(r))
    if cid not in images:raise HTTPException(404)
    return Response(images[cid],media_type='image/png')

@app.get('/reports/{rid}/message.eml')
def report_eml(rid: str):
    with retention.maintenance_lock(exclusive=False):
        r=get_report(rid)
        text,html=email_reports.message_parts(r)
        message=mail_message.build(text,report_mail.subject(r),
            'webmonitor@example.test',r.get('recipient_email') or 'preview@example.test',html,email_reports.saved_images(r))
    return Response(message.as_bytes(),media_type='message/rfc822',headers={
        'Content-Disposition':'attachment; filename="KI-Webmonitor-Wochenbericht-'+rid+'.eml"'})

@app.get('/reports/{rid}')
async def report_detail(rid: str, request: Request):
    r=get_report(rid)
    try:selection=json.loads(r['selection_snapshot'])
    except (ValueError,TypeError):selection={}
    return render(request,'report',selection_info=selection,report=r,mail_labels=report_mail.LABELS,mail_events=rows('SELECT * FROM report_mail_events WHERE report_id=? ORDER BY created,id',(rid,)),child_reports=rows('SELECT * FROM reports WHERE parent_id=? ORDER BY created,distribution_alias,recipient_email',(rid,)))

@app.post('/reports/{rid}/retry')
async def report_retry(rid: str,request: Request):
    form=await request.form()
    try:report_mail.retry(rid,form.get('confirm_duplicate')=='on')
    except ValueError as exc:raise HTTPException(400,str(exc))
    return redirect('/reports/'+rid,notice='report_retry')

def transfer_error(request,message,status=400):
    response=render(request,'transfer',transfer_error=message)
    response.status_code=status
    return response

@app.get('/transfer')
async def transfer_page(request: Request):
    transfer.workspace()
    return render(request,'transfer')

@app.post('/transfer/export')
async def transfer_export(request: Request):
    f=await request.form()
    if engine.BUSY.locked() or one("SELECT id FROM runs WHERE status IN ('running','queued')"):
        return transfer_error(request,'Erst laufende oder wartende Prüfungen beenden lassen.',409)
    folder=Path(tempfile.mkdtemp(prefix='export-',dir=transfer.workspace()))
    extended=f.get('kind')=='archive'
    target=folder/('webmonitor.zip' if extended else 'webmonitor.json')
    try:
        async with engine.BUSY:
            transfer.export_to(target,history=extended,include_secrets=f.get('include_secrets')=='on')
        stamp=datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
        return FileResponse(target,media_type='application/zip' if extended else 'application/json',filename=f'Webmonitor-{stamp}'+('.zip' if extended else '.json'),background=BackgroundTask(shutil.rmtree,folder))
    except (transfer.Invalid,OSError,ValueError) as error:
        shutil.rmtree(folder,ignore_errors=True)
        if isinstance(error, PermissionError):
            return transfer_error(request,'Export wegen fehlender Dateirechte abgebrochen. Eigentümer und Rechte für den App-Benutzer (UID 1000) prüfen.')
        return transfer_error(request,str(error) if isinstance(error,transfer.Invalid) else 'Export fehlgeschlagen. Freien Speicher und Archivdateien prüfen.')

@app.post('/transfer/preview')
async def transfer_preview(request: Request):
    f=await request.form(max_files=1,max_fields=10,max_part_size=65536)
    if not hmac.compare_digest(str(f.get('csrf','')),request.state.csrf):
        await f.close()
        raise HTTPException(403,'CSRF-Prüfung fehlgeschlagen')
    upload=f.get('file')
    if not upload or not hasattr(upload,'read'):
        return transfer_error(request,'JSON- oder ZIP-Datei auswählen.')
    token,folder=transfer.new_upload(request.state.csrf)
    try:
        path=folder/'package';total=0
        with path.open('xb') as out:
            os.chmod(path,0o600)
            while chunk:=await upload.read(1024**2):
                total+=len(chunk)
                if total>transfer.MAX_UPLOAD:raise transfer.Invalid('Datei übersteigt 512 MB.')
                out.write(chunk)
        payload=transfer.read_package(path)
        with db() as c:transfer.preflight(c,payload)
        return render(request,'transfer',import_preview=transfer.preview(payload),import_token=token)
    except (transfer.Invalid,ValueError,TypeError,KeyError,OSError) as error:
        shutil.rmtree(folder,ignore_errors=True)
        return transfer_error(request,str(error) if isinstance(error,transfer.Invalid) else 'Datei ist ungültig oder kann nicht gelesen werden.')
    finally:
        await upload.close()

@app.post('/transfer/import')
async def transfer_import(request: Request):
    f=await request.form()
    if f.get('confirm')!='on':return transfer_error(request,'Import in der Vorschau bestätigen.')
    if engine.BUSY.locked():return transfer_error(request,'Erst laufende Prüfungen beenden lassen.',409)
    try:
        folder=transfer.pending(str(f.get('token','')),request.state.csrf)
        payload=transfer.read_package(folder/'package')
        async with engine.BUSY:
            result=transfer.import_package(folder/'package',payload,apply_settings=f.get('apply_settings')=='on',apply_secrets=f.get('apply_secrets')=='on',activate=f.get('activate')=='on')
        shutil.rmtree(folder)
        return render(request,'transfer',import_result=result)
    except (transfer.Invalid,ValueError,TypeError,KeyError,OSError,sqlite3.Error) as error:
        return transfer_error(request,str(error) if isinstance(error,transfer.Invalid) else 'Import fehlgeschlagen und zurückgerollt. Vorhandene Daten bleiben erhalten.')
