import json
import socket
import urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:9223/json/version',timeout=3) as r:
    assert json.load(r).get('webSocketDebuggerUrl')
opener.open('http://127.0.0.1:6080/vnc.html',timeout=3).close()
with socket.create_connection(('127.0.0.1',5900),3) as c:
    assert c.recv(12).startswith(b'RFB ')
