"""Bounded mouse/keyboard input through the existing private noVNC transport.

No shell, clipboard, framebuffer capture, new listening port or model-supplied
protocol messages. Pixel coordinates belong to the real X11 desktop.
"""
import asyncio
import struct
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect


KEYS = {'ctrl': 0xffe3, 'a': 0x61, 'l': 0x6c, 'return': 0xff0d,
        'tab': 0xff09, 'escape': 0xff1b, 'backspace': 0xff08}


def endpoint(cdp_url):
    u = urlsplit(cdp_url)
    if u.scheme != 'http' or not u.hostname or u.username or u.password:
        raise ValueError('Ungültiger interner Browserzugang.')
    host = '['+u.hostname+']' if ':' in u.hostname else u.hostname
    return urlunsplit(('ws', host+':6080', '/websockify', '', ''))


class NativeInput:
    def __init__(self, url):
        self.url, self.ws, self.buffer = url, None, bytearray()
        self.width = self.height = 0
        self.position = None

    async def read(self, size):
        while len(self.buffer) < size:
            chunk = await asyncio.wait_for(self.ws.recv(), 5)
            if not isinstance(chunk, bytes):
                raise RuntimeError('Ungültige Antwort der internen Maussteuerung.')
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise RuntimeError('Unerwartete Antwort der internen Maussteuerung.')
        result = bytes(self.buffer[:size]); del self.buffer[:size]
        return result

    async def __aenter__(self):
        self.ws = await connect(self.url, subprotocols=['binary'], proxy=None,
                                open_timeout=5, max_size=65536, compression=None)
        try:
            version = await self.read(12)
            if version not in (b'RFB 003.008\n', b'RFB 003.007\n'):
                raise RuntimeError('Nicht unterstützte interne Browsersteuerung.')
            await self.ws.send(b'RFB 003.008\n')
            count = (await self.read(1))[0]
            if not count or 1 not in await self.read(count):
                raise RuntimeError('Interne Browsersteuerung nicht verfügbar.')
            await self.ws.send(b'\x01')
            if struct.unpack('!I', await self.read(4))[0] != 0:
                raise RuntimeError('Interne Browsersteuerung abgewiesen.')
            await self.ws.send(b'\x01')  # Shared: preserve the user's noVNC view.
            header = await self.read(24)
            self.width, self.height = struct.unpack('!HH', header[:4])
            length = struct.unpack('!I', header[20:])[0]
            if length > 4096 or not (100 <= self.width <= 8192 and 100 <= self.height <= 8192):
                raise RuntimeError('Ungültige Desktop-Abmessungen.')
            await self.read(length)
            return self
        except BaseException:
            await self.ws.close(); self.ws = None
            raise

    async def __aexit__(self, *unused):
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def key(self, code, down):
        await self.ws.send(struct.pack('!BBHI', 4, int(down), 0, code))

    async def chord(self, *names):
        if not names or any(n not in KEYS for n in names):
            raise ValueError('Nicht unterstützte Tastenkombination.')
        pressed = []
        try:
            for name in names:
                await self.key(KEYS[name], True); pressed.append(KEYS[name])
                await asyncio.sleep(.04)
        finally:
            for code in reversed(pressed):
                await self.key(code, False)
                await asyncio.sleep(.04)
        await asyncio.sleep(.2)  # Let the desktop apply focus/shortcut changes.

    async def type_text(self, text, *, before_key=None):
        if not isinstance(text, str) or len(text) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError('Ungültige Texteingabe.')
        # Never log this argument: it can be a controller-owned credential.
        for char in text:
            if before_key is not None:
                await before_key()
            code = ord(char) if ord(char) <= 255 else 0x01000000 | ord(char)
            await self.key(code, True)
            await asyncio.sleep(.025)
            await self.key(code, False)
            await asyncio.sleep(.04)
        await asyncio.sleep(.2)  # Wait for the desktop to process the final key.

    async def pointer(self, x, y, mask=0):
        if (type(x) is not int or type(y) is not int or not 0 <= x < self.width
                or not 0 <= y < self.height or mask not in (0, 1, 8, 16)):
            raise ValueError('Mausziel liegt außerhalb des Browserdesktops.')
        await self.ws.send(struct.pack('!BBHH', 5, mask, x, y))
        self.position = (x, y)

    async def move(self, x, y):
        if self.position:
            a, b = self.position
            for step in range(1, 7):
                await self.pointer(round(a+(x-a)*step/6), round(b+(y-b)*step/6))
                await asyncio.sleep(.025)
        await self.pointer(x, y)
        await asyncio.sleep(.15)

    async def click(self, x, y):
        await self.move(x, y)
        await self.pointer(x, y, 1)
        await asyncio.sleep(.08)
        await self.pointer(x, y)

    async def scroll(self, x, y, direction, ticks=5):
        if direction not in ('up', 'down') or type(ticks) is not int or not 1 <= ticks <= 10:
            raise ValueError('Ungültiger Scrollauftrag.')
        await self.move(x, y)
        for _ in range(ticks):
            await self.pointer(x, y, 8 if direction == 'up' else 16)
            await self.pointer(x, y)
            await asyncio.sleep(.1)
