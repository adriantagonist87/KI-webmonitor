"""Resolve the trusted Docker service to an IP accepted by Chromium DevTools."""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

def resolve(value):
    url = urlsplit(value)
    if url.hostname != 'browser':
        return value
    if url.scheme != 'http' or url.username or url.password or url.query or url.fragment or url.port != 9223:
        raise ValueError('Ungültiger interner Browser-Endpunkt.')
    address = ipaddress.ip_address(socket.gethostbyname('browser'))
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local:
        raise ValueError('Browserdienst muss im privaten Docker-Netz liegen.')
    return urlunsplit((url.scheme, str(address)+':9223', url.path, '', ''))
