"""Task-local privacy filters for configured website authentication.

No vault, network, page mutation or model access. The controller binds only the
current project's values and resets the token in its finally block.
"""
import re
import html as html_escape
import json
from urllib.parse import quote, quote_plus
from contextvars import ContextVar, Token
from dataclasses import dataclass
from bs4 import BeautifulSoup, Comment

__all__ = ['bind','reset','redact_text','redact_payload','sanitize_html',
           'screenshot_masks','contains_auth_form','contains_exposed_subscriber']

NUMBER_MASK = '[ABONUMMER ENTFERNT]'
POSTCODE_MASK = '[PLZ ENTFERNT]'


@dataclass(frozen=True, repr=False)
class _Bound:
    abonummer: str
    postcode: str
    generic_values: tuple = ()
    auth_form: str = ''
    auth_username: str = ''
    auth_password: str = ''


_ACTIVE = ContextVar('site_auth_privacy', default=None)

# Keep article>header intact: it can contain the actual article headline.
_PRIVATE_SELECTORS = (
    '.page-header,body > header,[role="banner"],footer,body > nav,body > [role="navigation"],'
    '.nav-account,.account,.user-account,[data-account],form,.login,.login-form,'
    '[data-login],[data-auth],'
    '[autocomplete~="username" i],[autocomplete~="current-password" i],'
    '[autocomplete~="new-password" i],[autocomplete~="one-time-code" i],'
    '[autocomplete~="postal-code" i],'
    '[name="login[userIdentifier]"],[name="login[password]"]'
)
_INPUT_SELECTORS = 'input,textarea,[contenteditable]'
_AUTH_SELECTORS = (
    'input[type="password"]:visible,'
    'input[autocomplete~="one-time-code" i]:visible,'
    'input[autocomplete~="current-password" i]:visible'
)
_POSTAL_KEYS = {'plz','postleitzahl','postcode','postalcode','zip','zipcode',
                'loginpassword','password'}
_INLINE = {'a','abbr','b','bdi','bdo','code','em','i','label','mark','s','small',
           'span','strong','sub','sup','u'}


def bind(secret: dict | None, config: dict | None = None) -> Token:
    """Bind a copied secret to this task. No returned value contains its values."""
    if secret is None:
        return _ACTIVE.set(None)
    if not isinstance(secret,dict):
        raise ValueError('Ungültiger Datenschutzkontext.')
    if secret.get('provider')=='generic_form' or isinstance(config,dict) and config.get('provider')=='generic_form':
        values=[]
        for key in ('username','password'):
            value=secret.get(key,'')
            if not isinstance(value,str) or len(value)>4096:raise ValueError('Ungültiger Datenschutzkontext.')
            if value:
                values.extend((value,quote(value,safe=''),quote_plus(value,safe=''),html_escape.escape(value,quote=True),json.dumps(value,ensure_ascii=True)[1:-1]))
        config=config if isinstance(config,dict) else {}
        selectors=tuple(config.get(k,'') for k in ('form_selector','username_selector','password_selector'))
        if any(not isinstance(v,str) or len(v)>500 for v in selectors):raise ValueError('Ungültiger Datenschutzkontext.')
        return _ACTIVE.set(_Bound('','',tuple(sorted(set(values),key=len,reverse=True)),*selectors))
    number=secret.get('abonummer','')
    code=secret.get('postcode',secret.get('plz',''))
    if (not isinstance(number,str) or not isinstance(code,str)
            or number and not re.fullmatch(r'[0-9]{9}',number)
            or code and not re.fullmatch(r'[0-9]{5}',code)):
        raise ValueError('Ungültiger Datenschutzkontext.')
    return _ACTIVE.set(_Bound(number,code))


def reset(token: Token):
    _ACTIVE.reset(token)


def _postal_pattern(code):
    # Labels and separators are mandatory. A bare five-digit technical number
    # must not change just because it equals the user's postal code.
    label=r'(?:PLZ|Postleitzahl|postcode|postal[_-]?code|login(?:\[|%5b)password(?:\]|%5d))'
    return re.compile(r'(?P<label>\b'+label+r'\s*[\"\']?\s*[:=]\s*[\"\']?)'
                      +re.escape(code)+r'(?![0-9])',re.IGNORECASE)


def redact_text(text: str) -> str:
    active=_ACTIVE.get()
    if active is None:
        return text
    if active.generic_values:
        return re.sub('|'.join(re.escape(v) for v in active.generic_values),lambda match:'[ZUGANGSDATEN ENTFERNT]',text)
    if active.abonummer:
        text=text.replace(active.abonummer,NUMBER_MASK)
    if active.postcode:
        text=_postal_pattern(active.postcode).sub(lambda m:m.group('label')+POSTCODE_MASK,text)
    return text


def _postal_key(key):
    if not isinstance(key,str):
        return False
    normalized=re.sub(r'[^a-z]','',key.casefold())
    if normalized in _POSTAL_KEYS:
        return True
    return key.casefold().startswith('data-') and _postal_key(key[5:])


def redact_payload(value):
    """Copy JSON-like model/log data; never alter caller-owned structures."""
    active=_ACTIVE.get()
    if active is None:
        return value
    if isinstance(value,str):
        return redact_text(value)
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            clean_key=redact_text(key) if isinstance(key,str) else key
            if type(key) in (int,float) and (active.abonummer and active.abonummer in str(key) or any(v in str(key) for v in active.generic_values)):
                clean_key=redact_text(str(key))
            clean_item=redact_payload(item)
            if active.postcode and _postal_key(key):
                if isinstance(clean_item,str):
                    clean_item=clean_item.replace(active.postcode,POSTCODE_MASK)
                elif type(clean_item) is int and str(clean_item)==active.postcode:
                    clean_item=POSTCODE_MASK
            if clean_key in result:
                # Different private keys can become the same placeholder. Keep
                # their unrelated payloads instead of silently losing one.
                base=str(clean_key);index=2
                while base+' #'+str(index) in result:
                    index+=1
                clean_key=base+' #'+str(index)
            result[clean_key]=clean_item
        return result
    if isinstance(value,list):
        return [redact_payload(item) for item in value]
    if isinstance(value,tuple):
        return tuple(redact_payload(item) for item in value)
    if type(value) in (int,float) and (active.abonummer and active.abonummer in str(value) or any(v in str(value) for v in active.generic_values)):
        return redact_text(str(value))
    return value


def _redact_attribute(key,value,active):
    if isinstance(value,list):
        return [_redact_attribute(key,item,active) for item in value]
    if not isinstance(value,str):
        return value
    clean=redact_text(value)
    if active.postcode and _postal_key(key):
        clean=clean.replace(active.postcode,POSTCODE_MASK)
    return clean


def _redact_inline_postal_fields(soup,active):
    """Handle labels split by inline tags without masking an unrelated number."""
    if not active.postcode:
        return
    pattern=_postal_pattern(active.postcode)
    for element in list(soup.find_all(['p','li','td','dd','dt','label','span'])):
        if element.parent is None:
            continue
        # Do not combine separate blocks. For example a postal address in one
        # paragraph must never redact a technical value in the next paragraph.
        if any(child.name not in _INLINE for child in element.find_all(True)):
            continue
        text=element.get_text('',strip=False)
        if len(text)<=1000 and pattern.search(text):
            element.clear()
            element.append(redact_text(text))


def sanitize_html(html: str) -> str:
    active=_ACTIVE.get()
    if active is None:
        return html
    soup=BeautifulSoup(html,'html.parser')
    private_selectors=_PRIVATE_SELECTORS+(','+active.auth_form if active.auth_form else '')
    for element in list(soup.select(private_selectors)):
        if element.parent is not None:
            element.decompose()
    for element in list(soup.find_all(['script','style','noscript','template'])):
        element.decompose()
    for comment in list(soup.find_all(string=lambda s:isinstance(s,Comment))):
        comment.extract()
    _redact_inline_postal_fields(soup,active)
    for element in soup.find_all(True):
        for key,value in list(element.attrs.items()):
            if active.abonummer and active.abonummer in key:
                del element.attrs[key]
            else:
                element.attrs[key]=_redact_attribute(key,value,active)
    for node in list(soup.find_all(string=True)):
        clean=redact_text(str(node))
        if clean!=str(node):
            node.replace_with(clean)
    # A final pass also covers unusual tag names, document declarations and
    # attributes serialized by the parser. It never changes the live page.
    return redact_text(str(soup))


async def screenshot_masks(page):
    """Return static locators only; their selector representation has no secret."""
    masks=[]
    active=_ACTIVE.get()
    for frame in page.frames:
        masks.append(frame.locator(_INPUT_SELECTORS))
        if active is not None:
            masks.append(frame.locator(_PRIVATE_SELECTORS))
            if active.auth_form:masks.append(frame.locator(active.auth_form))
    return masks


_FRAME_HIDDEN = r'''(element, privateSelectors) => {
    const box=element.getBoundingClientRect();
    if (!element.getClientRects().length || box.width<=0 || box.height<=0) return true;
    const own=getComputedStyle(element);
    if(own.visibility==='hidden'||own.visibility==='collapse')return true;
    for (let e=element;e;e=e.parentElement) {
        if (e.matches(privateSelectors)) return true;
        const s=getComputedStyle(e);
        if(s.display==='none'||s.contentVisibility==='hidden'||Number(s.opacity)===0)return true;
    }
    return false;
}'''

_EXPOSED_TEXT = r'''({private_selectors,subscribers,max_nodes,max_characters}) => {
    if (!document.body) return false;
    const excluded=new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','INPUT','TEXTAREA','IFRAME','OBJECT','EMBED']);
    let nodes=0,characters=0,limited=false;
    const parts=[];
    const subtreeHidden=e=>{
        const s=getComputedStyle(e);
        return s.display==='none'||s.contentVisibility==='hidden'||Number(s.opacity)===0;
    };
    const textVisible=e=>{
        const visibility=getComputedStyle(e).visibility;
        return visibility!=='hidden'&&visibility!=='collapse';
    };
    const append=text=>{
        characters+=text.length;
        if(characters>max_characters){limited=true;return;}
        parts.push(text);
    };
    const walk=(node,depth)=>{
        if(limited)return;
        if(++nodes>max_nodes||depth>512){limited=true;return;}
        if(node.nodeType===Node.TEXT_NODE){
            if(node.parentElement&&textVisible(node.parentElement))append(node.data);
            return;
        }
        if(node.nodeType!==Node.ELEMENT_NODE)return;
        const tag=node.tagName.toUpperCase();
        if(excluded.has(tag)||node.matches(private_selectors)||subtreeHidden(node))return;
        if(tag==='SELECT'){
            if(!textVisible(node))return;
            const options=node.multiple||node.size>1?[...node.options]:[...node.selectedOptions];
            for(const option of options)if(!subtreeHidden(option)&&textVisible(option))append(option.textContent||'');
            return;
        }
        if(tag==='DETAILS'&&!node.open){
            const summary=[...node.children].find(e=>e.tagName==='SUMMARY');
            if(summary)walk(summary,depth+1);
            return;
        }
        for(const child of node.childNodes)walk(child,depth+1);
    };
    // Document-level hiding applies even though only body text is inspected.
    if(subtreeHidden(document.documentElement))return false;
    walk(document.body,0);
    if(limited)return true;  // Never approve a partially inspected screenshot.
    const text=parts.join('');
    const compact=text.replace(/\s+/g,'');
    return subscribers.some(value=>text.includes(value)||
        (value.replace(/\s+/g,'')&&compact.includes(value.replace(/\s+/g,''))));
}'''


async def contains_exposed_subscriber(page) -> bool:
    """Fail closed on rendered secret text outside the exact screenshot masks.

    Hidden DOM templates are not visible exposure. Ordinary off-screen text is
    still checked because a scoped capture may scroll to it. The reader never
    clones, mutates or constructs DOM nodes; secrets are only evaluation
    arguments, never selectors, script source, return values or log messages.
    """
    active=_ACTIVE.get()
    values=(active.abonummer,) if active and active.abonummer else active.generic_values if active else ()
    if not values:
        return False
    private_selectors=_INPUT_SELECTORS+','+_PRIVATE_SELECTORS+(','+active.auth_form if active.auth_form else '')
    for frame in page.frames:
        current=frame;masked=False
        while getattr(current,'parent_frame',None) is not None:
            element=await current.frame_element()
            try:
                if await element.evaluate(_FRAME_HIDDEN,private_selectors):masked=True;break
            finally:await element.dispose()
            current=current.parent_frame
        if masked:continue
        exposed=await frame.evaluate(_EXPOSED_TEXT,{'private_selectors':private_selectors,
            'subscribers':list(values),'max_nodes':100000,'max_characters':8*1024*1024})
        if exposed:
            return True
    return False


async def contains_auth_form(page) -> bool:
    active=_ACTIVE.get()
    for frame in page.frames:
        if active and active.auth_form and any([await e.is_visible() for e in await frame.locator(active.auth_form).all()]):return True
        if await frame.locator(_AUTH_SELECTORS).count():
            return True
        forms=frame.locator('form')
        for index in range(await forms.count()):
            form=forms.nth(index)
            identifier=form.locator('[name="login[userIdentifier]"]')
            password=form.locator('[name="login[password]"]')
            if await identifier.count() and await password.count():
                # Hidden header login modals do not block a normal content page.
                if (await form.locator('[name="login[userIdentifier]"]:visible').count()
                        or await form.locator('[name="login[password]"]:visible').count()):
                    return True
    return False
