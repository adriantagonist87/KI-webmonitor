"""Accumulate finite rotating content only after each selected item was visible.

No selector grants permission to reveal hidden nodes. Bootstrap's observed
carousel structure only establishes a finite coverage obligation; every item
still passes the same visible serializer as an ordinary browser read.
"""
from html import escape
from time import monotonic

from visible_content import MAX_CHARACTERS, visible_html, visible_selection

POLICY = 'observed-carousel-visible-v1'
_TRANSIENT_CLASSES = {'active','carousel-item-next','carousel-item-prev',
                     'carousel-item-start','carousel-item-end'}


def _topology(state):
    items=state.get('items')
    carousels=state.get('carousels')
    if (not isinstance(items,list) or not isinstance(carousels,list)
            or len(items)>2000 or len(carousels)>30):
        raise RuntimeError('Karussellstruktur überschreitet die sichere Lesegrenze.')
    topology=[]
    for item in items:
        if (not isinstance(item,dict) or type(item.get('order')) is not int
                or not isinstance(item.get('slot'),str) or not isinstance(item.get('identity'),str)
                or item.get('carousel') is not None and not isinstance(item['carousel'],str)
                or item.get('slide') is not None and type(item['slide']) is not int
                or item.get('html') is not None and not isinstance(item['html'],str)
                or item.get('content') is not None and not isinstance(item['content'],str)):
            raise RuntimeError('Karussellstruktur konnte nicht eindeutig geprüft werden.')
        topology.append((item['order'],item['slot'],item['identity'],item.get('carousel'),item.get('slide')))
    if len({item[1] for item in topology})!=len(topology):
        raise RuntimeError('Karussellstruktur enthält widersprüchliche Positionen.')
    carousel_topology=[]
    for carousel in carousels:
        if (not isinstance(carousel,dict) or not isinstance(carousel.get('path'),str)
                or type(carousel.get('slides')) is not int or not 2<=carousel['slides']<=30):
            raise RuntimeError('Karussellstruktur konnte nicht eindeutig geprüft werden.')
        indexes={entry[4] for entry in topology if entry[3]==carousel['path']}
        if indexes!=set(range(carousel['slides'])):
            raise RuntimeError('Karussell-Auswahl umfasst nicht alle beobachteten Folien; vollständigeren Inhaltsbereich wählen.')
        carousel_topology.append((carousel['path'],carousel['slides']))
    return tuple(topology),tuple(carousel_topology)


def _stable_content(value):
    # Active/transition classes are display state, not a card's subject matter.
    # Parsing is inert: it never constructs or touches live browser elements.
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(value,'html.parser')
    for node in soup.select('.carousel-item'):
        classes=[c for c in node.get('class',[]) if c not in _TRANSIENT_CLASSES]
        if classes:node['class']=classes
        else:node.attrs.pop('class',None)
        node.attrs.pop('aria-hidden',None)
    return str(soup)


async def carousel_html(page, selector, *, max_ms=22000, poll_ms=200):
    """Return complete selected HTML, or refuse to archive a partial rotation.

    ``selection_applied`` is true only when rotation was needed. In that case
    ``html`` already contains exactly the selected nodes, in their original DOM
    order. Positional selectors must not be applied again to that union. Its
    normal source scope remains the original selector, supplied by the caller.
    """
    if type(max_ms) is not int or not 1<=max_ms<=60000 or type(poll_ms) is not int or not 1<=poll_ms<=1000:
        raise ValueError('Ungültige Grenzen für Karussellprüfung.')
    if selector is None:
        return {'html':await visible_html(page),'selection_applied':False,
                'coverage':{'complete':True,'carousels':0,'expected_nodes':0,'observed_nodes':0,'policy':POLICY}}
    state=await visible_selection(page,selector)
    if not state.get('carousels'):
        return {'html':await visible_html(page),'selection_applied':False,
                'coverage':{'complete':True,'carousels':0,'expected_nodes':0,'observed_nodes':0,'policy':POLICY}}
    started=monotonic()
    expected,carousels=_topology(state)
    if not expected:
        raise RuntimeError('Karussell-Auswahl enthält keine lesbaren Einträge.')
    seen={};size=0
    while True:
        topology,current_carousels=_topology(state)
        if (topology,current_carousels)!=(expected,carousels):
            raise RuntimeError('Karussellstruktur hat sich während der Prüfung geändert; keine unvollständige Quelle archiviert.')
        for item in state['items']:
            if item['html'] is None:continue
            if item.get('content') is None:
                raise RuntimeError('Karussellinhalt konnte nicht sicher gelesen werden.')
            content=_stable_content(item['content'])
            previous=seen.get(item['slot'])
            if previous is not None:
                if previous['content']!=content:
                    raise RuntimeError('Karusselleintrag hat sich während der Prüfung geändert; Quelle erneut prüfen.')
            else:
                size+=len(item['html'])
                if size>MAX_CHARACTERS:
                    raise RuntimeError('Karussellinhalt überschreitet die sichere Lesegrenze.')
                seen[item['slot']]={'html':item['html'],'content':content}
        if len(seen)==len(expected):
            html='<!doctype html><html><head><meta charset="utf-8"><title>'+escape(state.get('title',''))+'</title></head><body>'
            html+=''.join(seen[item[1]]['html'] for item in expected)
            html+='</body></html>'
            if len(html)>MAX_CHARACTERS:
                raise RuntimeError('Karussellinhalt überschreitet die sichere Lesegrenze.')
            return {'html':html,'selection_applied':True,
                    'coverage':{'complete':True,'carousels':len(carousels),'expected_nodes':len(expected),
                                'observed_nodes':len(seen),'policy':POLICY}}
        elapsed=(monotonic()-started)*1000
        if elapsed>=max_ms:
            raise RuntimeError('Karussell nicht vollständig sichtbar geworden ('+str(len(seen))+'/'+str(len(expected))+'); keine unvollständige Quelle archiviert.')
        await page.wait_for_timeout(min(poll_ms,max_ms-elapsed))
        state=await visible_selection(page,selector)


__all__=['carousel_html','POLICY']
