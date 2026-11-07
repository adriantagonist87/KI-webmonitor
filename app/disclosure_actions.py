"""Open observed native/ARIA disclosures; inspect never changes the live DOM.

The caller retains the network request guard and archives visible content after
opening. ARIA controls require a reciprocal, local heading/panel relationship.
A missing ARIA target has a narrow, structural document-accordion fallback.
"""
import unicodedata


class DisclosureError(ValueError):
    """A short failure plus bounded, observed recovery choices, kept separately."""

    def __init__(self,message,details=None):
        super().__init__(message)
        self.details=details or {}


class DisclosureNotFound(DisclosureError):
    """No visible disclosure match; caller may try its existing menu guard."""


_LEGACY_DOM = r'''
    // A legacy download-list accordion may ship broken, duplicated ARIA IDs.
    // Its local structure and expanded/visibility state still must agree. This
    // does not authorize arbitrary buttons, general menus, or form panels.
    const legacyVisible=e=>{
        if(!e||!e.getClientRects().length)return false;
        const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
        for(let n=e;n;n=n.parentElement){
            const s=getComputedStyle(n);
            if(n.hidden||n.getAttribute('aria-hidden')==='true'||s.display==='none'||
               s.visibility==='hidden'||s.visibility==='collapse'||s.contentVisibility==='hidden'||
               Number(s.opacity)===0)return false;
        }
        return true;
    };
    const legacyPanel=c=>{
        if(c.tagName!=='BUTTON'||c.closest('form,dialog,nav,aside,[role="dialog"],[role="navigation"],[role="menu"]')||
           c.form||c.hasAttribute('href')||c.hasAttribute('formaction')||c.hasAttribute('formmethod')||
           c.hasAttribute('formtarget')||c.hasAttribute('formnovalidate')||
           (c.getAttribute('aria-haspopup')&&!['false'].includes(c.getAttribute('aria-haspopup')))||
           !['','button'].includes((c.getAttribute('type')||'').toLowerCase())||
           [...c.querySelectorAll('*')].some(e=>e.tagName!=='SPAN'||e.matches('[role],[tabindex],[contenteditable]')))
            return null;
        if(/\b(admin|administration|konto|account|abmelden|logout|signout|löschen|loeschen|delete|remove|kündigen|kuendigen|unsubscribe|checkout|kaufen|bestellen|purchase|payment|bezahlen)\b/i.test(c.innerText||''))return null;
        const expanded=c.getAttribute('aria-expanded'),ids=(c.getAttribute('aria-controls')||'').trim().split(/\s+/).filter(Boolean);
        if(!['true','false'].includes(expanded)||ids.length!==1||ids[0].length>400||
           /[\u0000-\u001f\u007f]/.test(ids[0])||document.querySelectorAll('#'+CSS.escape(ids[0])).length!==0)return null;
        const heading=c.parentElement,header=heading?.parentElement,outer=header?.parentElement;
        if(!heading||!/^H[2-6]$/.test(heading.tagName)||heading.children.length!==1||heading.firstElementChild!==c||
           header?.tagName!=='DIV'||header.children.length!==1||header.firstElementChild!==heading||
           outer?.tagName!=='SECTION'||outer.children.length!==2||outer.firstElementChild!==header||
           outer.closest('form,dialog,nav,aside,[role="dialog"],[role="navigation"],[role="menu"]'))return null;
        const panel=header.nextElementSibling;
        if(panel?.tagName!=='DIV'||panel.hasAttribute('aria-labelledby')||panel.hasAttribute('aria-controls')||
           panel.matches('[role],[contenteditable]')||
           panel.querySelector('form,button,input,textarea,select,iframe,object,embed,dialog,nav,aside,[role],[contenteditable]'))return null;
        const links=[...panel.querySelectorAll('a[href]')];
        if(!links.length||links.some(a=>{
            try{const u=new URL(a.getAttribute('href'),document.baseURI);
                return !['http:','https:'].includes(u.protocol)||u.username||u.password||
                    !/\.(pdf|docx?|xlsx?|txt|csv|xml)$/i.test(u.pathname);
            }catch{return true;}
        }))return null;
        const shown=legacyVisible(panel);
        if((expanded==='true')!==shown)return null;
        return {control:c,panel,outer,expanded,shown};
    };
    const legacyUniqueId=e=>{
        const id=e?.id;
        if(!id||id.length>400||/[\u0000-\u001f\u007f]/.test(id)||
           document.querySelectorAll('#'+CSS.escape(id)).length!==1)return null;
        for(const n of document.querySelectorAll('[aria-controls],[aria-labelledby]')){
            for(const attr of ['aria-controls','aria-labelledby'])
                if((n.getAttribute(attr)||'').trim().split(/\s+/).includes(id))return null;
        }
        return id;
    };
    const legacyPath=e=>{
        const parts=[];
        for(let n=e;n&&n.nodeType===1;n=n.parentElement){
            const id=legacyUniqueId(n);
            if(id){parts.unshift('#'+CSS.escape(id));break;}
            const peers=n.parentElement?[...n.parentElement.children].filter(p=>p.tagName===n.tagName):[n];
            parts.unshift(n.localName+(peers.length>1?':nth-of-type('+(peers.indexOf(n)+1)+')':''));
        }
        const result=parts.join(' > ');
        if(result.length>240)return null;
        const matches=document.querySelectorAll(result);
        return matches.length===1&&matches[0]===e?result:null;
    };
'''


_RESOLVE = r'''query => {''' + _LEGACY_DOM + r'''
    const clean=s=>(s||'').normalize('NFC').replace(/\p{Cf}/gu,'').replace(/\s+/g,' ').trim();
    const normalize=s=>clean(s).toLocaleLowerCase('de');
    const actual=s=>clean(s.innerText||'');
    const base=s=>normalize(s).replace(/\s*\(\s*[0-9]+\s*\)\s*$/,'').trim();
    const counter=s=>/\(\s*[0-9]+\s*\)\s*$/.test(normalize(s));
    const visible=e=>{
        if(!e)return false;
        const r=e.getBoundingClientRect();
        if(!e.getClientRects().length||r.width<=0||r.height<=0)return false;
        for(let n=e;n;n=n.parentElement){
            const s=getComputedStyle(n);
            if(n.hidden||n.getAttribute('aria-hidden')==='true'||s.display==='none'||
               s.visibility==='hidden'||s.visibility==='collapse'||
               s.contentVisibility==='hidden'||Number(s.opacity)===0)return false;
        }
        return true;
    };
    const uniqueId=e=>{
        const id=e?.id;
        if(!id||id.length>400||/[\u0000-\u001f\u007f]/.test(id))return null;
        return document.querySelectorAll('#'+CSS.escape(id)).length===1?id:null;
    };
    const controlledIds=new Set();
    for(const e of document.querySelectorAll('[aria-controls],[aria-labelledby]')){
        for(const attr of ['aria-controls','aria-labelledby'])
            for(const id of (e.getAttribute(attr)||'').trim().split(/\s+/))if(id)controlledIds.add(id);
    }
    const path=(e,stable=false)=>{
        const parts=[];
        for(let n=e;n&&n.nodeType===1;n=n.parentElement){
            const id=uniqueId(n),part=id&&(!stable||!controlledIds.has(id))?'#'+CSS.escape(id):null;
            if(part&&[part,...parts].join(' > ').length<=240){parts.unshift(part);break;}
            const siblings=n.parentElement?[...n.parentElement.children].filter(p=>p.tagName===n.tagName):[n];
            parts.unshift(n.localName+(siblings.length>1?':nth-of-type('+(siblings.indexOf(n)+1)+')':''));
        }
        const result=parts.join(' > ');
        if(result.length>240)return null;
        const targets=[...document.querySelectorAll(result)];
        return targets.length===1&&targets[0]===e?result:null;
    };
    let scope=document;
    if(query.container_selector){
        let nodes;
        try{nodes=document.querySelectorAll(query.container_selector);}catch{return {error:'Ungültiger Kontextselektor.'};}
        if(nodes.length!==1||!visible(nodes[0]))return {error:'Der Kontext muss ein eindeutiger sichtbarer echter Container sein.'};
        scope=nodes[0];
    }
    const all=[...document.querySelectorAll('details,button[aria-controls][aria-expanded],[role="button"][aria-controls][aria-expanded]')];
    if(all.length>2000)return {error:'Zu viele Klappbereiche für eine eindeutige Auswahl.'};
    const candidates=all.map(e=>e.tagName==='DETAILS'
        ?{details:e,control:[...e.children].find(n=>n.tagName==='SUMMARY'),type:'native'}
        :{control:e,type:'aria'})
        .filter(item=>item.control&&visible(item.control))
        .map(item=>({...item,label:actual(item.control)}));
    const wantedNormal=normalize(query.label),wantedHasCounter=counter(query.label);
    let selectedControl=null;
    // Private read-only enumeration selects one actual DOM control, then runs
    // exactly the same safety checks as normal label/context resolution below.
    if(query._control_selector){
        const nodes=document.querySelectorAll(query._control_selector);
        if(nodes.length!==1)return {error:'Beobachteter Klappknopf ist nicht eindeutig.'};
        selectedControl=nodes[0];
    }
    const matches=candidates.filter(item=>scope.contains(item.control)&&(!selectedControl||item.control===selectedControl)&&
        (normalize(item.label)===wantedNormal||(!wantedHasCounter&&base(item.label)===wantedNormal)));
    if(!matches.length)return {missing:true};
    if(matches.length!==1)return {error:'Der Klappbereich ist mit dieser Beschriftung nicht eindeutig.'};
    const selected=matches[0],c=selected.control;
    if(selected.label.length>200||!selected.label)return {error:'Die sichtbare Beschriftung ist zu lang oder leer.'};
    if(/\b(admin|administration|konto|account|abmelden|logout|signout|löschen|loeschen|delete|remove|kündigen|kuendigen|unsubscribe|checkout|kaufen|bestellen|purchase|payment|bezahlen)\b/i.test(selected.label))
        return {error:'Administrative oder transaktionsbezogene Klappknöpfe sind ausgeschlossen.'};
    const active='a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable]:not([contenteditable="false"])';
    const inertAnchor=e=>e.tagName==='A'&&e.childNodes.length===0&&
        [...e.attributes].every(a=>['id','name'].includes(a.name.toLowerCase()))&&
        [e.getAttribute('id'),e.getAttribute('name')].some(value=>value&&value.trim());
    const nestedActive=[...c.querySelectorAll(active)].some(e=>!inertAnchor(e));
    if(c.closest('form')||c.form||c.hasAttribute('href')||c.matches('a,[role="link"]')||nestedActive)
        return {error:'Dieser Klappbereich enthält ein Formular, einen Link oder ein anderes aktives Bedienelement.'};
    if(selected.type==='native'){
        const d=selected.details,selector=path(d);
        if(!selector)return {error:'Für diesen Klappbereich fehlt ein kurzer eindeutiger echter CSS-Pfad.'};
        const ownId=uniqueId(d);
        let identity;
        if(ownId)identity=JSON.stringify(['native-details-v1','details-id',ownId]);
        else{
            let anchor=d.parentElement,anchorId=null;
            while(anchor){anchorId=uniqueId(anchor);if(anchorId)break;anchor=anchor.parentElement;}
            const peers=candidates.filter(item=>item.type==='native'&&(!anchor||anchor.contains(item.details)));
            const semantic=base(selected.label),unique=peers.filter(item=>base(item.label)===semantic).length===1;
            identity=JSON.stringify(['native-details-v1',anchorId?'ancestor-id':'document',anchorId||'',
                unique?'summary':'exact-summary',unique?semantic:normalize(selected.label)]);
            if(peers.filter(item=>normalize(item.label)===normalize(selected.label)).length>1){
                const specific=path(d,true);
                if(!specific)return {error:'Für gleich beschriftete native Bereiche fehlt eine eindeutige äußere Zuordnung.'};
                identity=JSON.stringify(['native-details-v1','outer-container',specific,'exact-summary',normalize(selected.label)]);
            }
        }
        return {label:selected.label,selector,control_selector:path(c),identity,kind:'disclosure',
            disclosure_type:'native',container_selector:query.container_selector||null,context_selector:path(d,true),open:d.open};
    }
    const expanded=c.getAttribute('aria-expanded');
    const ids=(c.getAttribute('aria-controls')||'').trim().split(/\s+/).filter(Boolean);
    const legacy=legacyPanel(c);
    if(legacy){
        if(scope!==document&&(!scope.contains(legacy.panel)||scope===legacy.panel||scope===c||scope===c.parentElement))
            return {error:'Der lokale Dokumentbereich gehört nicht zum gewählten Kontext.'};
        const selector=legacyPath(legacy.panel),controlSelector=legacyPath(c),outer=legacyPath(legacy.outer);
        if(!selector||!controlSelector||!outer)return {error:'Für den lokalen Dokumentbereich fehlt ein eindeutiger CSS-Pfad.'};
        let anchor=legacy.outer.parentElement,anchorId=null;
        while(anchor){anchorId=legacyUniqueId(anchor);if(anchorId)break;anchor=anchor.parentElement;}
        const peers=candidates.filter(item=>item.type==='aria'&&legacyPanel(item.control)&&(!anchor||anchor.contains(item.control)));
        const semantic=base(selected.label),unique=peers.filter(item=>base(item.label)===semantic).length===1;
        const identity=JSON.stringify(['local-document-disclosure-v1',anchorId?'ancestor-id':'document',anchorId||'',
            unique?'label':'outer-label',unique?semantic:outer+'|'+normalize(selected.label)]);
        return {label:selected.label,selector,control_selector:controlSelector,identity,kind:'disclosure',
            disclosure_type:'local-document',container_selector:outer,open:legacy.shown};
    }
    if(!['true','false'].includes(expanded)||ids.length!==1||!uniqueId(c))
        return {error:'Kein eindeutig bestätigter ARIA-Klappknopf.'};
    const panels=document.querySelectorAll('#'+CSS.escape(ids[0]));
    if(panels.length!==1)return {error:'Das kontrollierte ARIA-Inhaltsfeld ist nicht eindeutig vorhanden.'};
    const panel=panels[0],heading=c.closest('h1,h2,h3,h4,h5,h6,[role="heading"]');
    if(!heading||panel===c||panel.contains(c)||heading.contains(panel)||
       panel.closest('form,dialog,[role="dialog"]')||
       panel.matches('a,button,input,textarea,select,form,nav,aside,[role="menu"],[role="navigation"],[role="button"],[role="link"]')||
       (c.getAttribute('aria-haspopup')&&!['false'].includes(c.getAttribute('aria-haspopup'))))
        return {error:'Kein sicherer lokaler Überschriften-/Inhaltsbereich für diesen ARIA-Knopf.'};
    const labels=(panel.getAttribute('aria-labelledby')||'').trim().split(/\s+/).filter(Boolean);
    const reciprocal=labels.some(id=>{
        const nodes=document.querySelectorAll('#'+CSS.escape(id));
        return nodes.length===1&&(nodes[0]===c||nodes[0]===heading);
    });
    if(!reciprocal)return {error:'Der ARIA-Inhaltsbereich bestätigt seinen Öffnungsknopf nicht.'};
    let common=heading.parentElement,depth=0;
    while(common&&!common.contains(panel)&&depth++<4)common=common.parentElement;
    if(!common||!common.contains(panel)||common===document.body||common===document.documentElement||
       common.matches('main,nav,aside,form,dialog,[role="main"],[role="dialog"]')||
       (scope!==document&&(!scope.contains(panel)||scope===panel||scope===c||scope===heading)))
        return {error:'ARIA-Überschrift und Inhalt gehören nicht zu einem bestätigten lokalen Klappbereich.'};
    let anchor=common,anchorId=null;
    while(anchor){
        const id=uniqueId(anchor);
        if(id&&!controlledIds.has(id)){anchorId=id;break;}
        anchor=anchor.parentElement;
    }
    const peers=candidates.filter(item=>item.type==='aria'&&(!anchor||anchor.contains(item.control)));
    const semantic=base(selected.label),unique=peers.filter(item=>base(item.label)===semantic).length===1;
    const outer=anchorId?'#'+CSS.escape(anchorId):path(common,true);
    const specific=unique?outer:path(common,true);
    if(!specific)return {error:'Für den ARIA-Kontext fehlt eine stabile eindeutige äußere Zuordnung.'};
    const selector=path(panel),controlSelector=path(c);
    if(!selector||!controlSelector)return {error:'Für den ARIA-Klappbereich fehlt ein eindeutiger echter CSS-Pfad.'};
    const identity=JSON.stringify(['aria-disclosure-v1',anchorId&&unique?'ancestor-id':'outer-container',
        anchorId&&unique?anchorId:specific,unique?'label':'exact-label',unique?semantic:normalize(selected.label)]);
    return {label:selected.label,selector,control_selector:controlSelector,identity,kind:'disclosure',
        disclosure_type:'aria',container_selector:specific,open:expanded==='true'&&visible(panel)};
}'''


_OPTIONS = r'''query => {''' + _LEGACY_DOM + r'''
    const resolve=('''+_RESOLVE+r''');
    const clean=s=>(s||'').normalize('NFC').replace(/\p{Cf}/gu,'').replace(/\s+/g,' ').trim();
    const normalize=s=>clean(s).toLocaleLowerCase('de');
    const base=s=>normalize(s).replace(/\s*\(\s*[0-9]+\s*\)\s*$/,'').trim();
    const nodes=[...document.querySelectorAll('details,button[aria-controls][aria-expanded],[role="button"][aria-controls][aria-expanded]')];
    if(nodes.length>2000)return {items:[],count:0,returned:0,truncated:true,error:'Zu viele Klappbereiche für eine sichere Auflistung.'};
    const options=[];
    for(const node of nodes){
        const control=node.tagName==='DETAILS'?[...node.children].find(e=>e.tagName==='SUMMARY'):node;
        if(!control||!legacyVisible(control))continue;
        const label=clean(control.innerText||'');
        if(!label||label.length>200)continue;
        const controlSelector=legacyPath(control);
        if(!controlSelector)continue;
        const resolved=resolve({label,container_selector:null,_control_selector:controlSelector});
        if(resolved.error||resolved.missing)continue;
        const container=resolved.container_selector||resolved.context_selector;
        if(!container)continue;
        // Advertise only choices that can be passed straight back through the
        // public label/context API without selecting a sibling or guessing IDs.
        const confirmed=resolve({label,container_selector:container});
        if(confirmed.error||confirmed.missing||confirmed.control_selector!==resolved.control_selector)continue;
        const outer=document.querySelector(container);let heading='';
        for(let n=outer,depth=0;n&&depth<5;n=n.parentElement,depth++){
            const context=[...n.children].find(e=>e.matches('h1,h2,h3,h4,h5,h6,[role="heading"]')&&
                !e.contains(control)&&legacyVisible(e));
            if(context){heading=clean(context.innerText||'').slice(0,160);break;}
        }
        options.push({label:resolved.label,container_selector:container,kind:'disclosure',
            disclosure_type:resolved.disclosure_type,expanded:resolved.open,heading});
    }
    let selected=options;
    if(query.label){
        const normal=normalize(query.label),hasCounter=/\(\s*[0-9]+\s*\)\s*$/.test(normal);
        const matches=options.filter(item=>normalize(item.label)===normal||(!hasCounter&&base(item.label)===normal));
        if(matches.length)selected=matches;
    }
    return {items:selected.slice(0,query.limit),count:selected.length,
        returned:Math.min(selected.length,query.limit),truncated:selected.length>query.limit};
}'''


async def _disclosure_options(page,limit,label=None):
    if type(limit) is not int or not 1<=limit<=40:
        raise ValueError('Die Auflistungsgrenze muss zwischen 1 und 40 liegen.')
    return await page.evaluate(_OPTIONS,{'limit':limit,'label':label})


async def list_disclosures(page,limit=40):
    """List bounded, safe live controls with round-trip verified outer contexts.

    Listing reads the DOM without opening, clicking, or exposing hidden panel
    content. A choice is returned only if the ordinary resolver accepts it.
    """
    return await _disclosure_options(page,limit)


def _query(label, container_selector):
    if (not isinstance(label,str) or not label.strip() or len(label)>200
            or any(ord(c)<32 and c not in '\n\r\t' for c in label)):
        raise ValueError('Eine kurze sichtbare Beschriftung ist erforderlich.')
    if container_selector is not None and (not isinstance(container_selector,str)
            or not container_selector.strip() or len(container_selector)>240
            or any(ord(c)<32 for c in container_selector)):
        raise ValueError('Ein kurzer beobachteter Kontextselektor ist erforderlich.')
    cleaned=''.join(c for c in unicodedata.normalize('NFC',label) if unicodedata.category(c)!='Cf')
    return {'label':' '.join(cleaned.split()),'container_selector':container_selector}


async def inspect_disclosure(page,label,container_selector=None):
    """Resolve current selectors/identity/open state without clicking or opening."""
    result=await page.evaluate(_RESOLVE,_query(label,container_selector))
    if result.get('missing') or result.get('error'):
        choices=await _disclosure_options(page,8,label)
        details={'contexts':choices['items'],'context_count':choices['count'],
                 'contexts_truncated':choices['truncated']}
        if result.get('missing'):
            raise DisclosureNotFound('Kein passender sichtbarer nativer oder ARIA-Klappbereich vorhanden.',details)
        raise DisclosureError(result['error'],details)
    return result


async def legacy_base_exclusions(page):
    """Read-only CSS paths of structurally verified legacy document panels.

    The base serializer uses this exact resolver predicate, including actual
    visibility and its ARIA-state agreement. No hidden source text is returned.
    """
    script=r'''() => {''' + _LEGACY_DOM + r'''
        const controls=[...document.querySelectorAll('button[aria-controls][aria-expanded]')];
        if(controls.length>2000)throw new Error('Legacy accordion limit');
        const selectors=[];
        for(const c of controls){
            if(!legacyVisible(c))continue;
            const item=legacyPanel(c);if(!item)continue;
            const control=legacyPath(c),panel=legacyPath(item.panel);
            if(control&&panel)selectors.push(control,panel);
        }
        return [...new Set(selectors)];
    }'''
    result=await page.evaluate(script)
    if not isinstance(result,list) or len(result)>4000 or any(not isinstance(s,str) or not s or len(s)>240 for s in result):
        raise RuntimeError('Lokale Dokumentbereiche konnten nicht sicher zugeordnet werden.')
    return result


async def open_disclosure(page,label,container_selector=None):
    """Open a confirmed disclosure once; existing open sections stay untouched.

    Repeated labels require an actually observed unique outer container. ARIA
    identity excludes generated control/panel IDs; only current selectors use
    those IDs. Native scope identities retain their existing format.
    """
    result=await inspect_disclosure(page,label,container_selector)
    if not result['open']:
        current=await inspect_disclosure(page,label,container_selector)
        if current['identity']!=result['identity']:
            raise ValueError('Der bestätigte Klappbereich wurde vor dem Öffnen verändert.')
        if result['disclosure_type']=='native' and current['selector']!=result['selector']:
            raise ValueError('Der bestätigte native Klappbereich wurde vor dem Öffnen verändert.')
        result=current
        await page.locator(result['control_selector']).click(timeout=4000)
    query=_query(label,result.get('container_selector') or container_selector)
    try:
        await page.wait_for_function('''args=>{
            const resolve=('''+_RESOLVE+''');
            const current=resolve(args.query);
            return !current.error&&!current.missing&&current.identity===args.identity&&current.open;
        }''',arg={'query':query,'identity':result['identity']},timeout=4000)
        result=await inspect_disclosure(page,label,query['container_selector'])
    except Exception:
        raise ValueError('Der bestätigte Klappbereich wurde nach dem Öffnen nicht sichtbar.') from None
    return {key:result[key] for key in ('label','selector','identity','kind','disclosure_type','container_selector')}


__all__=['open_disclosure','inspect_disclosure','list_disclosures','legacy_base_exclusions','DisclosureError','DisclosureNotFound']
