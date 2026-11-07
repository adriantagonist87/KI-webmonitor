"""Bounded, inert project icons. Fetches use the existing public-only proxy, no cookies."""
import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit
import httpx
from bs4 import BeautifulSoup
from PIL import Image
from project_urls import detail_path
import store

MAX_ICON = 256 * 1024
KEY = 'project_icon:'
SVG_TAGS = set('svg g path circle ellipse rect line polyline polygon defs linearGradient radialGradient stop clipPath mask title desc text tspan use'.split())
SVG_ATTRS = set('id viewBox width height x y x1 x2 y1 y2 cx cy r rx ry d points transform fill stroke stroke-width stroke-linecap stroke-linejoin stroke-miterlimit stroke-dasharray stroke-dashoffset fill-rule clip-rule opacity fill-opacity stroke-opacity offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod clip-path mask font-size font-family text-anchor preserveAspectRatio href'.split())


def normalize(raw, filename=''):
    if not raw or len(raw) > MAX_ICON:
        raise ValueError('Icon-Datei muss kleiner als 256 KB sein.')
    if raw.lstrip().startswith((b'<', b'\xef\xbb\xbf<')) or filename.lower().endswith('.svg'):
        if re.search(br'<!DOCTYPE|<!ENTITY', raw, re.I): raise ValueError('SVG mit Entitäten ist nicht erlaubt.')
        try: root = ET.fromstring(raw)
        except ET.ParseError: raise ValueError('Ungültige SVG-Datei.') from None
        if root.tag.split('}')[-1] != 'svg': raise ValueError('Keine SVG-Grafik.')
        if sum(1 for _ in root.iter()) > 3000: raise ValueError('SVG ist zu komplex.')
        for parent in list(root.iter()):
            for child in list(parent):
                if not isinstance(child.tag, str) or child.tag.split('}')[-1] not in SVG_TAGS: parent.remove(child)
        for node in root.iter():
            node.tag = node.tag.split('}')[-1]
            for key, value in list(node.attrib.items()):
                local = key.split('}')[-1]
                node.attrib.pop(key)
                if local not in SVG_ATTRS or len(value) > 20000: continue
                if local == 'href' and not re.fullmatch(r'#[A-Za-z_][\w.-]*', value): continue
                if re.search(r'url\s*\(', value, re.I) and not re.fullmatch(r'url\(#[A-Za-z_][\w.-]*\)', value): continue
                if any(c in value.lower() for c in ('javascript:', 'data:', 'http:', 'https:', '@import', 'expression')): continue
                node.set(local, value)
        root.set('xmlns', 'http://www.w3.org/2000/svg')
        return ET.tostring(root, encoding='utf-8'), 'image/svg+xml'
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width > 2048 or im.height > 2048: raise ValueError('Icon ist zu groß.')
            im.load(); im = im.convert('RGBA'); im.thumbnail((96, 96))
            out = io.BytesIO(); im.save(out, format='PNG')
            return out.getvalue(), 'image/png'
    except (OSError, Image.DecompressionBombError): raise ValueError('Keine gültige ICO-, PNG- oder SVG-Grafik.') from None


def record(raw, mode='manual', url='', filename=''):
    content, media = normalize(raw, filename)
    return {'mode': mode, 'url': url, 'media': media, 'data': base64.b64encode(content).decode(), 'sha256': hashlib.sha256(content).hexdigest()}


def validate(value):
    if not isinstance(value, dict) or set(value) != {'mode','url','media','data','sha256'}: raise ValueError('Ungültiges Projekticon.')
    if value['mode'] not in ('auto','manual') or value['media'] not in ('image/png','image/svg+xml'): raise ValueError('Ungültiger Iconmodus.')
    if not isinstance(value['url'],str) or len(value['url']) > 8000: raise ValueError('Ungültige Iconquelle.')
    if value['url']: safe_url(value['url'])
    if not isinstance(value['data'],str) or len(value['data']) > MAX_ICON * 2: raise ValueError('Icon ist zu groß.')
    try: raw = base64.b64decode(value['data'], validate=True)
    except (ValueError, TypeError): raise ValueError('Ungültige Icon-Daten.') from None
    clean, media = normalize(raw)
    # Re-sanitize imported SVGs; the digest must describe the original safe bytes.
    if value['media'] != media or hashlib.sha256(raw).hexdigest() != value['sha256']: raise ValueError('Icon-Prüfsumme stimmt nicht.')
    if clean != raw: raise ValueError('Icon enthält nicht bereinigte Daten.')
    return value


def get(pid):
    row = store.one('SELECT value FROM state WHERE key=?', (KEY + pid,))
    try: return json.loads(row['value']) if row else None
    except (ValueError, TypeError): return None


def image_url(pid, icon):
    return detail_path(pid) + '/icon?v=' + icon['sha256'][:16] if icon else ''


def save(pid, value, connection=None):
    validate(value)
    args = (KEY + pid, json.dumps(value, ensure_ascii=False))
    sql = 'INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value'
    if connection is not None: connection.execute(sql, args)
    else: store.execute(sql, args)


def safe_url(url):
    try:
        u = urlsplit(url)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.port not in (None,80,443): raise ValueError()
        host = u.hostname.lower()
        if host == 'localhost' or host.endswith(('.local','.lan','.internal')) or '.' not in host: raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global: raise ValueError('Privates Iconziel ist nicht erlaubt.')
        except ValueError as exc:
            if 'Privates' in str(exc): raise
        if any(ord(c) < 33 for c in url): raise ValueError()
    except (ValueError, TypeError): raise ValueError('Icons dürfen nur von öffentlichen HTTP(S)-Adressen geladen werden.') from None
    return url


async def _get(client, url, limit):
    import site_safety
    for _ in range(5):
        safe_url(url)
        await site_safety.wait()
        site_safety.raise_http(url,200)
        async with client.stream('GET', url) as response:
            site_safety.raise_http(str(response.url),response.status_code,response.headers)
            if response.status_code>=400 and 'html' in response.headers.get('content-type','').lower():
                protection_body=bytearray()
                async for chunk in response.aiter_bytes():
                    protection_body.extend(chunk[:65536-len(protection_body)])
                    if len(protection_body)>=65536:break
                site_safety.raise_http(str(response.url),response.status_code,response.headers,
                                       protection_body.decode('utf-8',errors='replace'))
            if response.status_code in (301,302,303,307,308):
                url = urljoin(url, response.headers.get('location','')); continue
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit: raise ValueError('Icon-Antwort ist zu groß.')
            if 'html' in response.headers.get('content-type','').lower():
                site_safety.raise_http(str(response.url),response.status_code,response.headers,
                                       bytes(data[:65536]).decode('utf-8',errors='replace'))
            return bytes(data), str(response.url)
    raise ValueError('Zu viele Icon-Weiterleitungen.')


async def discover(url):
    safe_url(url)
    async def anonymous(request):
        request.headers.pop('cookie', None)
        request.headers.pop('authorization', None)
    async def fetch():
        async with httpx.AsyncClient(proxy='http://proxy:3128', timeout=5, follow_redirects=False, trust_env=False,
                                     headers={'User-Agent':'KI-Webmonitor/1.0 Icon Fetcher'}, event_hooks={'request':[anonymous]}) as client:
            candidates = []
            try:
                raw, final = await _get(client, url, 512 * 1024)
                soup = BeautifulSoup(raw, 'html.parser')
                for link in soup.select('link[href]'):
                    rel = [str(r).lower() for r in link.get('rel',[])]
                    if 'icon' in rel or 'apple-touch-icon' in rel:
                        candidates.append(urljoin(final, link['href']))
            except (httpx.HTTPError, ValueError): pass
            candidates.append(urljoin(url, '/favicon.ico'))
            for candidate in list(dict.fromkeys(candidates))[:5]:
                try:
                    raw, final = await _get(client, candidate, MAX_ICON)
                    return record(raw, 'auto', final)
                except (httpx.HTTPError, ValueError): continue
            raise ValueError('Kein nutzbares Favicon gefunden. Sie können ein ICO oder SVG hochladen.')
    try: return await asyncio.wait_for(fetch(), timeout=15)
    except (asyncio.TimeoutError, httpx.HTTPError): raise ValueError('Favicon-Abruf nicht erreichbar oder Zeitüberschreitung.') from None


async def refresh(project):
    """Attempt each run; a failed fetch never replaces the previous good icon."""
    import site_access
    # Never probe login portals anonymously, and do not refetch manual assets.
    existing=get(project['id'])
    if existing and existing['mode']=='manual' or (site_access.public_config(project['id']) or {}).get('enabled'):
        return
    try:
        icon = await discover(project['url'])
        current = get(project['id'])
        if not current or current['mode'] != 'manual': save(project['id'], icon)
        result = 'ok'
    except (ValueError, OSError): result = 'unavailable'
    store.execute('INSERT OR REPLACE INTO state VALUES(?,?)', ('project_icon_attempt:'+project['id'], json.dumps({'time':store.now(),'result':result})))
