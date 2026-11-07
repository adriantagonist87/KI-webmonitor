"""Small immutable PNGs shared by MIME mail and authenticated preview assets."""
import base64,hashlib,io,re
from pathlib import Path
from PIL import Image,ImageDraw
import project_icons

# Keep the complete Content-ID header below the 78-column MIME fold limit.
# RFC 2047 encoded words are not valid identifiers for CID image resolution.
CID=re.compile(r'kiwm-[a-f0-9]{32}@assets\.webmonitor')

def fallback():
    image=Image.new('RGBA',(32,32),(0,0,0,0));draw=ImageDraw.Draw(image)
    draw.ellipse((3,3,29,29),outline='#66788c',width=2)
    draw.ellipse((10,3,22,29),outline='#66788c',width=2)
    draw.line((3,16,29,16),fill='#66788c',width=2)
    out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()

def png(raw,media='image/png',logo=False):
    if logo:
        if len(raw)>4*1024*1024:raise ValueError('Logo zu groß.')
        clean,kind=raw,'image/png'
    else:clean,kind=project_icons.normalize(raw,'.svg' if media=='image/svg+xml' else '')
    if kind=='image/svg+xml':
        import cairosvg
        clean=cairosvg.svg2png(bytestring=clean,output_width=32,output_height=32,unsafe=False)
    with Image.open(io.BytesIO(clean)) as image:
        if max(image.size)>2048:raise ValueError('Bild zu groß.')
        image.load();image=image.convert('RGBA');image.thumbnail((72,80) if logo else (32,32))
        if not logo:
            canvas=Image.new('RGBA',(32,32),(0,0,0,0));canvas.alpha_composite(image,((32-image.width)//2,(32-image.height)//2));image=canvas
        out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()

class Images:
    def __init__(self):self.items={}
    def add(self,raw):
        cid='kiwm-'+hashlib.sha256(raw).hexdigest()[:32]+'@assets.webmonitor'
        if cid in self.items and self.items[cid]['data']!=base64.b64encode(raw).decode('ascii'):
            raise ValueError('Bildkennung ist nicht eindeutig.')
        self.items[cid]={'cid':cid,'media_type':'image/png','data':base64.b64encode(raw).decode('ascii')}
        return cid
    def logo(self):
        try:raw=png((Path(__file__).parent/'static/app-logo.png').read_bytes(),logo=True)
        except (OSError,ValueError):raw=fallback()
        return self.add(raw)
    def project(self,value):
        try:
            if not isinstance(value,dict):raise ValueError()
            raw=base64.b64decode(value['data'],validate=True)
            raw=png(raw,value.get('media','image/png'))
        except Exception:raw=fallback()
        return self.add(raw)

def validate(items,html):
    if not isinstance(items,list) or len(items)>4096:raise ValueError('Ungültige Berichtsbilder.')
    clean={};total=0
    for item in items:
        if not isinstance(item,dict) or set(item)!={'cid','media_type','data'}:raise ValueError('Ungültiges Berichtsbild.')
        cid=item['cid']
        if not isinstance(cid,str) or not CID.fullmatch(cid) or item['media_type']!='image/png':raise ValueError('Ungültige Bildreferenz.')
        if not isinstance(item['data'],str) or len(item['data'])>100000:raise ValueError('Berichtsbild zu groß.')
        raw=base64.b64decode(item['data'],validate=True);total+=len(raw)
        if total>4*1024*1024 or hashlib.sha256(raw).hexdigest()[:32]!=cid[5:37]:raise ValueError('Bildprüfsumme stimmt nicht.')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format!='PNG' or max(image.size)>128:raise ValueError('Ungültiges PNG-Berichtsbild.')
            image.verify()
        if cid in clean:raise ValueError('Doppelte Bildreferenz.')
        clean[cid]=raw
    referenced=set(re.findall(r'cid:('+CID.pattern+r')',html or ''))
    if referenced!=set(clean):raise ValueError('Berichtsbild fehlt oder ist nicht referenziert.')
    return clean
