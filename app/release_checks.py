"""Release regressions: service DNS, honest preview, offline volume restoration."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from urllib.parse import urlunsplit
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import httpx
from fastapi.testclient import TestClient
import browser_endpoint
import gateway
import store
import engine
import web
import volume_backup as backup
from backup_archive import BackupError, FORMAT, MANIFEST


class Release(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/data');self.root=Path(self.temp.name)
        self.saved=store.DATA,store.CONFIG,store.SECRETS,engine.DATA,web.DATA
        store.DATA=engine.DATA=web.DATA=self.root/'data'
        store.CONFIG=self.root/'config/settings.json';store.SECRETS=self.root/'secrets/credentials.json'
        store.init()

    def tearDown(self):
        store.DATA,store.CONFIG,store.SECRETS,engine.DATA,web.DATA=self.saved
        self.temp.cleanup()

    def test_01_docker_service_resolves_without_fixed_ip(self):
        with patch.object(browser_endpoint.socket,'gethostbyname',return_value='172.28.7.14'):
            self.assertEqual(browser_endpoint.resolve('http://browser:9223'),'http://172.28.7.14:9223')

    def test_02_explicit_qa_endpoint_does_not_resolve_service(self):
        with patch.object(browser_endpoint.socket,'gethostbyname') as lookup:
            self.assertEqual(browser_endpoint.resolve('http://172.20.0.2:9223'),'http://172.20.0.2:9223')
            lookup.assert_not_called()

    def test_03_invalid_trusted_service_urls_rejected(self):
        invalid_auth=urlunsplit(('http','user:synthetic@browser:9223','','',''))
        for value in ('https://browser:9223',invalid_auth,'http://browser:80','http://browser:9223?q=x'):
            with self.subTest(value=value),self.assertRaises(ValueError):browser_endpoint.resolve(value)

    def test_04_nonprivate_service_addresses_rejected(self):
        for address in ('8.8.8.8','127.0.0.1','169.254.1.2','::1'):
            with self.subTest(address=address),patch.object(browser_endpoint.socket,'gethostbyname',return_value=address),self.assertRaises(ValueError):
                browser_endpoint.resolve('http://browser:9223')

    def test_05_unconfigured_email_is_preview_without_connection(self):
        with patch.dict(os.environ,{'KIWM_TEST_SMTP_HOST':''}),patch.object(gateway,'settings',return_value={}),patch.object(gateway.smtplib,'SMTP') as smtp:
            self.assertEqual(gateway.send_mail('content','subject',html='<p>content</p>'),'preview');smtp.assert_not_called()

    def test_06_optional_test_smtp_sends_multipart(self):
        with patch.dict(os.environ,{'KIWM_TEST_SMTP_HOST':'mailpit'}),patch.object(gateway,'settings',return_value={}),patch.object(gateway.smtplib,'SMTP') as smtp:
            self.assertEqual(gateway.send_mail('content','subject',True,'<p>content</p>'),'mailpit')
            smtp.assert_called_once_with('mailpit',1025,timeout=30)
            message=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message.get_content_type(),'multipart/alternative')

    def test_07_test_smtp_cannot_target_arbitrary_host(self):
        with patch.dict(os.environ,{'KIWM_TEST_SMTP_HOST':'192.168.0.1'}),patch.object(gateway,'settings',return_value={}),self.assertRaises(ValueError):
            gateway.send_mail('content','subject',True)

    def test_08_production_still_requires_tls(self):
        with patch.object(gateway,'settings',return_value={'smtp_enabled':True,'smtp_host':'smtp.example.test','smtp_tls':'none'}),self.assertRaises(ValueError):
            gateway.send_mail('content','subject')

    def test_09_gateway_does_not_claim_preview_was_sent(self):
        with patch.dict(os.environ,{'KIWM_TEST_SMTP_HOST':''}),patch.object(gateway,'settings',return_value={}):
            with TestClient(gateway.app) as client:
                response=client.post('/smtp',json={'content':'content','test':True})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json(),{'sent':False,'mode':'preview'})

    def test_10_report_preview_persists_and_is_not_retried(self):
        calls=[];real=httpx.AsyncClient
        def response(request):calls.append(request);return httpx.Response(200,json={'sent':False,'mode':'preview'})
        def client(*a,**k):return real(transport=httpx.MockTransport(response))
        with patch.object(engine.httpx,'AsyncClient',side_effect=client):
            rid=asyncio.run(engine.make_report());again=asyncio.run(engine.make_report())
        self.assertEqual(rid,again);self.assertEqual(len(calls),1)
        self.assertEqual(store.one('SELECT mail_status FROM reports WHERE id=?',(rid,))['mail_status'],'preview')
        self.assertTrue((store.DATA/'reports'/(rid+'.html')).is_file())

    def roots(self,name):
        p=self.root/name;p.mkdir()
        for d in backup.ROOTS:(p/d).mkdir()
        return p

    def archive(self):
        source=self.roots('source')
        with sqlite3.connect(source/'data/monitor.db') as db:db.execute('CREATE TABLE sample(value TEXT)');db.execute("INSERT INTO sample VALUES('preserved')")
        (source/'config/settings.json').write_text('{"sample":true}')
        (source/'secrets/credentials.json').write_text('{"sample":"SYNTHETIC"}')
        (source/'browser/Cookies').write_bytes(b'synthetic browser session')
        stream=io.BytesIO();backup.create(source,stream);stream.seek(0)
        return source,stream

    def test_11_backup_roundtrip_preserves_four_volumes(self):
        source,stream=self.archive();target=self.roots('target')
        self.assertEqual(backup.restore(target,stream),4)
        for d in backup.ROOTS:
            for p in (source/d).rglob('*'):
                if p.is_file():self.assertEqual(p.read_bytes(),(target/p.relative_to(source)).read_bytes())
        with sqlite3.connect(target/'data/monitor.db') as db:self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],'preserved')

    def test_12_restore_refuses_existing_data(self):
        _,stream=self.archive();target=self.roots('target');keep=target/'secrets/existing';keep.write_bytes(b'KEEP')
        with self.assertRaises(BackupError):backup.restore(target,stream)
        self.assertEqual(keep.read_bytes(),b'KEEP')

    def invalid_archive(self,path,expected=None):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            raw=b'content';entry=tarfile.TarInfo(path);entry.size=len(raw);tar.addfile(entry,io.BytesIO(raw))
            raw=json.dumps({'format':FORMAT,'files':{path:expected or hashlib.sha256(b'content').hexdigest()}}).encode()
            entry=tarfile.TarInfo(MANIFEST);entry.size=len(raw);tar.addfile(entry,io.BytesIO(raw))
        stream.seek(0);return stream

    def test_13_restore_rejects_path_traversal_before_writes(self):
        target=self.roots('target')
        with self.assertRaises(BackupError):backup.restore(target,self.invalid_archive('../escape'))
        self.assertFalse((self.root/'escape').exists());self.assertFalse(any(p.is_file() for p in target.rglob('*')))

    def test_14_restore_rejects_hash_mismatch_before_writes(self):
        target=self.roots('target')
        with self.assertRaises(BackupError):backup.restore(target,self.invalid_archive('data/monitor.db','0'*64))
        self.assertFalse(any(p.is_file() for p in target.rglob('*')))

    def test_15_backup_never_follows_browser_symlinks(self):
        source=self.roots('source');(source/'browser/untrusted').symlink_to('/etc/passwd')
        with self.assertRaises(BackupError):backup.create(source,io.BytesIO())

    def test_16_release_ui_hides_absent_test_mailbox(self):
        with patch.dict(os.environ,{'KIWM_TEST_TOOLS':''}):
            client=TestClient(web.app);settings=client.get('/smtp').text;reports=client.get('/reports').text;client.close()
        self.assertIn('Vorschau gespeichert',settings);self.assertNotIn('Testpostfach öffnen',settings)
        self.assertIn('Vorschau erstellen',reports)

    def test_17_restore_checks_available_space_before_writing(self):
        _,stream=self.archive();target=self.roots('target')
        with patch.object(backup.shutil,'disk_usage',return_value=SimpleNamespace(free=0)),self.assertRaises(BackupError):
            backup.restore(target,stream)
        self.assertFalse(any(p.is_file() for p in target.rglob('*')))


if __name__=='__main__':unittest.main(verbosity=2)
