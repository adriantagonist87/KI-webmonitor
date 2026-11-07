"""Image-owned local health probe; never calls an inference/model endpoint."""
import os
import urllib.request

path = '/' if os.environ.get('ROLE') == 'fixture' else '/health'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:8000' + path, timeout=5) as response:
    assert response.status == 200
