"""Extract digital text from page one in a networkless, unprivileged service."""
import json
import os
from pathlib import Path
import signal
import socketserver
import subprocess
import sys
import tempfile


SOCKET = '/run/kiwm-page-reader/reader.sock'
MAX_FILE_BYTES = 30 * 1024**2
MAX_TEXT_BYTES = 48_000
FILTERS = {
    **dict.fromkeys(('.doc', '.dot', '.docx', '.dotx', '.odt', '.rtf', '.txt', '.xml'), 'writer'),
    **dict.fromkeys(('.xls', '.xlt', '.xlsx', '.xlsm', '.xltx', '.xltm', '.ods', '.csv'), 'calc'),
    **dict.fromkeys(('.ppt', '.pps', '.pptx', '.ppsx', '.odp'), 'impress'),
}
SUPPORTED = {'.pdf', *FILTERS}


def run_command(args, timeout):
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            return process.wait(timeout=timeout) == 0
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise


def read_first_page(original, suffix, folder):
    if suffix != '.pdf':
        profile = folder / 'profile'
        (profile / 'user').mkdir(parents=True)
        (profile / 'user' / 'registrymodifications.xcu').write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
            '</item></oor:items>', encoding='utf-8')
        export = json.dumps({'PageRange': {'type': 'string', 'value': '1'}}, separators=(',', ':'))
        args = ['soffice', '-env:UserInstallation=' + profile.as_uri(), '--headless',
                '--nologo', '--norestore', '--nodefault', '--convert-to',
                f'pdf:{FILTERS[suffix]}_pdf_Export:{export}', '--outdir', str(folder), str(original)]
        if not run_command(args, timeout=30):
            return {'status': 'unreadable'}
        original = original.with_suffix('.pdf')
        if not original.is_file():
            return {'status': 'unreadable'}

    output = folder / 'page-one.txt'
    if not run_command(['pdftotext', '-f', '1', '-l', '1', '-layout', '-nopgbrk',
                        '-enc', 'UTF-8', str(original), str(output)], timeout=12):
        return {'status': 'unreadable'}
    with output.open('rb') as file:
        text = file.read(MAX_TEXT_BYTES).decode('utf-8', errors='ignore').strip()
    return {'status': 'text', 'text': text} if text else {'status': 'no_text'}


class PageRequest(socketserver.StreamRequestHandler):
    timeout = 8

    def handle(self):
        header = self.rfile.readline(80)
        if not header:
            return
        result = {'status': 'unreadable'}
        try:
            suffix, size = header.decode('ascii').split()
            size = int(size)
            if suffix in SUPPORTED and 0 < size <= MAX_FILE_BYTES:
                with tempfile.TemporaryDirectory(prefix='page-one-') as scratch:
                    folder = Path(scratch)
                    # XML is read as plain text, never opened as executable markup.
                    original = folder / ('original' + ('.txt' if suffix == '.xml' else suffix))
                    with original.open('wb') as file:
                        remaining = size
                        while remaining:
                            chunk = self.rfile.read(min(128 * 1024, remaining))
                            if not chunk:
                                return
                            file.write(chunk)
                            remaining -= len(chunk)
                    result = read_first_page(original, suffix, folder)
        except subprocess.TimeoutExpired:
            result = {'status': 'reader_timeout'}
        except (OSError, ValueError):
            pass
        try:
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8') + b'\n')
        except OSError:
            pass


def serve():
    os.umask(0o077)
    Path(SOCKET).unlink(missing_ok=True)
    with socketserver.UnixStreamServer(SOCKET, PageRequest) as server:
        server.serve_forever()


if __name__ == '__main__':
    if sys.argv[1:] == ['--health']:
        import socket
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(SOCKET)
    else:
        serve()
