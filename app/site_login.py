"""Controller-only subscription login. Never exposes a form or secret to the LLM."""
import asyncio
import hmac
import re
import time
import json
import site_safety
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, parse_qs


class LoginError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoginPolicy:
    # Legacy policy is application-owned; generic metadata is validated admin configuration.
    origin: str = 'https://www.elektro.net'
    login_path: str = '/user/login'
    form_selector: str = 'form[action="/user/login"]'
    consent_origin: str = 'https://consent.elektro.net'
    close_selector: str = 'button#close-modal[data-dismiss="modal"][aria-label="Close"]'
    logout_paths: tuple = ('/user/logout','/logout','/logout/')


ELEKTRO = LoginPolicy()
LOGIN_FIELDS = {'login[csrfToken]','login[userIdentifier]','login[password]','login[rememberMe]'}
UNSAFE_PATH = re.compile(r'(?i)(?:^|[/_?&=.-])(logout|signout|delete|remove|unsubscribe|checkout|purchase|payment|admin|settings|account|preferences|terminate)(?:$|[/_?&=.-])')


class GenericFormLogin:
    """Admin-configured, exact-form login; credentials never enter agent tooling."""
    def __init__(self,context,project_url,secret,config,log=lambda message:None,policy=None):
        if policy is None:
            from site_access import validate_public_config
            config=validate_public_config(project_url,config)
        elif (config.get('origin')!=policy.origin or urlsplit(config['login_url']).path!=policy.login_path):
            raise LoginError('Die Test-Anmelderichtlinie stimmt nicht mit der Konfiguration überein.')
        self.config=dict(config);self.context=context;self.target=project_url;self.secret=secret;self.log=log
        self.policy=policy or LoginPolicy(origin=config['origin'],login_path=urlsplit(config['login_url']).path,
            form_selector=config['form_selector'],consent_origin=config['origin'],close_selector=config.get('close_selector',''))
        self.page=None;self.attempts=0;self.login_until=0;self.expected=None;self.proved=False;self.last_status=None

    def _origin(self,url):
        return SubscriptionLogin._origin(self,url)

    async def permits(self,request):
        if request.method!='POST' or not self.page or not self.expected or not self.secret:return False
        try:
            if self.login_until<=time.monotonic() or request.frame.page!=self.page or request.frame!=self.page.main_frame:return False
            if request.url!=self.config['submit_url'] or self._origin(request.url)!=self.policy.origin:return False
            if not request.is_navigation_request() and request.resource_type not in ('xhr','fetch'):return False
            if not request.headers.get('content-type','').lower().startswith('application/x-www-form-urlencoded'):return False
            raw=request.post_data or ''
            if len(raw)>65536:return False
            values=parse_qs(raw,keep_blank_values=True,max_num_fields=40)
            if set(values)!=set(self.expected):return False
            encoded=lambda value:json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()
            if not hmac.compare_digest(encoded(values),encoded(self.expected)):return False
            self.login_until=0;self.expected=None
            return True
        except Exception:return False

    async def guard(self,route):
        r=route.request
        if await self.permits(r):return await route.continue_()
        u=urlsplit(r.url)
        login_get=r.method in ('GET','HEAD') and self._origin(r.url)==self.policy.origin and u.path==self.policy.login_path and not u.query
        if r.method not in ('GET','HEAD','OPTIONS') or UNSAFE_PATH.search(u.path) and not login_get:return await route.abort('blockedbyclient')
        if r.is_navigation_request() and self._origin(r.url)!=self.policy.origin:return await route.abort('blockedbyclient')
        return await route.continue_()

    async def dismiss(self,page):
        if self._origin(page.url)!=self.policy.origin:return
        for key,label,containers in (
            ('consent_selector','Cookie-Hinweis bestätigt.','[role=dialog],.cookie-banner,[class*=cookie],[id*=cookie],[class*=consent],[id*=consent]'),
            ('close_selector','Werbehinweis geschlossen.','dialog[open],[role=dialog],.modal,.advertisement')):
            selector=self.config.get(key)
            if not selector:continue
            for _ in range(3):
                visible=[e for e in await page.locator(selector).all() if await e.is_visible()]
                if not visible:break
                if len(visible)!=1:raise LoginError('Ein Anmeldehinweis ist nicht eindeutig konfiguriert.')
                button=visible[0]
                safe=await button.evaluate('(e,containers)=>e.tagName==="BUTTON"&&!e.form&&e.type==="button"&&!!e.closest(containers)',containers)
                if not safe:raise LoginError('Der konfigurierte Hinweis ist kein freigegebener Schließen-/Cookieknopf.')
                await button.click(timeout=4000);await page.wait_for_timeout(150);self.log(label)

    async def _logged_in(self,page):
        if self._origin(page.url)!=self.policy.origin:return False
        if any([await e.is_visible() for e in await page.locator(self.config['form_selector']).all()]):return False
        if await page.locator('input[type=password]:visible,input[autocomplete=one-time-code]:visible').count():return False
        visible=[e for e in await page.locator(self.config['success_selector']).all() if await e.is_visible()]
        return len(visible)==1

    async def _wait_logged_in(self,page):
        for _ in range(20):
            try:
                if await self._logged_in(page):return True
                if await page.locator('input[type=password]:visible').count():return False
            except Exception:pass  # An ongoing same-origin handoff can replace the execution context.
            await page.wait_for_timeout(250)
        return False

    async def ensure(self,force=False):
        if self.proved and not force:return self.last_status
        if self._origin(self.target)!=self.policy.origin:raise LoginError('Anmeldeziel stimmt nicht mit der Projektadresse überein.')
        if not self.secret or self.secret.get('provider')!='generic_form' or self.secret.get('origin')!=self.policy.origin:
            raise LoginError('Für die automatische Anmeldung fehlen passende gespeicherte Webseiten-Zugangsdaten.')
        self.page=await self.context.new_page()
        try:
            try:response=await site_safety.goto(self.page,self.target,wait_until='domcontentloaded',timeout=15000)
            except site_safety.ProtectionDetected:raise
            except Exception:response=None
            await self.page.wait_for_timeout(500);await self.dismiss(self.page)
            if response and response.status<400 and await self._wait_logged_in(self.page):
                self.proved=True;self.last_status='reused';self.log('Webseiten-Sitzung geprüft und wiederverwendet.');return self.last_status
            if self.page.url!=self.config['login_url'] or response is None or response.status>=400:
                response=await site_safety.goto(self.page,self.config['login_url'],wait_until='domcontentloaded',timeout=45000)
                if response and response.status>=400:raise LoginError('Die konfigurierte Anmeldeseite ist nicht erreichbar.')
                await self.page.wait_for_timeout(400);await self.dismiss(self.page)
            if self.attempts>=1:raise LoginError('Automatische Anmeldung bereits versucht; Zugangsdaten und Formularauswahl prüfen.')
            if await self.page.locator('input[autocomplete=one-time-code],iframe[src*=captcha],.g-recaptcha,.h-captcha').count():
                raise LoginError('Anmeldung benötigt eine interaktive Sicherheitsbestätigung.')
            form=self.page.locator(self.config['form_selector'])
            if await form.count()!=1 or not await form.is_visible():raise LoginError('Das Anmeldeformular ist nicht eindeutig. Formularauswahl in den Webseiten-Zugangseinstellungen prüfen.')
            username=form.locator(self.config['username_selector']);password=form.locator(self.config['password_selector']);submit=form.locator(self.config['submit_selector'])
            if await username.count()!=1 or await password.count()!=1 or await submit.count()!=1:
                raise LoginError('Anmeldefelder oder Anmeldeknopf sind nicht eindeutig. Erweiterte Formularauswahl prüfen.')
            if not await username.is_visible() or not await password.is_visible() or not await submit.is_visible():raise LoginError('Konfigurierte Anmeldefelder sind nicht sichtbar.')
            spec=await form.evaluate('''e=>({action:e.action,method:e.method,extra:[...e.elements].filter(x=>!['hidden','submit','button','checkbox','radio'].includes(x.type)).map(x=>({tag:x.tagName,name:x.name,type:x.type}))})''')
            if spec['action']!=self.config['submit_url'] or spec['method'].lower()!='post':raise LoginError('Das Formularziel oder Verfahren stimmt nicht mit der freigegebenen Anmeldung überein.')
            names=[]
            for field in (username,password):
                info=await field.evaluate('e=>({tag:e.tagName,type:e.type,name:e.name})')
                if info['tag']!='INPUT' or info['type'] not in ('text','email','password','tel','number') or not info['name'] or len(info['name'])>100:
                    raise LoginError('Konfigurierte Zugangsfelder sind keine benannten Eingabefelder.')
                names.append(info['name'])
            if names[0]==names[1] or len(spec['extra'])!=2 or {x['name'] for x in spec['extra']}!=set(names):
                raise LoginError('Das Formular enthält zusätzliche Eingabefelder; mehrstufige oder administrative Formulare werden nicht abgesendet.')
            submit_info=await submit.evaluate('e=>({tag:e.tagName,type:e.type,form:!!e.form})')
            if submit_info['tag'] not in ('BUTTON','INPUT') or submit_info['type']!='submit' or not submit_info['form']:raise LoginError('Der konfigurierte Anmeldeknopf ist kein Formular-Submit.')
            await username.fill(self.secret['username'],timeout=4000);await self.dismiss(self.page)
            await password.fill(self.secret['password'],timeout=4000);await self.dismiss(self.page)
            entries=await submit.evaluate('e=>[...new FormData(e.form,e)].map(([k,v])=>[k,typeof v==="string"?v:null])')
            expected={}
            for key,value in entries:
                if not key or len(key)>100 or not isinstance(value,str) or len(value)>8192:raise LoginError('Das Anmeldeformular enthält nicht unterstützte Felder.')
                expected.setdefault(key,[]).append(value)
            if len(entries)>40 or expected.get(names[0])!=[self.secret['username']] or expected.get(names[1])!=[self.secret['password']]:
                raise LoginError('Die Zugangsfelder konnten nicht sicher dem Formular zugeordnet werden.')
            await site_safety.wait()
            self.attempts+=1;self.expected=expected;self.login_until=time.monotonic()+10
            try:
                # DOMContentLoaded alone is already satisfied for an AJAX form.
                # Observe the exact authorized POST before leaving its page.
                async with self.page.expect_response(lambda response: response.url==self.config['submit_url'] and response.request.method=='POST',timeout=20000) as pending:
                    try:await submit.click(timeout=10000)
                    except Exception:pass  # A blocked post-login redirect may end the click after its POST response.
                response=await pending.value
                if response.status>=400:raise LoginError('Der Server hat die Anmeldung nicht bestätigt.')
                # Redirect responses have no authentication body to consume.
                # Their headers (including Set-Cookie) are already observed;
                # access is still proved on the explicit target below. Waiting
                # for finished() here can leave Playwright's redirect waiter
                # alive while the following navigation replaces its document.
                if not 300<=response.status<400:
                    await asyncio.wait_for(response.finished(),timeout=10)
                await self.page.wait_for_timeout(250)
            except asyncio.CancelledError:raise
            except LoginError:raise
            except Exception:raise LoginError('Keine bestätigte Antwort auf die konfigurierte Anmeldung; es wird kein zweiter Versuch gesendet.') from None
            finally:self.login_until=0;self.expected=None
            # Let the provider finish its same-origin callback before navigating
            # again. Interrupting that callback can invalidate a newly created session.
            try:await self.page.wait_for_url(lambda url:self._origin(str(url))==self.policy.origin and urlsplit(str(url)).path!=self.policy.login_path,wait_until='domcontentloaded',timeout=10000)
            except Exception:pass
            landed=await self._wait_logged_in(self.page)
            if not landed or urlsplit(self.page.url).path!=urlsplit(self.target).path:
                await site_safety.goto(self.page,self.target,wait_until='domcontentloaded',timeout=30000)
            await self.page.wait_for_timeout(400);await self.dismiss(self.page)
            if not await self._wait_logged_in(self.page):raise LoginError('Anmeldung nicht bestätigt. Zugangsdaten und sichtbares Erfolgsmerkmal prüfen; SSO, Captcha oder Zwei-Faktor-Anmeldung gegebenenfalls manuell durchführen.')
            self.proved=True;self.last_status='signed_in';self.log('Automatische Webseiten-Anmeldung bestätigt.');return self.last_status
        except asyncio.CancelledError:raise
        except site_safety.ProtectionDetected:raise
        except LoginError:raise
        except Exception:raise LoginError('Konfigurierte Webseiten-Anmeldung fehlgeschlagen; kein bestätigter Zugriff.') from None
        finally:
            self.login_until=0;self.expected=None;page,self.page=self.page,None
            if page:
                try:await page.close()
                except Exception:pass


def make_broker(context,project_url,secret,config,log=lambda message:None):
    if not config or not config.get('enabled'):return None
    if config.get('provider')=='generic_form':return GenericFormLogin(context,project_url,secret,config,log)
    if config.get('provider')=='elektro_subscription':return SubscriptionLogin(context,project_url,secret,log)
    raise LoginError('Unbekanntes Anmeldeverfahren.')


class SubscriptionLogin:
    def __init__(self, context, project_url, secret, log=lambda message: None, policy=ELEKTRO):
        self.context=context
        self.target=project_url
        self.secret=secret
        self.policy=policy
        self.log=log
        self.page=None
        self.attempts=0
        self.login_until=0
        self.consent_until=0
        self.consent_page=None
        self.proved=False
        self.last_status=None

    def _origin(self,url):
        try:
            u=urlsplit(url)
            if u.username or u.password or u.scheme not in ('http','https') or not u.hostname:return ''
            host=u.hostname.lower()
            if ':' in host:host='['+host+']'
            port=u.port
            suffix='' if port is None or (u.scheme,port) in (('https',443),('http',80)) else ':'+str(port)
            return f'{u.scheme}://{host}{suffix}'
        except ValueError:return ''

    async def permits(self, request):
        """One exact, time-bounded POST; all other mutations stay blocked."""
        if request.method!='POST':return False
        try:
            u=urlsplit(request.url)
            if self.consent_page and request.frame.page==self.consent_page and self.consent_until>time.monotonic() and self._origin(request.url)==self.policy.consent_origin and u.path.startswith('/wrapper/'):
                return True
            if not self.page or not self.secret or request.frame.page!=self.page:return False
            if (self.login_until<=time.monotonic() or request.frame!=self.page.main_frame
                    or self._origin(request.url)!=self.policy.origin or u.path!=self.policy.login_path
                    or u.query or u.fragment or not request.is_navigation_request()):return False
            values=parse_qs(request.post_data or '',keep_blank_values=True,max_num_fields=8)
            required={'login[csrfToken]','login[userIdentifier]','login[password]'}
            if not required.issubset(values) or set(values)-LOGIN_FIELDS or any(len(v)!=1 for v in values.values()):return False
            if not values['login[csrfToken]'][0] or len(values['login[csrfToken]'][0])>4096:return False
            if not hmac.compare_digest(values['login[userIdentifier]'][0],self.secret['abonummer']):return False
            if not hmac.compare_digest(values['login[password]'][0],self.secret['postcode']):return False
            if values.get('login[rememberMe]', [''])[0] not in ('','0','1','on'):return False
            self.login_until=0  # Consume the lease before allowing the request.
            return True
        except Exception:
            return False

    async def guard(self, route):
        """Used for an isolated broker test; the monitor's guard delegates permits()."""
        r=route.request
        if await self.permits(r):return await route.continue_()
        if r.method not in ('GET','HEAD','OPTIONS') or UNSAFE_PATH.search(urlsplit(r.url).path):return await route.abort('blockedbyclient')
        if r.is_navigation_request() and self._origin(r.url) not in (self.policy.origin,self.policy.consent_origin):return await route.abort('blockedbyclient')
        await route.continue_()

    async def dismiss(self,page):
        """Only observed consent/close controls of this configured site, never arbitrary X."""
        if self._origin(page.url)!=self.policy.origin:return
        for frame in page.frames:
            # The real CMP is cross-origin; the local acceptance fixture is first party.
            if self._origin(frame.url) not in (self.policy.consent_origin,self.policy.origin):continue
            if self._origin(frame.url)==self.policy.consent_origin:
                button=frame.get_by_role('button',name='Akzeptieren',exact=True)
            else:
                button=frame.locator('.cookie-banner[role="dialog"]').get_by_role('button',name='Akzeptieren',exact=True)
            try:
                if await button.count()==1 and await button.is_visible():
                    self.consent_until=time.monotonic()+8
                    self.consent_page=page
                    try:await button.click(timeout=4000)
                    finally:self.consent_until=0;self.consent_page=None
                    await page.wait_for_timeout(250)
                    self.log('Cookie-Hinweis bestätigt.')
            except asyncio.CancelledError:raise
            except Exception:raise LoginError('Cookie-Hinweis konnte nicht bestätigt werden.') from None
        # A repeated banner may appear while the credentials are being filled.
        for _ in range(3):
            close=page.locator(self.policy.close_selector)
            visible=[]
            for item in await close.all():
                if await item.is_visible():visible.append(item)
            if not visible:break
            if len(visible)!=1:raise LoginError('Werbehinweis kann nicht eindeutig geschlossen werden.')
            try:
                info=await visible[0].evaluate("e=>({form:!!e.form,type:e.getAttribute('type'),modal:!!e.closest('.modal,.advertisement,[role=dialog]')})")
                if info['form'] or info['type']!='button' or not info['modal']:raise LoginError('Werbe-Schaltfläche ist nicht freigegeben.')
                await visible[0].click(timeout=4000)
                await page.wait_for_timeout(150)
                self.log('Werbehinweis geschlossen.')
            except asyncio.CancelledError:raise
            except LoginError:raise
            except Exception:raise LoginError('Werbehinweis konnte nicht geschlossen werden.') from None

    async def _logged_in(self,page):
        if self._origin(page.url)!=self.policy.origin:return False
        # The publisher renders its signed-in logout link inside a collapsed
        # account menu. Its server-authored presence, combined with no visible
        # login form, is the state marker; it need not be visually expanded.
        if await page.locator(self.policy.form_selector+':visible').count():return False
        links=page.locator('a[href]')
        for item in await links.all():
            href=await item.get_attribute('href')
            u=urlsplit(urljoin(page.url,href or ''))
            if self._origin(u.geturl())==self.policy.origin and u.path in self.policy.logout_paths:
                return True
        return False

    async def ensure(self,force=False):
        """Verify/reuse the profile session or make one controller-authorized login."""
        if self.proved and not force:return self.last_status
        if self._origin(self.target)!=self.policy.origin:raise LoginError('Anmeldeziel stimmt nicht mit dem freigegebenen Anbieter überein.')
        if self.secret and self.secret.get('origin')!=self.policy.origin:raise LoginError('Für dieses Projekt fehlen passende Webseiten-Zugangsdaten.')
        self.page=await self.context.new_page()
        try:
            response=await site_safety.goto(self.page,self.policy.origin+self.policy.login_path,wait_until='domcontentloaded',timeout=45000)
            if response and response.status>=400:raise LoginError('Die Anmeldeseite ist nicht erreichbar.')
            await self.page.wait_for_timeout(1200)
            await self.dismiss(self.page)
            if await self._logged_in(self.page):
                self.proved=True;self.last_status='reused'
                self.log('Webseiten-Sitzung geprüft und wiederverwendet.')
                return self.last_status
            if not self.secret:raise LoginError('Für die automatische Neuanmeldung fehlen Abonummer und PLZ in den Webseiten-Zugangseinstellungen.')
            if self.attempts>=1:raise LoginError('Automatische Anmeldung bereits versucht; Zugangsdaten oder Anbieter prüfen.')
            if await self.page.locator('input[autocomplete="one-time-code"],iframe[src*="captcha"],.g-recaptcha,.h-captcha').count():
                raise LoginError('Anmeldung benötigt eine interaktive Sicherheitsbestätigung.')
            form=self.page.locator(self.policy.form_selector)
            if await form.count()!=1:raise LoginError('Das freigegebene Anmeldeformular wurde nicht gefunden.')
            spec=await form.evaluate("e=>({action:e.action,method:e.method,names:[...e.querySelectorAll('input[name]')].map(x=>x.name)})")
            action=urlsplit(spec['action'])
            if (self._origin(spec['action'])!=self.policy.origin or action.path!=self.policy.login_path or action.query
                    or spec['method'].lower()!='post' or set(spec['names'])-LOGIN_FIELDS
                    or not {'login[csrfToken]','login[userIdentifier]','login[password]'}.issubset(spec['names'])):
                raise LoginError('Das Anmeldeformular entspricht nicht dem freigegebenen Ziel.')
            await self.dismiss(self.page)
            await form.locator('[name="login[userIdentifier]"]').fill(self.secret['abonummer'],timeout=4000)
            await self.dismiss(self.page)
            await form.locator('[name="login[password]"]').fill(self.secret['postcode'],timeout=4000)
            await self.dismiss(self.page)
            submit=form.get_by_role('button',name='Einloggen',exact=True)
            if await submit.count()!=1:raise LoginError('Die Anmeldeschaltfläche wurde nicht eindeutig erkannt.')
            await site_safety.wait()
            self.attempts+=1
            self.login_until=time.monotonic()+10
            try:
                await submit.click(timeout=7000)
                await self.page.wait_for_load_state('domcontentloaded',timeout=20000)
            finally:self.login_until=0
            await self.dismiss(self.page)
            if not await self._logged_in(self.page):raise LoginError('Anmeldung nicht bestätigt. Abonummer, PLZ oder Zugriffsstatus prüfen.')
            self.proved=True;self.last_status='signed_in'
            self.log('Automatische Webseiten-Anmeldung bestätigt.')
            return self.last_status
        except asyncio.CancelledError:raise
        except site_safety.ProtectionDetected:raise
        except LoginError:raise
        except Exception:raise LoginError('Automatische Webseiten-Anmeldung fehlgeschlagen; kein bestätigter Zugriff.') from None
        finally:
            self.login_until=self.consent_until=0
            self.consent_page=None
            page,self.page=self.page,None
            if page:
                try:await page.close()
                except Exception:pass


async def open_readonly_section(page,label):
    """Open an observed same-document tab/anchor; no forms or arbitrary buttons."""
    if not isinstance(label,str) or not 0<len(label.strip())<=160:raise ValueError('Sichtbare Bereichsbezeichnung fehlt')
    visible_name=re.compile(r'^\s*'+r'\s+'.join(re.escape(word) for word in label.split())+r'\s*$',re.IGNORECASE)
    target=page.get_by_role('link',name=visible_name)
    if await target.count()!=1:target=page.get_by_role('tab',name=visible_name)
    if await target.count()!=1:raise ValueError('Kein eindeutiger Seitenreiter')
    spec=await target.evaluate("e=>({tag:e.tagName,href:e.getAttribute('href'),controls:e.getAttribute('aria-controls'),role:e.getAttribute('role'),form:!!e.form})")
    if spec['form']:raise ValueError('Formulare sind nicht als Seitenreiter freigegeben')
    fragment=None
    if spec['href']:
        current=urlsplit(page.url);wanted=urlsplit(urljoin(page.url,spec['href']))
        if (wanted.scheme,wanted.netloc,wanted.path,wanted.query)!=(current.scheme,current.netloc,current.path,current.query):raise ValueError('Seitenreiter führt zu einer anderen Quelle')
        fragment=wanted.fragment
    elif spec['role']=='tab':fragment=spec['controls']
    if not fragment or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,100}',fragment):raise ValueError('Seitenreiter hat kein bestätigtes Inhaltsziel')
    panel=page.locator('[id="'+fragment+'"]')
    if await panel.count()!=1:raise ValueError('Inhaltsbereich des Seitenreiters fehlt')
    if await panel.locator('form').count():raise ValueError('Formularbereiche sind nicht als Lesereiter freigegeben')
    await target.click(timeout=5000)
    await panel.wait_for(state='visible',timeout=5000)
    return {'selected_section':fragment,'label':label}
