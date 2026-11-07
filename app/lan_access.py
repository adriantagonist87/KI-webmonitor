"""Explicit LAN origin; never trust an arbitrary forwarded host."""
import ipaddress
import re
from urllib.parse import urlsplit


def lan_origin(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        url = urlsplit(value)
        address = ipaddress.IPv4Address(url.hostname)
        private = any(address in ipaddress.ip_network(n) for n in
                      ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        if (url.scheme not in ('http', 'https') or not private or not url.port or not 1024 <= url.port <= 65535
                or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment
                or value.rstrip('/') != f'{url.scheme}://{address}:{url.port}'):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('KIWM_LAN_URL requires http(s)://PRIVATE_IPV4:PORT (1024-65535).') from None
    return value.rstrip('/')


def is_lan_request(request, origin):
    return bool(origin and request.headers.get('host') == urlsplit(origin).netloc)


def public_origin(value):
    value=(value or '').strip()
    if not value:return None
    try:
        url=urlsplit(value)
        hostname=url.hostname or ''
        labels=hostname.split('.')
        if (url.scheme!='https' or url.port is not None or url.username or url.password
                or url.path not in ('','/') or url.query or url.fragment
                or value.rstrip('/')!='https://'+hostname or len(hostname)>253 or len(labels)<2
                or re.fullmatch(r'[0-9.]+',hostname)
                or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',s) for s in labels)
                or hostname.endswith(('.local','.lan','.internal','.localhost'))):raise ValueError()
        try:ipaddress.ip_address(hostname)
        except ValueError:pass
        else:raise ValueError()
    except (ValueError,TypeError):
        raise ValueError('KIWM_PUBLIC_URL requires https://PUBLIC_HOSTNAME, without port, credentials or path.') from None
    return value.rstrip('/')


def browser_url(request, origin, local, public=None):
    remote = is_lan_request(request, origin) or is_lan_request(request,public)
    base = '/browser' if remote else local
    return base + '/vnc.html?autoconnect=true&resize=scale' + ('&path=browser/websockify' if remote else '')
