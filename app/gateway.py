import asyncio
import ipaddress
import json
import os
import smtplib
import socket
import ssl
import mail_message
from urllib.parse import urlsplit
import httpx
import communication_transport
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from store import settings, credentials

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

def monitor_network():
    """Reject invalid network configuration before allowing an upstream call."""
    try:
        network = ipaddress.ip_network(os.environ.get('MONITOR_SUBNET', '172.29.133.0/24'))
    except ValueError:
        raise ValueError('Ungültiges internes Stack-Netz') from None
    private_ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
    if network.version != 4 or not any(network.subnet_of(ipaddress.ip_network(n)) for n in private_ranges):
        raise ValueError('Ungültiges internes Stack-Netz')
    return network

def endpoint():
    internal_network = monitor_network()
    value = settings().get('omlx_url', '').rstrip('/')
    u = urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('oMLX-Adresse fehlt oder ist ungültig')
    addresses = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('oMLX-Adresse ist nicht auflösbar')
    # This gateway is deliberately restricted to the explicitly configured local inference server.
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            raise ValueError('oMLX muss eine private LAN-Adresse verwenden')
        if ip in internal_network:
            raise ValueError('oMLX darf kein Stack-interner Dienst sein')
    return value if value.endswith('/v1') else value + '/v1'

@app.get('/health')
async def health():
    monitor_network()
    return {'status': 'ok', 'role': 'gateway'}

@app.api_route('/v1/{path:path}', methods=['GET', 'POST'])
async def llm(path: str, request: Request):
    if (request.method, path) not in [('GET', 'models'), ('POST', 'chat/completions')]:
        return JSONResponse({'error': 'Endpoint nicht erlaubt'}, 403)
    body = await request.body()
    if len(body) > 24 * 1024 * 1024:
        return JSONResponse({'error': 'Anfrage zu groß'}, 413)
    if request.method == 'POST':
        try:
            payload = json.loads(body)
            payload.setdefault('chat_template_kwargs', {})['enable_thinking'] = False
            body = json.dumps(payload).encode()
        except (ValueError, TypeError, KeyError):
            return JSONResponse({'error': 'Ungültige Modellanfrage'}, 400)
    try:
        base = endpoint()
        headers = {'content-type': 'application/json'}
        key = credentials().get('omlx_key')
        if key:
            headers['authorization'] = 'Bearer ' + key
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), follow_redirects=False, trust_env=False) as client:
            for attempt in range(3):
                try:
                    r = await client.request(request.method, base + '/' + path, content=body, headers=headers)
                    if r.status_code in (429, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    # Upstream errors may contain headers or credentials: expose only the status.
                    if r.is_error:
                        return JSONResponse({'error': 'oMLX HTTP ' + str(r.status_code)}, r.status_code)
                    return Response(r.content, media_type='application/json')
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 ** attempt)
    except Exception as e:
        return JSONResponse({'error': 'oMLX-Verbindung fehlgeschlagen (' + type(e).__name__ + ')'}, 503)

def send_mail(content, subject, force_test=False, html=None, images=None, recipient=None):
    if recipient is not None:
        from distributions import email
        recipient=email(recipient)
    s = settings()
    production = bool(s.get('smtp_enabled')) and not force_test
    if production and any(k.startswith('smtp_') for k in {**s.get('_communication_errors',{}),**credentials().get('_communication_errors',{})}):
        raise ValueError('SMTP-Konfiguration ungültig; Portainer-Vorgaben prüfen.')
    host = s.get('smtp_host') if production else os.environ.get('KIWM_TEST_SMTP_HOST','')
    if not production and not host:
        return 'preview'  # The app has already saved the HTML/text report.
    if not production and host != 'mailpit':
        raise ValueError('Ungültiger interner Test-SMTP-Server')
    port = int(s.get('smtp_port', 587)) if production else 1025
    msg = mail_message.build(content,subject,
        s.get('smtp_from', 'webmonitor@example.test') if production else 'webmonitor@example.test',
        recipient or (s.get('smtp_to', '') if production else 'test@example.test'),html,images)
    mode = s.get('smtp_tls', 'starttls') if production else 'none'
    if production and mode not in ('starttls', 'ssl'):
        raise ValueError('Produktiver SMTP-Versand erfordert TLS')
    cls = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
    kwargs = {'context': ssl.create_default_context()} if mode == 'ssl' else {}
    with cls(host, port, timeout=30, **kwargs) as client:
        if mode == 'starttls':
            client.starttls(context=ssl.create_default_context())
        if production and s.get('smtp_user'):
            client.login(s['smtp_user'], credentials().get('smtp_password', ''))
        client.send_message(msg)
    return 'production' if production else 'mailpit'

@app.post('/smtp')
async def smtp(request: Request):
    d = await request.json()
    try:
        mode = await asyncio.to_thread(send_mail, d['content'], d.get('subject', 'KI-Webmonitor'), d.get('test', False), d.get('html'), d.get('images'), d.get('recipient'))
        return {'sent': mode != 'preview', 'mode': mode}
    except Exception as e:
        definite=isinstance(e,(ValueError,smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError,smtplib.SMTPConnectError,ConnectionRefusedError,ssl.SSLError))
        return JSONResponse({'error': 'SMTP fehlgeschlagen (' + type(e).__name__ + ')','failure_kind':'definite' if definite else 'uncertain'}, 502)


@app.post('/smtp/test')
async def smtp_test(request: Request):
    try:
        data=await request.json()
        if data.get('action') not in ('connection','message'):raise ValueError()
        return await asyncio.to_thread(communication_transport.smtp_probe,data['action']=='message')
    except Exception as exc:return communication_transport.safe_failure(exc,'SMTP')


@app.post('/telegram/test')
async def telegram_test(request: Request):
    try:
        data=await request.json()
        if data.get('action') not in ('connection','message'):raise ValueError()
        return await asyncio.to_thread(communication_transport.telegram_probe,data['action']=='message')
    except Exception as exc:return communication_transport.safe_failure(exc,'Telegram')


@app.post('/telegram/send')
async def telegram_send(request: Request):
    try:
        data=await request.json()
        return await asyncio.to_thread(communication_transport.telegram_send,data.get('text'))
    except Exception as exc:return communication_transport.safe_failure(exc,'Telegram')
