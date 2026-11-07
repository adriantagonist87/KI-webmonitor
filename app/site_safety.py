"""Respect provider protection pages; never solve or bypass their challenges."""
import asyncio
import contextvars
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import re
import time
from urllib.parse import urlsplit

import store

PACE_KEY = 'crawl_pace:'
PROFILES = {'standard': 1.0, 'gentle': 5.0}
MESSAGES = {
    'security_check': 'Bot-Schutz erkannt. Keine zusätzliche Wiederholung; nächster Versuch im regulären Prüfzyklus.',
    'captcha': 'CAPTCHA erkannt. Keine zusätzliche Wiederholung; nächster Versuch im regulären Prüfzyklus.',
    'rate_limit': 'Zugriffsbegrenzung des Anbieters. Keine zusätzliche Wiederholung; nächster Versuch im regulären Prüfzyklus.',
}
_CURRENT = contextvars.ContextVar('site_safety', default=None)
_CHALLENGE_PATH = re.compile(r'(?i)(?:^|/)(?:myracloud-captcha|captcha|challenge-platform)(?:/|$)')


class ProtectionDetected(RuntimeError):
    def __init__(self, kind='security_check', not_before=None):
        self.kind = kind if kind in MESSAGES else 'security_check'
        self.not_before = not_before
        super().__init__(MESSAGES[self.kind])


def host(url):
    try:
        value = (urlsplit(url).hostname or '').lower().rstrip('.')
        return value[4:] if value.startswith('www.') else value
    except ValueError:
        return ''


def profile(project_id, connection=None):
    row = (connection.execute('SELECT value FROM state WHERE key=?',(PACE_KEY+project_id,)).fetchone()
           if connection else store.one('SELECT value FROM state WHERE key=?',(PACE_KEY+project_id,)))
    value = row['value'] if row else 'gentle'
    return value if value in PROFILES else 'gentle'


def save_profile(project_id, value, connection=None):
    if value not in PROFILES:
        raise ValueError('Bitte ein gültiges Prüftempo wählen.')
    if connection:
        connection.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(PACE_KEY+project_id,value))
    else:
        store.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(PACE_KEY+project_id,value))


def result(error):
    value = {'source':'controller_site_safety_v1','kind':error.kind,'retry_suppressed':True}
    if error.not_before:
        value['not_before'] = error.not_before
    return json.dumps({'provider_protection':value})


def protected_run(run):
    try:
        value = json.loads(run['agent_result'] or '{}').get('provider_protection')
        if (isinstance(value,dict) and value.get('source')=='controller_site_safety_v1'
                and value.get('retry_suppressed') is True and value.get('kind') in MESSAGES
                and run['error']==MESSAGES[value['kind']]):
            return value
    except (ValueError,TypeError,KeyError,AttributeError):
        pass
    return None


def latest_protection(project_id):
    run=store.one("SELECT error,agent_result FROM runs WHERE project_id=? AND status IN ('success','failed','partial') ORDER BY started DESC,rowid DESC LIMIT 1",(project_id,))
    return protected_run(run) if run else None


def respect_wait(project_id):
    previous=latest_protection(project_id)
    if previous and previous.get('not_before'):
        try:
            if datetime.fromisoformat(previous['not_before'])>datetime.now(timezone.utc):
                raise ProtectionDetected('rate_limit',previous['not_before'])
        except (ValueError,TypeError):
            pass


def classify(url='', status=200, *, title='', text='', visible_captcha=False, article=False):
    if status == 429:
        return 'rate_limit'
    if _CHALLENGE_PATH.search(urlsplit(url).path):
        return 'captcha'
    if visible_captcha:
        return 'captcha'
    if article:
        return None
    heading = ' '.join(title.casefold().split())
    content = ' '.join(text.casefold().split())
    request = any(phrase in content for phrase in (
        'wenn sie kein roboter sind', 'verify that you are not a bot',
        'checks if it is really you sending the requests and not a bot',
        'überprüft, ob die anfragen wirklich von ihnen', 'verify you are human'))
    unusual = any(phrase in content for phrase in ('ungewöhnlichen datenverkehr','unusual traffic'))
    if request and (unusual or any(word in heading for word in ('security','sicherheit','captcha','just a moment'))):
        return 'security_check'
    if heading in ('captcha','human verification','verify you are human') and request:
        return 'captcha'
    return None


def retry_after(value, *, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        if isinstance(value,str) and re.fullmatch(r'[0-9]{1,8}',value.strip()):
            until = now + timedelta(seconds=int(value))
        else:
            until = parsedate_to_datetime(value or '')
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        return max(now,until).isoformat()
    except (ValueError,TypeError,OverflowError,AttributeError):
        return (now+timedelta(hours=1)).isoformat()


class Guard:
    def __init__(self, project_id, origin, seconds, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.project_id, self.host, self.seconds = project_id, host(origin), seconds
        self.clock, self.sleep, self.next_at = clock, sleep, 0
        self.pending = None
        self.lock = asyncio.Lock()

    def raise_pending(self):
        if self.pending:
            raise self.pending

    async def wait(self):
        self.raise_pending()
        async with self.lock:
            remaining = self.next_at-self.clock()
            if remaining > 0:
                await self.sleep(remaining)
            self.raise_pending()
            self.next_at = self.clock()+self.seconds

    def note_response(self, response):
        try:
            if (host(response.url)==self.host and response.status==429 and
                    response.request.resource_type in ('document','xhr','fetch')):
                self.pending = ProtectionDetected('rate_limit',retry_after(response.headers.get('retry-after')))
        except (AttributeError,TypeError,ValueError):
            pass


def bind(project):
    return _CURRENT.set(Guard(project['id'],project['url'],PROFILES[profile(project['id'])]))


def reset(token):
    _CURRENT.reset(token)


def current():
    return _CURRENT.get()


async def wait():
    if current():
        await current().wait()


def raise_http(url, status, headers=None, html=''):
    facts={}
    if html:
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(html,'html.parser')
        heading=soup.find(['h1','title'])
        facts={'text':soup.get_text(' ',strip=True),'title':heading.get_text(' ',strip=True) if heading else '',
               'article':bool(soup.find('article'))}
    kind = classify(url,status,**facts)
    if status==503 and (headers or {}).get('retry-after'):
        kind='rate_limit'
    if not kind:
        return
    error = ProtectionDetected(kind,retry_after((headers or {}).get('retry-after')) if kind=='rate_limit' else None)
    if current():
        current().pending = error
    raise error


async def check_page(page, response=None):
    if current():
        current().raise_pending()
    url = str(page.url)
    status = response.status if response is not None else 200
    headers = response.headers if response is not None else {}
    raise_http(url,status,headers)
    media = headers.get('content-type','').lower()
    if media and 'html' not in media and 'xhtml' not in media:
        return
    facts = await page.evaluate(r'''() => {
      if (!/html/i.test(document.contentType) || document.querySelector('embed[type="application/pdf"],object[type="application/pdf"],.pdfViewer')) return null;
      const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
      const markers=[...document.querySelectorAll('input[name*="captcha" i],iframe[src*="captcha" i],.g-recaptcha,.h-captcha')];
      const text=(document.body?.innerText||'').slice(0,8000);
      const title=(document.title+' '+(document.querySelector('h1')?.innerText||'')).trim();
      const prominent=/captcha|verification|sicherheitsprüfung|security check/i.test(title);
      const auth=[...document.querySelectorAll('input[type="password"]')].some(visible);
      const request=text.length<3000 && /verify.*human|kein.*roboter|not a bot/i.test(text);
      return {title,text,article:!!document.querySelector('article,[itemtype*="Article"]'),
        visible_captcha:markers.some(e=>visible(e)&&(e.tagName==='INPUT'||prominent||auth||request))};
    }''')
    if isinstance(facts,dict) and 'text' in facts and 'visible_captcha' in facts:
        kind = classify(url,status,**facts)
        if kind:
            error = ProtectionDetected(kind)
            if current():
                current().pending = error
            raise error


async def goto(page, url, **options):
    await wait()
    raise_http(url,200)
    try:
        response = await page.goto(url,**options)
    except Exception:
        if current():
            current().raise_pending()
        # Some navigation failures leave a provider's security page rendered.
        # Inspect that existing page once; do not issue another request or login.
        try:
            await check_page(page)
        except ProtectionDetected:
            raise
        except Exception:
            pass
        raise
    await check_page(page,response)
    return response
