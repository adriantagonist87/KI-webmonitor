"""Opt-in real desktop input, with local action checks and same-tab login.

DOM reads validate coordinates, origin and credential fields; they do not
intercept requests, modify form values or submit forms. Passwords stay local.
"""
import asyncio
import re
import time
from urllib.parse import parse_qsl, urlsplit
from native_input import NativeInput, endpoint
from site_login import GenericFormLogin, LoginError
from browser_actions import UNSAFE_LABEL, NAVIGATION_CONTAINERS, DISMISS_LABELS, _matching_controls, safe_navigation_url
import site_safety

_DANGER = UNSAFE_LABEL
_SAFE_BUTTON = re.compile(r'(?i)^(filter einblenden|filter ausblenden|menü.*|menu.*|schließen|schliessen|close|verstanden|ok|×|✕|x|alle akzeptieren|alle ablehnen|nur notwendige.*|zustimmen|ablehnen)$')


async def mobile_window(browser, context):
    """Open a temporary narrow Brave window without changing UA or the main window."""
    session = await browser.new_browser_cdp_session()
    target_id = None
    try:
        async with context.expect_page(timeout=10000) as opened:
            target = await session.send('Target.createTarget', {
                'url': 'about:blank', 'newWindow': True, 'left': 20, 'top': 20,
                'width': 460, 'height': 900,
            })
            target_id = target['targetId']
        page = await opened.value
        page_session = await context.new_cdp_session(page)
        try:
            info = await page_session.send('Target.getTargetInfo')
            if info['targetInfo']['targetId'] != target_id:
                raise RuntimeError('Schmales Brave-Fenster konnte nicht eindeutig zugeordnet werden.')
        finally:
            await page_session.detach()
        bounds = await session.send('Browser.getWindowForTarget', {'targetId': target_id})
        await session.send('Browser.setWindowBounds', {'windowId': bounds['windowId'],
                           'bounds': {'left': 20, 'top': 20, 'width': 460, 'height': 900}})
        await page.wait_for_function('innerWidth <= 600 && innerWidth < innerHeight', timeout=8000)
        return page
    except BaseException:
        if target_id:
            await session.send('Target.closeTarget', {'targetId': target_id})
        raise
    finally:
        await session.detach()


class Desktop:
    def __init__(self, page, cdp_url, allowed):
        self.page, self.allowed = page, allowed
        self.input = NativeInput(endpoint(cdp_url))

    async def __aenter__(self):
        await self.input.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.input.__aexit__(*args)

    async def _window_geometry(self):
        await self.page.bring_to_front()
        last=None;stable=0
        session=await self.page.context.new_cdp_session(self.page)
        try:
            for _ in range(20):
                # Brave may protect/fuzz JavaScript screenX/outerHeight. Read
                # window geometry from the browser, without changing Shields.
                bounds=(await session.send('Browser.getWindowForTarget'))['bounds']
                layout=(await session.send('Page.getLayoutMetrics'))['cssVisualViewport']
                m={'x':bounds['left'],'y':bounds['top'],'ow':bounds['width'],'oh':bounds['height'],
                   'iw':layout['clientWidth'],'ih':layout['clientHeight'],'d':layout['scale']}
                stable=stable+1 if m==last else 0
                if stable>=4:break
                last=m;await asyncio.sleep(.15)
            else:raise ValueError('Browserfenster ist noch nicht stabil.')
        finally:await session.detach()
        return m

    @staticmethod
    def _desktop_point(m, x, y):
        if m['d'] != 1 or not 0 <= x < m['iw'] or not 0 <= y < m['ih']:
            raise ValueError('Bildkoordinaten oder Desktop-Skalierung nicht unterstützt.')
        border = (m['ow']-m['iw'])/2
        if not 0<=border<=16:raise ValueError('Browserzoom oder Seitenleiste verändert die Bildkoordinaten.')
        return round(m['x']+border+x), round(m['y']+m['oh']-m['ih']-border+y)

    async def coordinates(self, x, y):
        if type(x) not in (int, float) or type(y) not in (int, float):
            raise ValueError('Numerische Bildkoordinaten erforderlich.')
        return self._desktop_point(await self._window_geometry(), x, y)

    async def point(self, locator):
        if await locator.count() != 1 or not await locator.is_visible():
            raise ValueError('Kein eindeutiges sichtbares Ziel.')
        box = await locator.bounding_box()
        if not box: raise ValueError('Kein sichtbares Ziel.')
        return box['x']+box['width']/2, box['y']+box['height']/2

    async def click_locator(self, locator):
        geometry = await self._window_geometry()
        x, y = await self.point(locator)
        receives_click = await locator.evaluate('''(e, {x, y}) => {
            const hit = e.ownerDocument.elementFromPoint(x, y);
            return !e.matches(':disabled') && (hit === e || e.contains(hit));
        }''', {'x': x, 'y': y}, timeout=1500)
        if not receives_click:
            return False
        await self.input.click(*self._desktop_point(geometry, x, y))
        return True

    async def wait_ready(self, timeout=20):
        deadline = time.monotonic()+timeout
        last = None; stable = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(.5)
            try:
                await site_safety.check_page(self.page)
                state = await self.page.evaluate('({url:location.href,ready:document.readyState,n:document.body?.innerText.length||0})')
                if state['ready'] == 'complete' and state['n'] and state == last:
                    stable += 1
                    if stable >= 3: return
                else: stable = 0
                last = state
            except site_safety.ProtectionDetected: raise
            except Exception: pass  # Navigation is replacing the visible document.
        await site_safety.check_page(self.page)

    async def goto(self, url, *, force=False):
        if not self.allowed(url): raise ValueError('Navigation außerhalb des Projektauftrags.')
        if not force and self.page.url.rstrip('/') == url.rstrip('/'):
            await self.wait_ready()
            return
        await site_safety.wait()
        await self.page.bring_to_front()
        await self.input.chord('ctrl', 'l')
        await self.input.type_text(url)
        try:
            async with self.page.expect_navigation(wait_until='domcontentloaded',timeout=30000):
                await self.input.chord('return')
        except Exception:
            await site_safety.check_page(self.page)
            raise RuntimeError('Die Adresse wurde nicht im sichtbaren Browser geöffnet.') from None
        await self.wait_ready()

    async def click(self, x, y, label, *, macro_anchor=''):
        if not isinstance(label, str) or (not label.strip() and not macro_anchor) or len(label) > 160:
            raise ValueError('Sichtbare Beschriftung erforderlich.')
        await site_safety.check_page(self.page)
        await site_safety.wait()
        desktop = await self.coordinates(x, y)
        info = await self.page.evaluate('''({x,y,nav})=>{const e=document.elementFromPoint(x,y)?.closest('a,button,[role=button],[role=tab],summary');if(!e)return null;return {label:(e.getAttribute('aria-label')||e.innerText||e.title||'').trim(),href:e.href||'',form:!!e.form||!!e.closest('form'),disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',target:e.target||'',cookie:!!e.closest('[id*=cookie],[class*=cookie],[id*=consent],[class*=consent]'),tag:e.tagName,expanded:e.hasAttribute('aria-expanded'),role:e.getAttribute('role'),nav:!!e.closest(nav),type:e.type||'',anchors:[e.id,e.getAttribute('aria-controls')]}}''', {'x':x, 'y':y,'nav':NAVIGATION_CONTAINERS})
        normalize = lambda v: ' '.join(v.casefold().split())
        matches = info and (normalize(info['label']) == normalize(label) if label
                            else not info['label'] and macro_anchor in info['anchors'])
        if not matches or info['form'] or info['disabled'] or info['target'] not in ('','_self') or _DANGER.search(info['label']):
            raise ValueError('Bildziel stimmt nicht mit einer erlaubten sichtbaren Schaltfläche überein.')
        if re.search(r'(?i)akzeptier|zustimm|ablehn|notwendige',info['label']) and not info['cookie']:
            raise ValueError('Zustimmung ist nur in einem bestätigten Cookiehinweis möglich.')
        if info['href']:
            if not self.allowed(info['href']) or not safe_navigation_url(self.page.url,info['href']):
                raise ValueError('Link außerhalb des erlaubten Projektbereichs.')
        elif normalize(info['label']) in DISMISS_LABELS:
            matches=await _matching_controls(self.page,normalize(info['label']))
            if len(matches)!=1 or any(matches[0][1][key] for key in ('inForm','disabled','agreement')):
                raise ValueError('Dieser Hinweis darf nicht automatisch bestätigt werden.')
        elif not (info['tag']=='SUMMARY' or info['expanded'] or info['role']=='tab' or info['nav'] or _SAFE_BUTTON.fullmatch(info['label'])):
            raise ValueError('Schaltfläche ist keine bestätigte Navigation oder Hinweisquittierung.')
        await self.input.click(*desktop)
        await self.wait_ready()
        return {'clicked':info['label']}

    async def click_label(self, label):
        targets = self.page.get_by_role('button', name=label, exact=True)
        if await targets.count() != 1:
            targets = self.page.get_by_role('link', name=label, exact=True)
        if await targets.count() != 1:
            targets = self.page.get_by_role('tab', name=label, exact=True)
        x, y = await self.point(targets)
        return await self.click(x, y, label)

    async def scroll(self, direction, selector=None, x=None, y=None):
        await site_safety.check_page(self.page)
        await site_safety.wait()
        if selector:
            targets=[e for e in await self.page.locator(selector).all() if await e.is_visible()]
            if len(targets)!=1:raise ValueError('Scrollbereich ist nicht eindeutig sichtbar.')
            box = await targets[0].bounding_box()
            if not box: raise ValueError('Scrollbereich nicht sichtbar.')
            size = await self.page.evaluate('({w:innerWidth,h:innerHeight})')
            x = max(1, min(size['w']-2, box['x']+box['width']/2))
            y = max(1, min(size['h']-2, box['y']+min(box['height'],size['h']-box['y'])/2))
        elif x is None or y is None:
            size = await self.page.evaluate('({w:innerWidth,h:innerHeight})')
            x, y = size['w']/2, size['h']/2
        measure = "s=>{let es=s?[...document.querySelectorAll(s)].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[hidden],[aria-hidden=\"true\"]')):[document.scrollingElement];if(es.length!==1||!es[0])return null;const e=es[0];return {after:e.scrollTop,scroll_height:e.scrollHeight,client_height:e.clientHeight}}"
        before = await self.page.evaluate(measure, selector)
        await self.input.scroll(*await self.coordinates(x,y), direction)
        await self.wait_ready(timeout=8)
        after = await self.page.evaluate(measure, selector)
        if before is None or after is None: raise ValueError('Scrollbereich nicht messbar.')
        return {**after,'before':before['after'],'progress':before!=after,
                'at_start':after['after']<=1,'at_end':after['after']+after['client_height']>=after['scroll_height']-2}


def approved_login_action(actual, configured, project_url):
    if actual == configured:
        return True
    # Some SSO forms change only the return URL when the library is opened
    # from another entry point. The POST endpoint and every other parameter
    # must match, and the new return URL may only be the exact project URL.
    try:
        action, saved, target = map(urlsplit, (actual, configured, project_url))
        if (action._replace(query='') != saved._replace(query='') or action.fragment
                or target.scheme != saved.scheme or target.netloc != saved.netloc
                or target.fragment or target.username or target.password):
            return False
        query = parse_qsl(action.query, keep_blank_values=True, strict_parsing=True)
        previous = parse_qsl(saved.query, keep_blank_values=True, strict_parsing=True)
        returns = [value for key, value in query if key == 'return_url']
        return (sum(key == 'return_url' for key, _ in previous) == 1
                and len(returns) == 1
                and urlsplit(returns[0]) == target
                and [(key, value) for key, value in query if key != 'return_url']
                == [(key, value) for key, value in previous if key != 'return_url'])
    except ValueError:
        return False


class VisualLogin(GenericFormLogin):
    def __init__(self, context, project_url, secret, config, desktop, log):
        super().__init__(context,project_url,secret,config,log)
        self.desktop = desktop
        self.page = desktop.page
        self.submit_url = config["submit_url"]

    async def _field_focused(self, field, key):
        return await field.evaluate('''(e, check) => {
            const doc = e.ownerDocument;
            return doc.location.origin === check.origin && doc.hasFocus() && doc.activeElement === e
                && e instanceof HTMLInputElement && !e.matches(':disabled') && !e.readOnly
                && e.form?.action === check.submit && e.form.method.toLowerCase() === 'post'
                && (check.password ? e.type === 'password' : ['text', 'email', 'tel', 'number'].includes(e.type));
        }''', {'origin': self.policy.origin, 'submit': self.submit_url,
              'password': key == 'password'}, timeout=1500)

    async def _focus_field(self, field, key, check_target):
        for _ in range(2):
            await site_safety.check_page(self.page)
            await check_target()
            if await self.desktop.click_locator(field):
                for _ in range(10):
                    if await self._field_focused(field, key):
                        return
                    await asyncio.sleep(.12)
            else:
                await asyncio.sleep(.5)
        raise LoginError('Zugangsfeld konnte nicht fokussiert werden.')

    async def dismiss(self, page):
        for name in ('consent_selector','close_selector'):
            selector = self.config.get(name)
            if not selector: continue
            for _ in range(3):
                targets = page.locator(selector)
                visible = [e for e in await targets.all() if await e.is_visible()]
                if not visible: break
                if len(visible)!=1: raise LoginError('Anmeldehinweis ist nicht eindeutig.')
                label = await visible[0].evaluate("e=>(e.getAttribute('aria-label')||e.innerText||e.title||'').trim()")
                x,y = await self.desktop.point(visible[0])
                await self.desktop.click(x,y,label)

    async def ensure(self, force=False):
        page = self.page
        if self.proved and not force: return self.last_status
        await self.desktop.wait_ready()
        if await self._logged_in(page):
            self.proved=True; self.last_status='reused'
            self.log('Vorhandene Webseiten-Sitzung im selben Tab wiederverwendet.')
            return self.last_status
        if self.attempts: raise LoginError('Anmeldung bereits versucht; kein weiterer Anmeldeversuch.')
        if self._origin(page.url)!=self.policy.origin:
            raise LoginError('Anmeldeseite gehört nicht zum gespeicherten Webseitenzugang.')
        await self.dismiss(page)
        await site_safety.check_page(page)
        if not self.secret or self.secret.get('provider')!='generic_form' or self.secret.get('origin')!=self.policy.origin:
            raise LoginError('Passende gespeicherte Webseiten-Zugangsdaten fehlen.')
        form=page.locator(self.config['form_selector'])
        if await form.count()!=1 or not await form.is_visible():
            raise LoginError('Keine eindeutige sichtbare Anmeldemaske; keine direkte SSO-URL wird aufgerufen.')
        spec=await form.evaluate('e=>({action:e.action,method:e.method,fields:[...e.elements].filter(x=>!["hidden","submit","button","checkbox","radio"].includes(x.type)).map(x=>({name:x.name,type:x.type}))})')
        if (not approved_login_action(spec['action'], self.config['submit_url'], self.target)
                or spec['method'].lower() != 'post' or len(spec['fields']) != 2):
            raise LoginError('Sichtbares Formular entspricht nicht dem freigegebenen Webseitenzugang.')
        self.submit_url = spec['action']
        user=form.locator(self.config['username_selector']);password=form.locator(self.config['password_selector']);submit=form.locator(self.config['submit_selector'])
        for field in (user,password,submit):
            if await field.count()!=1 or not await field.is_visible(): raise LoginError('Anmeldefelder nicht eindeutig sichtbar.')
        if not await submit.evaluate('e=>!!e.form&&e.type==="submit"'):
            raise LoginError('Kein bestätigter Anmeldeknopf.')
        async def check_target():
            spec=await submit.evaluate('e=>({action:e.hasAttribute("formaction")?e.formAction:e.form.action,method:e.hasAttribute("formmethod")?e.formMethod:e.form.method})')
            if self._origin(page.url)!=self.policy.origin or spec['action']!=self.submit_url or spec['method'].lower()!='post':
                raise LoginError('Anmeldeziel hat sich geändert.')
            if await password.get_attribute('type')!='password':
                raise LoginError('Kein geschütztes Passwortfeld.')
        await check_target()
        self.attempts+=1
        # No screenshot/model call occurs while entering or submitting secrets.
        for field,key in ((user,'username'),(password,'password')):
            async def verify_focus():
                if not await self._field_focused(field, key):
                    raise LoginError('Zugangsfeld hat den Fokus verloren. Eingabe abgebrochen.')

            await self._focus_field(field, key, check_target)
            await verify_focus()
            await self.desktop.input.chord('ctrl','a')
            await self.desktop.input.type_text(self.secret[key], before_key=verify_focus)
            for _ in range(20):
                if await field.input_value()==self.secret[key]:break
                await asyncio.sleep(.1)
            else:raise LoginError('Zugangsfeld konnte nicht vollständig ausgefüllt werden.')
        await site_safety.wait()
        await site_safety.check_page(page)
        await check_target()
        if not await self.desktop.click_locator(submit):
            raise LoginError('Anmeldeknopf ist verdeckt. Anmeldung nicht abgeschickt.')
        await self.desktop.wait_ready(timeout=45)
        if not await self._wait_logged_in(page):
            raise LoginError('Anmeldung nicht bestätigt; kein zweiter Versuch und kein erneuter Seitenaufruf.')
        self.proved=True;self.last_status='signed_in'
        self.log('Native Anmeldung bestätigt; derselbe Tab bleibt für die Analyse geöffnet.')
        await self.dismiss(page)
        return self.last_status


async def collection_structure(page, date_label):
    """Observe repeated dated cards and their real scroll parent; never guess CSS."""
    if not isinstance(date_label,str) or not 0<len(date_label)<100:
        raise ValueError('Sichtbare Datumsbeschriftung erforderlich.')
    result=await page.evaluate(r'''label=>{
      const norm=s=>s.replace(/[:\s]+$/,'').trim().toLocaleLowerCase('de');
      const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[hidden],[aria-hidden="true"],form');
      const dates=e=>[...e.querySelectorAll('dt')].filter(d=>visible(d)&&norm(d.innerText)===norm(label)&&visible(d.nextElementSibling)&&/^\d{4}-\d{2}(?:-\d{2})?$/.test(d.nextElementSibling.innerText.trim()));
      const heading=e=>e.querySelectorAll('h1 a[href],h2 a[href],h3 a[href],h4 a[href],h5 a[href],h6 a[href]');
      const cards=[];
      for(const dt of dates(document)){
        let e=dt.parentElement, fallback=null, found=null;
        for(let depth=0;e&&e!==document.body&&depth<12;depth++,e=e.parentElement){
          if(dates(e).length!==1||heading(e).length>1)break;
          if(heading(e).length!==1)continue;
          if(!fallback)fallback=e;
          if(e.matches('article,section,li,tr')){found=e;break;}
        }
        if(found||fallback)cards.push(found||fallback);
      }
      const unique=[...new Set(cards)];
      if(!unique.length||unique.length>500)return {error:'Keine eindeutig gegliederten datierten Einträge mit verlinkter Überschrift sichtbar.'};
      const choose=(first,all)=>{
        const tag=first.tagName.toLowerCase();
        const candidates=[tag,...[...first.classList].map(c=>tag+'.'+CSS.escape(c))];
        if(first.id)candidates.push('#'+CSS.escape(first.id));
        candidates.push(tag+[...first.classList].map(c=>'.'+CSS.escape(c)).join(''));
        return candidates.find(s=>{const matches=[...document.querySelectorAll(s)].filter(visible);return matches.length===all.length&&all.every(e=>matches.includes(e));});
      };
      const item=choose(unique[0],unique);
      if(!item)return {error:'Mehrere Listenbereiche sind nicht eindeutig voneinander abgrenzbar.'};
      const title=['h1 a[href]','h2 a[href]','h3 a[href]','h4 a[href]','h5 a[href]','h6 a[href]'].find(s=>unique.every(e=>e.querySelectorAll(s).length===1));
      let scroller=unique[0].parentElement;
      while(scroller&&scroller!==document.body){
        if(/auto|scroll/.test(getComputedStyle(scroller).overflowY)&&unique.every(e=>scroller.contains(e)))break;
        scroller=scroller.parentElement;
      }
      if(!scroller||scroller===document.body)scroller=document.scrollingElement;
      const container=choose(scroller,[scroller]);
      if(!title||!container)return {error:'Titel oder Scrollbereich der sichtbaren Sammlung ist nicht eindeutig.'};
      return {item_selector:item,title_selector:title,date_label:label,container_selector:container};
    }''',date_label)
    if result.get('error'):raise ValueError(result['error'])
    return result


def agent_options():
    from agent_driver import TOOLS, AGENT_PROMPT, tool
    tools=[t for t in TOOLS if t['function']['name'] not in ('expand_section','open_section','scan_collection')]
    tools.append(tool('scan_collection','Read the visible dated collection using the actually observed cards and scroll area. Supply only the exact selected navigation label and visible date label. The controller discovers structure; do not invent CSS selectors. Stops at current-year boundary.',
                      {'filter_label':{'type':'string'},'date_label':{'type':'string'}},['filter_label','date_label']))
    tools += [
        tool('visual_click','Click a control recognized in the screenshot. Give its exact visible label; the local controller locates that visible control and moves the real mouse to its center. Optional x/y only if using exact screenshot pixels. One click; then inspect the new screenshot. No purchases, account changes or credential fields.',
             {'x':{'type':'number'},'y':{'type':'number'},'label':{'type':'string'}},['label']),
        tool('visual_scroll','Scroll the visible area under screenshot coordinates using the real mouse wheel.',
             {'x':{'type':'number'},'y':{'type':'number'},'direction':{'type':'string','enum':['up','down']}},['x','y','direction'])]
    prompt=AGENT_PROMPT+'''\nVISUELLER BEDIENMODUS: Du siehst den aktuellen Brave-Viewport. Bediene Navigation und Hinweisfenster bevorzugt mit visual_click anhand der Bildkoordinaten und der exakten sichtbaren Beschriftung. Die Koordinaten sind Pixel des gelieferten Bildes, nicht auf 1000 normiert. Klicke nur ein Ziel pro Runde und prüfe danach das Ergebnis. activate_navigation und dismiss_notice benutzen hier ebenfalls die echte Maus. Formulare und Zugangsdaten bedient ausschließlich der lokale Anmeldecontroller. Keine Käufe oder Kontoeinstellungen. Vorhandene Sitzungen und normale Seitenweiterleitungen bleiben bestehen. scan_collection liest belegte Einträge und scrollt ebenfalls mit dem Mausrad. Verwende es erst nach visuell bestätigter Auswahl des verlangten Filters. expand_section und open_section stehen hier nicht zur Verfügung; verwende sichtbare Knöpfe.\n'''
    prompt+='\nBei lesbarer Beschriftung visual_click nur mit label aufrufen, ohne x/y. Der Controller bestimmt die tatsächliche Mitte des sichtbaren Knopfs; nicht raten. Deine Auswahl stammt aus dem Bild, der Klick erfolgt über die echte Maus.\n'
    prompt+='\nscan_collection benötigt in diesem Modus ausschließlich filter_label und date_label aus der sichtbaren Seite. Karten, Titel und Scrollbereich bestimmt der Controller aus dem tatsächlich geladenen Dokument. Keine CSS-Selektoren raten.\n'
    return {'tools':tools,'prompt':prompt}
