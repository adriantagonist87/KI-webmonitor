"""Site-neutral content extraction. The model can select a content region."""
import copy
import re
from urllib.parse import urljoin, urldefrag, urlsplit
from bs4 import BeautifulSoup, Comment
from document_inventory import declared_file, document_mime
from document_types import DOC_EXT


def _download_hints(anchor):
    hints={}
    if anchor.has_attr('download'):
        value=anchor.get('download')
        hints['download']=(value.strip() if isinstance(value,str) and 0<len(value.strip())<=200
            and not any(ord(character)<32 or ord(character)==127 for character in value) else True)
    mime=document_mime(anchor.get('type'))
    if mime is not None:hints['type']=mime
    return hints


def _file_anchor(anchor,url):
    return (urlsplit(urljoin(url,anchor['href'])).path.casefold().endswith(DOC_EXT)
            or declared_file(_download_hints(anchor)))


def _link_metadata(anchor, url):
    target = urldefrag(urljoin(url, anchor['href']))[0]
    label = anchor.get_text(' ', strip=True)
    context = anchor.parent.get_text(' ', strip=True)
    generic = {'download', 'herunterladen', 'pdf', 'pdf-download',
               'datei herunterladen', 'dokument herunterladen'}
    if label.casefold() in generic and _file_anchor(anchor,url):
        for parent in list(anchor.parents)[:4]:
            if parent.name in ('html', 'body', 'main', '[document]'):
                break
            headings = parent.select('h2,h3,h4')
            urls = {urldefrag(urljoin(url, a['href']))[0] for a in parent.select('a[href]')
                    if _file_anchor(a,url)}
            if len(headings) == 1 and urls == {target}:
                title = headings[0].get_text(' ', strip=True)
                if title:
                    label = title
                    context = parent.get_text(' ', strip=True)
                    break
    return {'url': target, 'label': label[:200], 'context': context[:240], **_download_hints(anchor)}


def editorial_content(html, url, selector=None, *, preselected=False):
    soup = BeautifulSoup(html, 'html.parser')
    regions, seen = [], set()
    for h in soup.select('h1,h2')[:30]:
        for p in list(h.parents)[:4]:
            if p.name in ('body','html','[document]'): continue
            ident=p.get('id',''); classes=[c for c in p.get('class',[])[:2] if re.fullmatch(r'[A-Za-z_][\w-]*',c)]
            css='#'+ident if re.fullmatch(r'[A-Za-z_][\w-]*',ident) else p.name+('.'+'.'.join(classes) if classes else '')
            if css in seen or (not ident and not classes): continue
            seen.add(css); regions.append({'selector':css,'heading':h.get_text(' ',strip=True)[:160],'characters':len(p.get_text(' ',strip=True))})
    if selector:
        if not isinstance(selector,str) or len(selector)>240: raise ValueError('Invalid content selector')
        try: nodes=soup.select(selector)
        except Exception as exc: raise ValueError('Invalid content selector') from exc
        if not nodes: raise ValueError('Selected content region not found; choose again')
        root=BeautifulSoup('<div></div>','html.parser').div; chosen={id(n) for n in nodes}
        for n in nodes:
            if not any(id(p) in chosen for p in n.parents): root.append(copy.deepcopy(n))
        scope='css:'+selector
    else:
        # A verified selection union can contain several inert ancestor/main
        # wrappers. Taking only the first main would discard later entries.
        main=None if preselected else soup.select_one('main,[role="main"]')
        root=copy.deepcopy(main or soup.body or soup)
        scope='main-content-v2' if main else 'body-content-v2'
    for e in root.select('script,style,noscript,input,textarea,select,template,nav,footer,aside,[role="complementary"],[role="dialog"],.advertisement,.ad-container,.cookie-banner,.newsletter-signup'):
        e.decompose()
    for e in root.find_all(True):
        for attr in list(e.attrs):
            if attr.startswith('on') or attr in ('value','nonce'): del e.attrs[attr]
    for c in root.find_all(string=lambda s:isinstance(s,Comment)): c.extract()
    title=soup.title.get_text(' ',strip=True) if soup.title else ''
    if root.find('h1'): title=root.h1.get_text(' ',strip=True)
    links=[_link_metadata(a,url) for a in root.select('a[href]')]
    text=root.get_text('\n',strip=True)
    if not text: raise ValueError('No readable content in selected region')
    root['data-kiwm-scope']=scope
    return {'scope':scope,'html':str(root),'text':text,'links':[a['url'] for a in links],
            'metadata':{'title':title,'selected_scope':selector or 'auto','regions':regions[:18],
                        'headings':[h.get_text(' ',strip=True) for h in root.select('h1,h2,h3')][:80],
                        'paragraphs':[p.get_text(' ',strip=True) for p in root.select('p') if p.get_text(' ',strip=True)][:8], 'links':links}}
