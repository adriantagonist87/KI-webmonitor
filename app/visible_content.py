"""Read-only visible HTML copy before the existing task-local privacy scrubber.

No node is inserted, removed or edited on the live page. Serialization rather
than cloneNode/importNode also avoids constructing custom elements or loading
resources in a detached document. Normal off-screen content is retained: scroll
position, viewport intersection and element height are not visibility tests.
"""
from disclosure_actions import legacy_base_exclusions

MAX_NODES = 100000
MAX_CHARACTERS = 8 * 1024 * 1024

_VISIBLE_HTML = r'''limits => {
    if (!document.documentElement || !document.body)
        throw new Error('No HTML document');
    let nodes = 0, characters = 0;
    const pieces = [];
    const excluded = new Set([
        'SCRIPT','STYLE','NOSCRIPT','TEMPLATE','INPUT','TEXTAREA','SELECT',
        'IFRAME','OBJECT','EMBED','LINK','META','BASE'
    ]);
    const voidTags = new Set([
        'AREA','BASE','BR','COL','EMBED','HR','IMG','INPUT','LINK','META',
        'PARAM','SOURCE','TRACK','WBR'
    ]);
    const escaped = value => String(value).replace(/&/g,'&amp;')
        .replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    const append = value => {
        characters += value.length;
        if (characters > limits.characters) throw new Error('HTML size limit');
        pieces.push(value);
    };
    const attrs = element => {
        let output = '';
        for (const attr of element.attributes) {
            const name = attr.name.toLowerCase();
            if (name === 'value' || name === 'style' || name.startsWith('on')) continue;
            output += ' ' + attr.name + '="' + escaped(attr.value) + '"';
        }
        return output;
    };
    const hidden = element => {
        if (element.hasAttribute('hidden') ||
            (element.getAttribute('aria-hidden') || '').trim().toLowerCase() === 'true')
            return true;
        const style = getComputedStyle(element);
        return style.display === 'none' || style.visibility === 'hidden' ||
            style.visibility === 'collapse' || style.contentVisibility === 'hidden' ||
            Number(style.opacity) === 0;
    };
    // Build exclusions from the real document, before visibility pruning.
    // Merely reading these relationships never clicks, opens or edits a node.
    const disclosureNodes = new Set();
    if (limits.disclosure_base) {
        const legacy=limits.legacy_exclusions||[];
        if(!Array.isArray(legacy)||legacy.length>4000) throw new Error('Disclosure limit');
        for(const selector of legacy){
            const nodes=document.querySelectorAll(selector);
            if(nodes.length!==1) throw new Error('Disclosure region changed');
            disclosureNodes.add(nodes[0]);
        }
        const native=document.querySelectorAll('details');
        const controls=document.querySelectorAll('button[aria-controls][aria-expanded],[role="button"][aria-controls][aria-expanded]');
        if(native.length+controls.length>2000) throw new Error('Disclosure limit');
        for (const details of native) {
            if (Array.from(details.children).some(e => e.tagName === 'SUMMARY'))
                disclosureNodes.add(details);
        }
        for (const control of controls) {
            const ids=(control.getAttribute('aria-controls')||'').trim().split(/\s+/).filter(Boolean);
            const heading=control.closest('h1,h2,h3,h4,h5,h6,[role="heading"]');
            if (!heading || ids.length!==1 || !control.id ||
                !['true','false'].includes(control.getAttribute('aria-expanded')) ||
                control.closest('form') || control.form || control.hasAttribute('href') ||
                document.querySelectorAll('#'+CSS.escape(control.id)).length!==1) continue;
            const matches=document.querySelectorAll('#'+CSS.escape(ids[0]));
            if(matches.length!==1) continue;
            const panel=matches[0];
            const labels=(panel.getAttribute('aria-labelledby')||'').trim().split(/\s+/).filter(Boolean);
            const reciprocal=labels.some(id=>{
                const nodes=document.querySelectorAll('#'+CSS.escape(id));
                return nodes.length===1 && (nodes[0]===control || nodes[0]===heading);
            });
            if(!reciprocal || panel===control || panel.contains(control) || heading.contains(panel) ||
               panel.closest('form,dialog,[role="dialog"]') ||
               panel.matches('main,nav,aside,form,dialog,a,button,input,textarea,select,[role="main"],[role="navigation"],[role="menu"]')) continue;
            let common=heading.parentElement,depth=0;
            while(common && !common.contains(panel) && depth++<4) common=common.parentElement;
            if(!common || !common.contains(panel) || common===document.body || common===document.documentElement ||
               common.matches('main,nav,aside,form,dialog,[role="main"],[role="dialog"]')) continue;
            disclosureNodes.add(control);disclosureNodes.add(panel);
        }
    }
    const visit = (node, depth) => {
        if (++nodes > limits.nodes || depth > 512) throw new Error('HTML node limit');
        if (node.nodeType === Node.TEXT_NODE) {
            append(escaped(node.data));
            return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const tag = node.tagName.toUpperCase();
        if (excluded.has(tag) || disclosureNodes.has(node) || hidden(node)) return;
        const name = node.localName;
        append('<' + name + attrs(node) + '>');
        if (voidTags.has(tag)) return;
        if (tag === 'DETAILS' && !node.open) {
            // A closed disclosure displays only its first direct summary.
            const summary = Array.from(node.children).find(e => e.tagName === 'SUMMARY');
            if (summary) visit(summary, depth + 1);
        } else {
            for (const child of node.childNodes) visit(child, depth + 1);
        }
        append('</' + name + '>');
    };
    if (limits.selection) {
        // This branch observes selected nodes in place. An inactive, structurally
        // confirmed carousel slide contributes only an opaque topology record.
        // Its text/HTML is never serialized until it actually becomes visible.
        const matches=Array.from(document.querySelectorAll(limits.selection));
        if(matches.length>2000) throw new Error('Selection limit');
        const selected=new Set(matches);
        const path = element => {
            const parts=[];
            for(let e=element;e && e!==document.body;e=e.parentElement){
                const parent=e.parentElement;
                if(!parent || parts.length>512) throw new Error('Selection path limit');
                parts.push(e.localName+':nth-child('+(Array.from(parent.children).indexOf(e)+1)+')');
            }
            return parts.reverse().join(' > ');
        };
        const fingerprint = element => {
            // Detect replacement/reordering without returning hidden labels or
            // link values. No state is stored on the live page.
            const value=[element.localName,element.id,element.getAttribute('href')||'',
                (element.className||'').toString().split(/\s+/).filter(c=>
                    !['active','carousel-item-next','carousel-item-prev','carousel-item-start','carousel-item-end'].includes(c)).join(' ')].join('\0');
            let hash=2166136261;
            for(let i=0;i<value.length;i++) hash=Math.imul(hash^value.charCodeAt(i),16777619);
            return value.length+':'+(hash>>>0);
        };
        const permitted = (element, ignoredSlide=null) => {
            for(let e=element;e;e=e.parentElement){
                if(excluded.has(e.tagName) || e.matches('form,dialog,[role="dialog"]')) return false;
                if(e!==ignoredSlide && hidden(e)) return false;
                if(e.tagName==='DETAILS' && !e.open){
                    const summary=Array.from(e.children).find(n=>n.tagName==='SUMMARY');
                    if(!summary || (element!==summary && !summary.contains(element))) return false;
                }
            }
            return true;
        };
        const records=[],carousels=new Map();
        for(let index=0;index<matches.length;index++){
            const element=matches[index];
            let selectedAncestor=false;
            for(let parent=element.parentElement;parent;parent=parent.parentElement){
                if(selected.has(parent)){selectedAncestor=true;break;}
            }
            if(selectedAncestor) continue;
            const slide=element.closest('.carousel-item');
            const inner=slide?.parentElement;
            const carousel=inner?.parentElement;
            const slides=inner && inner.matches('.carousel-inner') && carousel?.matches('.carousel')
                ? Array.from(inner.children).filter(e=>e.matches('.carousel-item')) : [];
            const confirmed=slides.length>1 && slides.length<=30 && permitted(element,slide);
            if(!confirmed && !permitted(element)) continue;
            let carouselPath=null,slideIndex=null;
            if(confirmed){
                carouselPath=path(carousel);slideIndex=slides.indexOf(slide);
                carousels.set(carouselPath,{path:carouselPath,slides:slides.length});
            }
            let fragment=null,content=null;
            if(permitted(element)){
                const start=pieces.length;
                const ancestors=[];
                for(let parent=element.parentElement;parent && parent!==document.body;parent=parent.parentElement) ancestors.push(parent);
                for(const ancestor of ancestors.slice().reverse()) append('<'+ancestor.localName+attrs(ancestor)+'>');
                const contentStart=pieces.length;
                visit(element,0);
                content=pieces.slice(contentStart).join('');
                for(const ancestor of ancestors) append('</'+ancestor.localName+'>');
                fragment=pieces.splice(start).join('');
            }
            records.push({order:index,slot:path(element),identity:fingerprint(element),carousel:carouselPath,slide:slideIndex,html:fragment,content});
        }
        return {items:records,carousels:Array.from(carousels.values()),title:document.title};
    }
    append('<!doctype html><html' + attrs(document.documentElement) + '>');
    append('<head><meta charset="utf-8"><title>' + escaped(document.title) + '</title></head>');
    if (hidden(document.documentElement) || hidden(document.body)) append('<body></body>');
    else visit(document.body, 0);
    append('</html>');
    const html=pieces.join('');
    return limits.disclosure_base ? {html,disclosures:disclosureNodes.size} : html;
}'''


async def visible_html(page):
    """Return an inert HTML copy of currently visible page content.

    Hiding any ancestor prunes its entire subtree, including account/billing
    modules. Closed details bodies and all input/textarea/select elements are
    omitted, while ordinary entries below an inner or outer scroll viewport
    remain. This is not an authentication or secret detector: the caller must
    still run its existing auth guard and site_privacy.sanitize_html afterwards.
    """
    try:
        result = await page.evaluate(_VISIBLE_HTML, {'nodes': MAX_NODES,
                                                    'characters': MAX_CHARACTERS})
    except Exception:
        raise RuntimeError('Sichtbarer Seiteninhalt konnte nicht sicher gelesen werden.') from None
    if not isinstance(result, str) or len(result) > MAX_CHARACTERS:
        raise RuntimeError('Sichtbarer Seiteninhalt überschreitet die sichere Lesegrenze.')
    return result


async def visible_selection(page, selector):
    """Private carousel helper: visible fragments plus content-free topology.

    Inactive slides are identified structurally, never made visible or copied.
    The caller must continue to apply the task-local privacy scrubber.
    """
    if not isinstance(selector,str) or not selector.strip() or len(selector)>240:
        raise ValueError('Ungültiger Inhaltsbereich für Karussellprüfung.')
    try:
        result=await page.evaluate(_VISIBLE_HTML,{'nodes':MAX_NODES,
            'characters':MAX_CHARACTERS,'selection':selector})
    except Exception:
        raise RuntimeError('Karussellinhalt konnte nicht sicher gelesen werden.') from None
    if (not isinstance(result,dict) or not isinstance(result.get('items'),list)
            or not isinstance(result.get('carousels'),list) or len(result['items'])>2000):
        raise RuntimeError('Karussellinhalt überschreitet die sichere Lesegrenze.')
    return result


async def visible_base_html(page):
    """Inert visible HTML outside confirmed native/ARIA disclosure regions.

    None means there are no structurally confirmed disclosures. This does not
    grant click authority, expose hidden content or change the live document.
    """
    try:
        legacy=await legacy_base_exclusions(page)
        result=await page.evaluate(_VISIBLE_HTML, {'nodes':MAX_NODES,
                                                'characters':MAX_CHARACTERS,
                                                'disclosure_base':True,
                                                'legacy_exclusions':legacy})
    except Exception:
        raise RuntimeError('Grundbereich konnte nicht sicher gelesen werden.') from None
    if (not isinstance(result,dict) or not isinstance(result.get('html'),str)
            or len(result['html'])>MAX_CHARACTERS or type(result.get('disclosures')) is not int):
        raise RuntimeError('Grundbereich überschreitet die sichere Lesegrenze.')
    return result['html'] if result['disclosures'] else None


__all__ = ['visible_html','visible_base_html','visible_selection']
