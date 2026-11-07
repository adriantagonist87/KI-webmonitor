import asyncio
import json
import os
from pathlib import Path
import httpx

async def main():
    result = {}
    private_lan_url = os.environ['KIWM_TEST_PRIVATE_LAN_URL']
    async with httpx.AsyncClient(base_url='http://app:8000', timeout=15) as h:
        r = await h.get('/')
        assert r.status_code == 200 and 'KI-Webmonitor' in r.text
        result['management_ui'] = 'PASS'
        token = h.cookies['kiwm_csrf']
        r = await h.post('/manual', data={'csrf': 'invalid', 'mode': 'stop'})
        assert r.status_code == 403
        result['invalid_csrf'] = 'BLOCKED'
        r = await h.post('/manual', data={'csrf': token, 'mode': 'stop'}, headers={'Origin': 'http://evil.invalid'})
        assert r.status_code == 403
        result['cross_origin_form'] = 'BLOCKED'
        r = await h.get('/artifacts/..%2F..%2Fsecrets%2Fcredentials.json')
        assert r.status_code == 404
        result['artifact_path_traversal'] = 'BLOCKED'
    async with httpx.AsyncClient(timeout=15) as h:
        r = await h.get('http://ingress:8081/vnc.html', headers={'Origin': 'http://evil.invalid'})
        assert r.status_code == 403
        result['foreign_browser_origin'] = 'BLOCKED'
    async with httpx.AsyncClient(proxy='http://proxy:3128', timeout=20) as h:
        r = await h.get(private_lan_url)
        assert r.status_code == 403
        result['proxy_private_lan'] = 'BLOCKED'
        r = await h.get('https://example.org')
        assert r.status_code == 200
        result['proxy_public_https'] = 'PASS'
    try:
        async with httpx.AsyncClient(timeout=3) as h:
            await h.get(private_lan_url)
    except (httpx.ConnectError, httpx.TimeoutException):
        result['direct_private_lan'] = 'BLOCKED'
    else:
        raise AssertionError('Unexpected direct LAN access')
    assert not Path('/var/run/docker.sock').exists()
    result['agent_docker_socket'] = 'ABSENT'
    assert Path('/secrets/credentials.json').stat().st_mode & 0o077 == 0
    result['credential_permissions'] = '0600'
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
