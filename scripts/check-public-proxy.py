"""Isolated nginx/Brave integration proof; synthetic QA accounts only."""
import asyncio,json
from http.cookies import SimpleCookie
import httpx
from websockets.asyncio.client import connect
ORIGIN='https://webmonitor.example.org'
cookie={}
client=httpx.Client(base_url='http://ingress:8083',follow_redirects=False,trust_env=False)
def request(method,path,**kw):
    headers={'Host':'webmonitor.example.org','X-Forwarded-Proto':'https','Cookie':'; '.join(k+'='+v for k,v in cookie.items())}
    headers.update(kw.pop('headers',{}))
    r=client.request(method,path,headers=headers,**kw)
    for header in r.headers.get_list('set-cookie'):
        parsed=SimpleCookie();parsed.load(header)
        for key,m in parsed.items():
            if m.value:cookie[key]=m.value
            else:cookie.pop(key,None)
    return r
async def main():
    for path in ('/login','/projects?filter=active','/browser/vnc.html'):
        r=request('GET',path,headers={'X-Forwarded-Proto':'http'})
        assert r.status_code==308 and r.headers['location']==ORIGIN+path
        assert 'set-cookie' not in r.headers
    r=request('POST','/login',data={'csrf':'synthetic','password':'synthetic'},headers={'X-Forwarded-Proto':'http','Origin':'http://webmonitor.example.org'})
    assert r.status_code==303 and r.headers['location']==ORIGIN+'/login'
    assert 'set-cookie' not in r.headers
    r=request('GET','/login',headers={'X-Forwarded-Proto':''})
    assert r.status_code==308
    assert request('GET','/browser/vnc.html').status_code==303
    r=request('GET','/login');assert r.status_code==200
    assert 'Secure' in r.headers['set-cookie'] and 'HttpOnly' in r.headers['set-cookie']
    assert request('GET','/static/login.css').status_code==200
    assert request('POST','/login',data={'csrf':cookie['kiwm_csrf']},headers={'Origin':'https://attacker.example'}).status_code==403
    r=request('POST','/login',data={'username':'synthetic-admin','password':'SYNTHETIC-PUBLIC-PASSWORD','csrf':cookie['kiwm_csrf']},headers={'Origin':ORIGIN})
    assert r.status_code==303 and 'kiwm_session' in cookie
    assert request('GET','/').status_code==200
    assert request('GET','/auth/check').status_code==204
    assert '/browser/vnc.html?' in request('GET','/browser').text
    assert request('GET','/browser/vnc.html').status_code==200
    headers={'X-Forwarded-Proto':'https','Cookie':'; '.join(k+'='+v for k,v in cookie.items())}
    async with connect('ws://webmonitor.example.org/browser/websockify',host='ingress',port=8083,proxy=None,origin=ORIGIN,subprotocols=['binary'],additional_headers=headers,open_timeout=10) as ws:
        greeting=await asyncio.wait_for(ws.recv(),10)
        assert greeting.startswith(b'RFB ')
    assert request('GET','/browser/websockify',headers={'Origin':'https://attacker.example'}).status_code==403
    assert request('GET','/',headers={'Host':'192.168.50.10:8765'}).status_code==200
    assert request('POST','/logout',data={'csrf':cookie['kiwm_csrf']},headers={'Origin':ORIGIN}).status_code==303
    assert request('GET','/auth/check').status_code==401
    print(json.dumps({'http_get_upgrade':'passed','stale_http_post_restart':'passed','missing_protocol_upgrade':'passed','nginx_login_dashboard_lan':'passed','authenticated_brave_html':'passed','authenticated_websocket_rfb':'passed','foreign_origin_rejected':'passed','logout':'passed'}))
asyncio.run(main())
