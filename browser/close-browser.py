import json
import urllib.request
import websocket

try:
    data = json.load(urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=3))
    ws = websocket.create_connection(data['webSocketDebuggerUrl'], origin='http://localhost:9222', timeout=3)
    ws.send(json.dumps({'id': 1, 'method': 'Browser.close'}))
    ws.close()
except Exception:
    pass
