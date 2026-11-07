"""Read-only inventory fixture. Only a test cookie selects the response stage."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter(prefix='/inventory-test')
COOKIE = 'kiwm_inventory_stage'
_REQUESTS = {'rule': 0, 'new': 0}
_NO_CACHE = {'cache-control': 'no-store, max-age=0', 'pragma': 'no-cache'}


def _stage(request):
    value = request.cookies.get(COOKIE, '1')
    return value if value in ('1', '2', '3') else '1'


def _pdf(text, metadata):
    """Minimal valid text PDF; no import from the parent fixture module."""
    safe = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream = f'BT /F1 16 Tf 50 760 Td ({safe}) Tj ET'.encode('latin-1')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
               b'<< /Producer (' + metadata.encode('ascii') + b') >>']
    result = b'%PDF-1.4\n'
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(result)
    result += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    result += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return result + f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


@router.get('/', response_class=HTMLResponse)
async def inventory_page(request: Request):
    annex = '<li><a href="/inventory-test/new.pdf">Neue Anlage</a></li>' if _stage(request) == '2' else ''
    html = f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Dokumentinventar-Test</title></head>
<body><header><a href="/inventory-test/new.pdf">Link außerhalb des Prüfbereichs</a></header>
<main id="inventory"><h1>Regelwerke und Anlagen</h1><p>Prüfung verlinkter Dateien unter unveränderten URLs.</p>
<ul><li><a href="/inventory-test/rule.pdf">Regelwerk</a></li>{annex}</ul></main></body></html>'''
    return HTMLResponse(html, headers=_NO_CACHE)


@router.get('/rule.pdf')
async def rule_pdf(request: Request):
    _REQUESTS['rule'] += 1
    stage = _stage(request)
    # Stage 1 -> 2 changes exactly one byte in the PDF text stream.
    text = 'Deadline 30 days.' if stage == '1' else 'Deadline 31 days.'
    metadata = 'changed-metadata-stage-3' if stage == '3' else 'stable-metadata'
    return Response(_pdf(text, metadata), media_type='application/pdf',
                    headers={**_NO_CACHE, 'content-disposition': 'attachment; filename="rule.pdf"'})


@router.get('/new.pdf')
async def new_pdf(request: Request):
    _REQUESTS['new'] += 1
    if _stage(request) != '2':
        raise HTTPException(404, 'Testanlage nicht vorhanden.')
    return Response(_pdf('New annex. Review annually.', 'annex-metadata'), media_type='application/pdf',
                    headers={**_NO_CACHE, 'content-disposition': 'attachment; filename="new.pdf"'})


@router.get('/status')
async def request_status():
    # No reset/mutation endpoint and no request contents or cookies are exposed.
    return dict(_REQUESTS)
