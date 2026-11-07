"""Check model-authored report links against exact URLs in archived evidence."""
import json
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from report_render import render_report


def _evidence_url(value):
    if not isinstance(value,str) or not re.match(r'^https?://',value,re.IGNORECASE):
        return False
    if any(character.isspace() or ord(character)<32 or ord(character)==127 for character in value):
        return False
    try:
        parsed=urlsplit(value)
        return (parsed.scheme in ('http','https') and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and (parsed.port is None or 1<=parsed.port<=65535))
    except ValueError:
        return False


def _items(value):
    return value if isinstance(value,(list,tuple)) else ()


def _add(value,result):
    if _evidence_url(value):result.add(value)


def _add_links(links,result):
    for link in _items(links):
        _add(link.get('url') if isinstance(link,dict) else link,result)


def _source_urls(evidence):
    """Only source records may authorize links, never task or AI-authored text."""
    result=set()
    if not isinstance(evidence,dict):return result
    for observation in _items(evidence.get('observations')):
        if not isinstance(observation,dict):continue
        _add(observation.get('url'),result)
        metadata=observation.get('metadata')
        if isinstance(metadata,dict):_add_links(metadata.get('links'),result)
    for change in _items(evidence.get('changes')):
        if not isinstance(change,dict):continue
        _add(change.get('url'),result)
        detail=change.get('evidence')
        if isinstance(detail,str):
            try:detail=json.loads(detail)
            except (ValueError,TypeError):continue
        if isinstance(detail,dict):
            for field in ('links_added','links_removed'):
                _add_links(detail.get(field),result)
    return result


def unknown_report_links(report,evidence) -> list[str]:
    """Return distinct unsupported HTTP(S) hrefs, in report order.

    Link labels never authorize a target. No slug generation, normalization or
    correction is applied: slash, query and fragment remain part of the URL.
    Relative links and local anchors are outside this external-source check.
    """
    allowed=_source_urls(evidence)
    rendered=BeautifulSoup(str(render_report(report)),'html.parser')
    unknown=[];seen=set()
    for anchor in rendered.select('a[href]'):
        href=anchor.get('href','')
        try:external=urlsplit(href).scheme in ('http','https')
        except ValueError:external=True
        if external and href not in allowed and href not in seen:
            seen.add(href);unknown.append(href)
    return unknown
