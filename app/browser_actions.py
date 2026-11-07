"""Small, site-independent, read-only browser actions with bounded DOM checks.

The caller keeps its network request guard installed. These actions never fill
fields, submit forms, execute page-provided code, or accept account transactions.
"""
import re
from urllib.parse import urljoin, urlsplit, unquote


NOTICE_CONTAINERS = 'dialog[open],[role="dialog"],[aria-modal="true"],.modal,.uk-modal,.ant-modal,.v-dialog'
NAVIGATION_CONTAINERS = 'nav,aside,[role="navigation"],.sidebar,.uk-nav,.uk-offcanvas-bar'
DISMISS_LABELS = {'x', '×', '✖', 'schließen', 'schliessen', 'verstanden', 'ok', 'close'}
UNSAFE_LABEL = re.compile(
    r'(?i)(?:abmeld|anmeld|log\s*(?:in|out|off)|sign\s*(?:in|out)|admin|benutzerdaten|'
    r'benutzerkonto|\bkonto\b|account|\bprofil\b|warenkorb|\bcart\b|\bbasket\b|'
    r'checkout|bestell|kaufen|purchase|bezahlen|payment|abonn|kündig|kuendig|'
    r'rechnung|billing|passwort|password|einstellungen|settings|delete|löschen|loeschen|unsubscribe)'
)
UNSAFE_PATH = re.compile(
    r'(?i)(?:^|[/_?&=.#-])(?:logout|logoff|signin|signout|login|admin|account|settings|'
    r'preferences|profile|cart|basket|checkout|purchase|payment|delete|remove|unsubscribe|'
    r'cancel|terminate|billing|invoice|warenkorb|bestellen|abmelden)(?:$|[/_?&=.#-])'
)


def normalize_label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError('Eine eindeutige sichtbare Beschriftung ist erforderlich.')
    return ' '.join(value.split()).casefold()


def safe_navigation_url(current, target):
    """This tool selects local navigation; ordinary source links use read_sources."""
    try:
        result = urljoin(current, target)
        start, end = urlsplit(current), urlsplit(result)
        origin = lambda u: (u.scheme, u.hostname, u.port or (443 if u.scheme == 'https' else 80))
        if end.scheme not in ('http', 'https') or not end.hostname or end.username or end.password:
            return False
        location=end.path + '?' + end.query + '#' + end.fragment
        for _ in range(2):location=unquote(location)
        if origin(start) != origin(end) or UNSAFE_PATH.search(location):
            return False
        return not any(ord(c) < 32 for c in result)
    except (TypeError, ValueError):
        return False


async def _matching_controls(page, label, navigation=False):
    """Read fixed properties only; never return a whole dialog or login form."""
    result = []
    containers=NAVIGATION_CONTAINERS if navigation else NOTICE_CONTAINERS
    controls = page.locator(':is('+containers+') :is(a,button,[role="button"],[role="link"],input[type="button"])')
    if await controls.count() > 3000:
        raise ValueError('Zu viele Bedienelemente für eine eindeutige sichere Auswahl.')
    for control in await controls.all():
        if not await control.is_visible():
            continue
        info = await control.evaluate(r'''(e, navigation) => {
          const norm = s => (s || '').replace(/\s+/g,' ').trim().toLocaleLowerCase('de');
          const names = [e.getAttribute('aria-label'), e.getAttribute('title'), e.innerText || e.textContent, e.tagName==='INPUT' ? e.value : ''].map(norm).filter(Boolean);
          const container = e.closest(navigation ? 'nav,aside,[role="navigation"],.sidebar,.uk-nav,.uk-offcanvas-bar' : 'dialog[open],[role="dialog"],[aria-modal="true"],.modal,.uk-modal,.ant-modal,.v-dialog');
          const tag = e.tagName;
          return {names, inContainer:!!container, inForm:!!e.closest('form') || !!e.form,
            button:tag==='BUTTON'||(e.getAttribute('role')==='button'&&tag!=='A')||(tag==='INPUT'&&e.type==='button'),
            link:tag==='A'||e.getAttribute('role')==='link', href:e.getAttribute('href'),
            target:e.getAttribute('target'), disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
            agreement:!!container && (!navigation) && (
              [...container.querySelectorAll('input,select,textarea')].some(n=>n.getClientRects().length) ||
              /(?:hiermit|ich)\s+(?:akzeptiere|bestelle|kaufe|schließe|schliesse|stimme\s+zu)|mit\s+(?:diesem\s+)?(?:klick|bestätigen).{0,90}(?:vertrag|kostenpflichtig|bestell|akzeptier)/i.test(container.innerText||'')
            )};
        }''', navigation)
        if label not in {name.casefold() for name in info['names']} or not info['inContainer']:
            continue
        # A conflicting same-labelled control makes the selection ambiguous,
        # even if another matching control would be safe on its own.
        result.append((control, info))
    return result


async def dismiss_notice(page, label):
    wanted = normalize_label(label)
    if wanted not in DISMISS_LABELS:
        raise ValueError('Nur ein ausdrücklich benannter Schließen-/Hinweisknopf ist erlaubt.')
    matches = await _matching_controls(page, wanted)
    if len(matches) != 1:
        raise ValueError('Kein eindeutiger sichtbarer Hinweisknopf in einem bestätigten Dialog.')
    button, info = matches[0]
    if not info['button'] or info['href'] is not None or info['inForm'] or info['disabled'] or info['agreement']:
        raise ValueError('Dieser Hinweis darf nicht automatisch bestätigt werden.')
    modal = button.locator('xpath=ancestor::*[(self::dialog and @open) or @role="dialog" or @aria-modal="true" or contains(concat(" ",normalize-space(@class)," ")," modal ") or contains(concat(" ",normalize-space(@class)," ")," uk-modal ") or contains(concat(" ",normalize-space(@class)," ")," ant-modal ") or contains(concat(" ",normalize-space(@class)," ")," v-dialog ")][1]')
    await button.click(timeout=4000)
    for _ in range(20):
        if not await modal.is_visible():
            return {'action': 'dismiss_notice', 'label': ' '.join(label.split()), 'dismissed': True}
        await page.wait_for_timeout(100)
    raise ValueError('Der Hinweis wurde nach dem Klick nicht geschlossen; keine Wiederholung ausgeführt.')


_COOKIE_CHOICE = r'''() => {
    const norm=s=>(s||'').normalize('NFC').replace(/\s+/g,' ').trim().toLocaleLowerCase('de');
    const priorities=new Map([
        ['auswahl speichern',0],['auswahl bestätigen',0],['einstellungen speichern',0],
        ['save selection',0],['save preferences',0],
        ['nur notwendige cookies akzeptieren',1],['nur notwendige akzeptieren',1],
        ['nur erforderliche cookies akzeptieren',1],['accept necessary cookies',1],
        ['alle auswählen und akzeptieren',2],['alle cookies akzeptieren',2],
        ['alle akzeptieren',2],['accept all cookies',2],['accept all',2],['akzeptieren',2]
    ]);
    const visible=e=>{
        if(!e||!e.getClientRects().length)return false;
        const r=e.getBoundingClientRect(),own=getComputedStyle(e);
        if(r.width<=0||r.height<=0||own.visibility==='hidden'||own.visibility==='collapse')return false;
        for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
            if(s.display==='none'||s.contentVisibility==='hidden'||Number(s.opacity)===0)return false;}
        return true;
    };
    const cookieHeading=e=>[...e.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],legend')]
        .some(h=>visible(h)&&/\bcookies?\b|\bconsent\b|privacy preferences|datenschutz[-\s]?einstellungen/i.test(h.innerText||''));
    const context=button=>{
        let n=button.parentElement;
        for(let depth=0;n&&depth<10;depth++,n=n.parentElement){
            if(n.tagName==='BODY'||n.tagName==='HTML')break;
            const marked=/cookie|consent/i.test(n.id+' '+(n.getAttribute('class')||''))||
                n.matches('dialog[open],[role="dialog"],[aria-modal="true"]');
            if(marked&&visible(n)&&cookieHeading(n))return n;
        }
        return null;
    };
    const uniqueId=e=>e.id&&e.id.length<=200&&!/[\u0000-\u001f\u007f]/.test(e.id)&&
        document.querySelectorAll('#'+CSS.escape(e.id)).length===1?e.id:null;
    const path=e=>{
        const parts=[];
        for(let n=e;n&&n.nodeType===1;n=n.parentElement){
            const id=uniqueId(n);
            if(id){parts.unshift('#'+CSS.escape(id));break;}
            const peers=n.parentElement?[...n.parentElement.children].filter(p=>p.tagName===n.tagName):[n];
            parts.unshift(n.localName+(peers.length>1?':nth-of-type('+(peers.indexOf(n)+1)+')':''));
        }
        const result=parts.join(' > ');
        return result.length<=240&&document.querySelectorAll(result).length===1?result:null;
    };
    const buttons=[...document.querySelectorAll('button')];
    if(buttons.length>3000)return {error:'Zu viele Cookie-Bedienelemente für eine sichere Auswahl.'};
    const candidates=[];
    for(const button of buttons){
        if(!visible(button))continue;
        const label=(button.innerText||'').normalize('NFC').replace(/\s+/g,' ').trim();
        const priority=priorities.get(norm(label));
        if(priority===undefined)continue;
        const container=context(button);
        if(!container)continue;
        const active='a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable]:not([contenteditable="false"])';
        const unsafe=button.form||button.closest('form')||container.closest('form')||
            button.disabled||button.getAttribute('aria-disabled')==='true'||button.closest('[inert]')||
            button.hasAttribute('href')||button.hasAttribute('formaction')||button.getAttribute('role')==='link'||
            !['button','submit'].includes(button.type)||button.querySelector(active)||
            container.querySelector('form,textarea,select,[contenteditable]:not([contenteditable="false"]),input:not([type="checkbox"]):not([type="radio"])')||
            /kostenpflicht|bestell|warenkorb|checkout|purchase|payment|abonnement|passwort|password/i.test(container.innerText||'');
        const selector=path(button),container_selector=path(container);
        candidates.push({label,priority,selector,container_selector,unsafe:!!unsafe});
    }
    if(!candidates.length)return {missing:true};
    const priority=Math.min(...candidates.map(c=>c.priority));
    const matches=candidates.filter(c=>c.priority===priority);
    if(matches.length!==1)return {error:'Die sichtbare Cookie-Auswahl ist nicht eindeutig.'};
    const selected=matches[0];
    if(selected.unsafe||!selected.selector||!selected.container_selector)
        return {error:'Die Cookie-Auswahl ist kein sicherer formularfreier Bestätigungsknopf.'};
    return selected;
}'''


async def dismiss_cookie_banner(page):
    """Confirm one main-DOM cookie choice, preferring existing selection.

    Only an exact visible label inside an independently identified cookie/consent
    container is eligible. A native button's default ``submit`` type is harmless
    only when neither a surrounding nor an external form owns it. No routes,
    request permissions, checkbox values or page scripts are changed here.
    Cross-origin/iframe consent remains the configured login broker's scope.
    """
    choice=await page.evaluate(_COOKIE_CHOICE)
    if choice.get('missing'):
        return {'action':'dismiss_cookie_banner','dismissed':False}
    if choice.get('error'):
        raise ValueError(choice['error'])
    current=await page.evaluate(_COOKIE_CHOICE)
    if (current.get('error') or current.get('missing') or
            any(current.get(key)!=choice.get(key) for key in ('label','selector','container_selector'))):
        raise ValueError('Die sichere Cookie-Auswahl wurde vor dem Klick verändert.')
    button=page.locator(choice['selector'])
    if await button.count()!=1:
        raise ValueError('Die sichere Cookie-Auswahl wurde vor dem Klick verändert.')
    try:
        await button.click(timeout=4000)
    except Exception:
        raise ValueError('Die Cookie-Auswahl konnte nicht sicher bestätigt werden; keine Wiederholung ausgeführt.') from None
    for _ in range(20):
        if not await page.locator(choice['container_selector']).is_visible():
            return {'action':'dismiss_cookie_banner','label':choice['label'],'dismissed':True}
        await page.wait_for_timeout(100)
    raise ValueError('Der Cookie-Hinweis blieb nach der Auswahl sichtbar; keine Wiederholung ausgeführt.')


async def _navigation_target(page, wanted):
    matches = await _matching_controls(page, wanted, navigation=True)
    if len(matches) != 1:
        raise ValueError('Kein eindeutiger sichtbarer Navigationspunkt mit exakt dieser Beschriftung.')
    control, info = matches[0]
    if info['inForm'] or info['disabled'] or not (info['button'] or info['link']):
        raise ValueError('Der Navigationspunkt ist keine freigegebene Leseaktion.')
    if info['target'] and info['target'].lower() not in ('_self',):
        raise ValueError('Navigation muss im selben Seitenfenster bleiben.')
    if info['href'] is not None and not safe_navigation_url(page.url, info['href']):
        raise ValueError('Das Navigationsziel ist keine freigegebene lokale Leseadresse.')
    if info['link'] and info['href'] is None:
        raise ValueError('Ein Navigationslink ohne bestätigtes Ziel ist nicht erlaubt.')
    return control


async def _visible_notice(page):
    return any([await e.is_visible() for e in await page.locator(NOTICE_CONTAINERS).all()])


async def _dismiss_information_once(page):
    """Find a single safe close action; never try another action after a click."""
    for label in ('✖', '×', 'X', 'Schließen', 'Verstanden', 'OK', 'Close'):
        matches=await _matching_controls(page,normalize_label(label))
        if not matches:
            continue
        if len(matches)!=1:
            raise ValueError('Hinweis ist nicht eindeutig; keine automatische Quittierung ausgeführt.')
        try:
            result=await dismiss_notice(page,label)
        except Exception:
            raise ValueError('Hinweis konnte nicht sicher geschlossen werden; Navigation abgebrochen.') from None
        if result.get('dismissed'):
            return True
        raise ValueError('Hinweis wurde nicht bestätigt geschlossen; Navigation abgebrochen.')
    raise ValueError('Hinweis hat keinen eindeutig freigegebenen Schließenknopf; Navigation abgebrochen.')


async def activate_navigation(page, label):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    wanted = normalize_label(label)
    if UNSAFE_LABEL.search(wanted):
        raise ValueError('Konto-, Verwaltungs- und Transaktionsaktionen sind nicht erlaubt.')
    control=await _navigation_target(page,wanted)
    dismissed=False
    if await _visible_notice(page):
        dismissed=await _dismiss_information_once(page)
        control=await _navigation_target(page,wanted)
    try:
        await control.click(timeout=4000)
    except PlaywrightTimeoutError:
        if dismissed or not await _visible_notice(page):
            raise ValueError('Hinweis oder blockierte Navigation verhindert den Klick; keine weitere Quittierung ausgeführt.') from None
        dismissed=await _dismiss_information_once(page)
        # The dialog may replace controls or change their form/action binding.
        # The exact same label and every safety check must therefore run again.
        control=await _navigation_target(page,wanted)
        try:
            await control.click(timeout=4000)
        except Exception:
            raise ValueError('Hinweis oder blockierte Navigation bleibt nach einmaliger Quittierung bestehen.') from None
    except Exception:
        raise ValueError('Navigation konnte nicht sicher angeklickt werden; keine Wiederholung ausgeführt.') from None
    try:
        await page.wait_for_load_state('domcontentloaded', timeout=5000)
    except Exception:
        # A single-page route need not trigger a document navigation.
        pass
    await page.wait_for_timeout(200)
    return {'action': 'activate_navigation', 'label': ' '.join(label.split()), 'url': page.url,
            'notices_closed':int(dismissed)}


_SCROLL = r'''({direction, selector}) => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth&&s.visibility!=='hidden'&&s.display!=='none'};
  const excluded=e=>!!e.closest('nav,aside,[role="navigation"],.sidebar,.uk-nav,form,dialog[open],[role="dialog"],[aria-modal="true"],.modal,.uk-modal,.ant-modal,.v-dialog');
  const scrollable=e=>visible(e)&&!excluded(e)&&!/^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName)&&/(auto|scroll|overlay)/.test(getComputedStyle(e).overflowY)&&e.clientHeight>60&&e.scrollHeight>e.clientHeight+2;
  const area=e=>{const r=e.getBoundingClientRect();return Math.min(r.width,innerWidth)*Math.min(r.height,innerHeight)};
  const biggest=es=>es.filter(scrollable).sort((a,b)=>area(b)-area(a))[0];
  let chosen;
  if(selector){
    let matches;try{matches=[...document.querySelectorAll(selector)]}catch{return {error:'Ungültiger CSS-Inhaltsbereich.'}};
    const visibleMatches=matches.filter(visible);
    if(visibleMatches.length!==1)return {error:'Der gewählte Inhaltsbereich ist nicht eindeutig sichtbar.'};
    const root=visibleMatches[0];
    if(excluded(root))return {error:'Formulare, Navigation und Dialoge sind kein Inhalts-Scrollbereich.'};
    chosen=scrollable(root)?root:biggest([...root.querySelectorAll('*')]);
    for(let p=root.parentElement;!chosen&&p;p=p.parentElement)if(scrollable(p))chosen=p;
  }else{
    if([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"],.modal,.uk-modal,.ant-modal,.v-dialog')].some(visible))return {error:'Zuerst den sichtbaren Hinweis schließen.'};
    chosen=biggest([...document.querySelectorAll('body *')]);
  }
  if(!chosen)chosen=document.scrollingElement;
  if(!chosen)return {error:'Kein bestätigter Scrollbereich vorhanden.'};
  const path=e=>{
    if(e===document.documentElement)return 'html';
    if(e===document.body)return 'body';
    let parts=[];
    for(let n=e;n&&n.nodeType===1;n=n.parentElement){
      if(n.id){const id='#'+CSS.escape(n.id);if(document.querySelectorAll(id).length===1){parts.unshift(id);break}}
      const tag=n.tagName.toLowerCase();const siblings=n.parentElement?[...n.parentElement.children].filter(x=>x.tagName===n.tagName):[];
      parts.unshift(tag+(siblings.length>1?':nth-of-type('+(siblings.indexOf(n)+1)+')':''));
      if(n===document.body)break;
    }
    return parts.join(' > ');
  };
  const before=chosen.scrollTop, height=chosen.scrollHeight, client=chosen.clientHeight;
  const amount=Math.max(100,Math.floor((chosen===document.scrollingElement?innerHeight:client)*.8));
  chosen.scrollTop=direction==='down'?Math.min(height-client,before+amount):Math.max(0,before-amount);
  return {before,initial_height:height,scroll_height:height,client_height:client,selector:path(chosen),direction};
}'''


async def scroll_region(page, direction, selector=None):
    if direction not in ('up', 'down'):
        raise ValueError('Scrollrichtung muss up oder down sein.')
    if selector is not None and (not isinstance(selector,str) or not selector.strip() or len(selector)>600 or any(ord(c)<32 for c in selector)):
        raise ValueError('Ungültiger CSS-Inhaltsbereich.')
    result = await page.evaluate(_SCROLL, {'direction':direction, 'selector':selector})
    if result.get('error'):
        raise ValueError(result['error'])
    previous = None; stable = 0; after = None
    for attempt in range(12):
        await page.wait_for_timeout(125)
        after = await page.evaluate('''selector=>{
          const e=document.querySelector(selector);if(!e)return null;
          return {after:e.scrollTop,scroll_height:e.scrollHeight,client_height:e.clientHeight,
            loading:[...e.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(n=>n.getClientRects().length)};
        }''', result['selector'])
        if after is None:
            raise ValueError('Der bestätigte Inhaltsbereich wurde während des Ladens ersetzt.')
        sample = (round(after['after']), after['scroll_height'])
        stable = stable + 1 if sample == previous and not after['loading'] else 0
        previous = sample
        if attempt >= 3 and stable >= 2:
            break
    result.update({k:v for k,v in after.items() if k != 'loading'})
    result['progress'] = abs(result['after']-result['before']) > 1 or result['scroll_height'] != result['initial_height']
    result['at_end'] = result['after']+result['client_height'] >= result['scroll_height']-2
    result['at_start'] = result['after'] <= 1
    return result
