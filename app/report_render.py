"""Render model-authored Markdown and inert formatting, including disclosures."""
from urllib.parse import urlsplit
from bs4 import BeautifulSoup, Comment
from markdown_it import MarkdownIt
from markupsafe import Markup

ALLOWED=set('p br h1 h2 h3 h4 h5 h6 a ul ol li table thead tbody tr th td blockquote strong em s del pre code hr details summary'.split())
ACTIVE=set('script style iframe object embed form input textarea select button svg math img video audio link meta base template'.split())
PARSER=MarkdownIt('commonmark',{'html':True}).enable('table')


def render_report(text):
    soup=BeautifulSoup(PARSER.render(text or ''),'html.parser')
    for c in soup.find_all(string=lambda x:isinstance(x,Comment)): c.extract()
    for e in list(soup.find_all(True)):
        if e.parent is None: continue
        if e.name in ACTIVE:
            e.decompose(); continue
        if e.name not in ALLOWED:
            e.unwrap(); continue
        href=e.get('href') if e.name=='a' else None
        e.attrs.clear()
        if href and not any(ord(c)<32 for c in href):
            try: u=urlsplit(href)
            except ValueError: continue
            if u.scheme in ('http','https') and not u.username and not u.password or not u.scheme and href.startswith(('/','#')) and not href.startswith('//'):
                e['href']=href; e['rel']='noreferrer noopener'
    return Markup(str(soup))
