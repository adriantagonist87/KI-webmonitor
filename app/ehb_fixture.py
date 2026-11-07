"""Private deterministic PDF-version fixture; only a root-mounted file selects state.

No mutation route, cookies, script, form, clock, random ID or external request.
The page is byte-identical in all states. State 4 reuses state 3 PDF bytes.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response


router = APIRouter(prefix='/ehb-case')
VERSION_FILE = Path('/tests/ehb-version.txt')
CREATED = "D:20260615143439+02'00'"
STALE_HTTP_DATE = 'Mon, 15 Jun 2026 15:12:38 +0200'
NEW_HTTP_DATE = 'Wed, 17 Jun 2026 15:12:38 +0200'
NO_CACHE = {'Cache-Control': 'no-store, max-age=0', 'Pragma': 'no-cache'}
HTML = '''<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF-Dateiversionen</title><style>body{font:18px/1.6 Arial,sans-serif;max-width:900px;margin:60px auto;padding:0 24px;color:#252a32;background:white}a{color:#1967d2}</style></head>
<body><main id="ehb-content"><h1>Dokumente und Richtlinien</h1>
<p>Hier finden Sie die aktuelle Richtlinie. Maßgeblich ist die verlinkte Originaldatei einschließlich ihres dokumentierten Bearbeitungsstands.</p>
<p><a href="/ehb-case/document.pdf">Richtlinie zur regelmäßigen Prüfung (PDF)</a></p>
</main></body></html>'''


def version():
    try:
        with VERSION_FILE.open(encoding='ascii') as stream:
            value = stream.read(8).strip()
    except FileNotFoundError:
        return 1
    except (OSError, UnicodeError):
        raise HTTPException(500, 'Testzustand nicht lesbar.') from None
    if value not in ('1', '2', '3', '4'):
        raise HTTPException(500, 'Ungültiger Testzustand.')
    return int(value)


def document_bytes(stage):
    """Actual one-page text PDF with fixed xref/Info; no automatic writer dates/ID."""
    if stage not in (1, 2, 3, 4):
        raise ValueError('Invalid fixture stage')
    days = 30 if stage <= 2 else 31
    modified = CREATED if stage == 1 else "D:20260616143439+02'00'"
    lines = [
        'Richtlinie zur regelmaessigen Pruefung',
        f'Die Prueffrist betraegt {days} Tage.',
        'Die verantwortliche Fachkraft dokumentiert jede abgeschlossene Pruefung.',
        'Die Aufzeichnungen enthalten den Pruefumfang und die festgestellten Ergebnisse.',
        'Bestehende Nachweise sind bei der folgenden Pruefung zum Vergleich heranzuziehen.',
        'Diese Richtlinie beschreibt die regelmaessige technische Kontrolle der Anlagen.',
        'Der Bearbeitungsstand wird in den Datumsmetadaten der Originaldatei gefuehrt.',
    ]
    commands = ['BT /F1 11 Tf 50 770 Td']
    for index, line in enumerate(lines):
        safe = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        if index:
            commands.append('0 -25 Td')
        commands.append('(' + safe + ') Tj')
    commands.append('ET')
    stream = '\n'.join(commands).encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode('ascii') + b' >>\nstream\n' + stream + b'\nendstream',
               ('<< /Producer (KI-Webmonitor deterministic fixture) /CreationDate (' + CREATED
                + ') /ModDate (' + modified + ') >>').encode('ascii')]
    result, offsets = b'%PDF-1.4\n', [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f'{number} 0 obj\n'.encode('ascii') + obj + b'\nendobj\n'
    xref = len(result)
    result += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode('ascii')
    result += b''.join(f'{offset:010d} 00000 n \n'.encode('ascii') for offset in offsets[1:])
    return result + f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode('ascii')


def document_headers(stage):
    if stage not in (1, 2, 3, 4):
        raise ValueError('Invalid fixture stage')
    return {**NO_CACHE, 'Content-Disposition': 'attachment; filename="document.pdf"',
            'Last-Modified': NEW_HTTP_DATE if stage == 4 else STALE_HTTP_DATE,
            'ETag': '"ehb-header-only"' if stage == 4 else '"ehb-stale"'}


@router.get('/', response_class=HTMLResponse)
async def page():
    return HTMLResponse(HTML, headers=NO_CACHE)


@router.get('/document.pdf')
async def document():
    stage = version()
    return Response(document_bytes(stage), media_type='application/pdf', headers=document_headers(stage))
