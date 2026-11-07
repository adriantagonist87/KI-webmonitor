"""Catalog/search/integrity/streaming tests using isolated original documents."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from pypdf import PdfWriter
import store
import document_files as files
import retention


def pdf(title='Sicherheit Berlin', author='Fachstelle'):
    writer = PdfWriter()
    writer.add_blank_page(width=100,height=100)
    writer.add_metadata({'/Title':title,'/Author':author,'/Keywords':'Brandschutz 2026',
                         '/CreationDate':'D:20251201090000Z','/ModDate':'D:20260101100000Z'})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class DocumentFiles(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='.document-files-check-',dir='/data')
        self.previous = store.DATA,store.CONFIG,store.SECRETS
        store.DATA = Path(self.scratch.name)
        store.CONFIG = store.DATA/'settings.json'
        store.SECRETS = store.DATA/'credentials.json'
        store.init()
        store.execute('INSERT INTO tags VALUES(?,?,?,?)',('work','Arbeit','arbeit','#1967d2'))
        store.execute('INSERT INTO tags VALUES(?,?,?,?)',('private','Privat','privat','#7c3aed'))
        for pid,name,tag in [('p1','Planung Alpha','Arbeit'),('p2','Bau Beta','Privat')]:
            store.execute('''INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created,tags)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(pid,name,'https://example.test/','PROMPT_ONLY_SECRET',
                'example.test','0 6 * * *',1,'2027-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',json.dumps([tag])))
        files._cached_metadata.cache_clear()

    def tearDown(self):
        files._cached_metadata.cache_clear()
        store.DATA,store.CONFIG,store.SECRETS = self.previous
        self.scratch.cleanup()

    def add(self, raw=None, url='https://example.test/Richtlinie-2026-Bau.pdf',
            when='2026-01-01T10:00:00+00:00', pid='p1', extension='.pdf', expired=False):
        raw = raw or pdf()
        rid,sid = store.uid(),store.uid()
        sha = store.digest(raw)
        relative = 'artifacts/objects/'+sha+extension
        path = store.DATA/relative
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(raw)
            timestamp = datetime.fromisoformat(when).timestamp()
            os.utime(path,(timestamp,timestamp))
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid,pid,when,when,'success','test'))
        store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (sid,rid,pid,url,'document',when,sha,store.digest('CONTENT_ONLY_SECRET'),
                       'CONTENT_ONLY_SECRET','[]',None if expired else relative,None,None))
        return {'id':sid,'run_id':rid,'project_id':pid,'sha256':sha,'file':relative,'url':url,'raw':raw}

    def change(self, old, new, detected):
        store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (store.uid(),new['project_id'],new['run_id'],new['url'],'Inhalt geändert',
                       detected,old['id'],new['id'],'{}',store.uid()))

    def test_01_repeated_runs_deduplicate_and_dates_do_not_refresh(self):
        first = self.add()
        latest = self.add(raw=first['raw'],when='2026-01-09T10:00:00+00:00',url=first['url']+'?ts=1720000000')
        result = files.list_files()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['files'][0]['id'],latest['id'])
        self.assertEqual(result['files'][0]['changed_at'],'2026-01-01T10:00:00+00:00')
        self.assertEqual(result['files'][0]['filename'],'Richtlinie-2026-Bau.pdf')

    def test_02_actual_transition_and_returning_old_bytes_are_recent(self):
        a = self.add()
        b = self.add(raw=pdf('Neue Fassung'),when='2026-01-03T10:00:00+00:00')
        self.change(a,b,'2026-01-03T11:00:00+00:00')
        again = self.add(raw=a['raw'],when='2026-01-05T10:00:00+00:00')
        self.change(b,again,'2026-01-05T11:00:00+00:00')
        rows = files.list_files()['files']
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['sha256'],a['sha256'])
        self.assertEqual(rows[0]['changed_at'],'2026-01-05T11:00:00+00:00')
        self.assertEqual(rows[0]['version_label'],'Aktuell')
        self.assertEqual(rows[1]['version_label'],'Vorversion')

    def test_03_compacted_initial_baseline_uses_known_original_save_time(self):
        first = self.add()
        self.add(raw=first['raw'],when='2026-03-05T10:00:00+00:00')
        store.execute('DELETE FROM snapshots WHERE id=?',(first['id'],))
        row = files.list_files()['files'][0]
        self.assertEqual(row['changed_at'],'2026-01-01T10:00:00+00:00')
        self.assertIn('Früheste verfügbare',row['changed_title'])

    def test_04_wildcard_casefold_terms_order_metadata_not_contents(self):
        self.add()
        for query in ('BERLIN richt* 202?', 'arbeit alpha', 'fach*', '20251201'):
            with self.subTest(query=query):
                self.assertEqual(files.list_files(query)['matched'],1)
        self.assertEqual(files.list_files('CONTENT_ONLY_SECRET')['matched'],0)
        self.assertEqual(files.list_files('PROMPT_ONLY_SECRET')['matched'],0)
        self.assertEqual(files.list_files('Berlin fehlend')['matched'],0)

    def test_05_unicode_and_literal_brackets_are_searchable(self):
        self.add(url='https://example.test/Stra%C3%9Fe-Anlage%5B1%5D.pdf')
        self.assertEqual(files.list_files('STRASSE Anlage[1]')['matched'],1)
        self.assertEqual(files.list_files('Anlage[2]')['matched'],0)

    def test_06_all_sort_fields_and_invalid_input(self):
        self.add(url='https://example.test/Zulu.pdf')
        self.add(raw=pdf('Andere Datei'),url='https://example.test/Alpha.pdf',pid='p2',when='2026-01-03T10:00:00+00:00')
        self.assertEqual(files.list_files()['files'][0]['project_id'],'p2')
        self.assertEqual(files.list_files(sort='name',direction='asc')['files'][0]['filename'],'Alpha.pdf')
        self.assertEqual(files.list_files(sort='project',direction='asc')['files'][0]['project_name'],'Bau Beta')
        self.assertEqual(files.list_files(sort='tags',direction='asc')['files'][0]['tags'][0]['name'],'Arbeit')
        self.assertEqual(len(files.list_files(sort='type')['files']),2)
        for keywords in ({'query':'x'*513},{'query':'a\x00b'},{'sort':'project;DELETE'},{'direction':'sideways'}):
            with self.assertRaises(files.InvalidQuery):
                files.list_files(**keywords)

    def test_07_expired_missing_and_unsafe_originals_are_not_listed(self):
        self.add(expired=True)
        missing = self.add(raw=pdf('Fehlt'))
        (store.DATA/missing['file']).unlink()
        outside = self.add(raw=pdf('Privat'))
        store.execute('UPDATE snapshots SET file=? WHERE id=?',('/secrets/credentials.json',outside['id']))
        result = files.list_files()
        self.assertEqual(result['total'],0)
        self.assertEqual(result['unavailable'],2)
        for sid in (missing['id'],outside['id'],'../secret'):
            with self.assertRaises(files.FileUnavailable):
                files.prepare_download(sid)

    def test_08_symlink_files_and_symlink_directories_are_rejected(self):
        item = self.add()
        path = store.DATA/item['file']
        target = store.DATA/'private.pdf'
        target.write_bytes(item['raw'])
        path.unlink();path.symlink_to(target)
        self.assertEqual(files.list_files()['total'],0)
        with self.assertRaises(files.FileUnavailable):
            files.prepare_download(item['id'])
        path.unlink();path.parent.rmdir()
        path.parent.symlink_to(store.DATA,target_is_directory=True)
        with self.assertRaises(files.FileUnavailable):
            files.prepare_download(item['id'])

    def test_09_checksum_mismatch_refuses_download_and_entire_zip(self):
        item = self.add()
        (store.DATA/item['file']).write_bytes(pdf('Manipuliert'))
        with self.assertRaises(files.IntegrityError):
            files.prepare_download(item['id'])
        with self.assertRaises(files.IntegrityError):
            files.prepare_zip()

    def test_10_individual_stream_exact_bytes_and_cross_thread_close(self):
        item = self.add()
        download = files.prepare_download(item['id'])
        self.assertEqual(download.media_type,'application/pdf')
        self.assertEqual(download.size,len(item['raw']))
        parts = []
        iterator = download.iterator()
        def read():
            parts.append(next(iterator))
            self.assertIsNone(getattr(retention._LOCAL,'held',None))
        worker = threading.Thread(target=read)
        worker.start();worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        parts.extend(iterator)
        self.assertEqual(b''.join(parts),item['raw'])
        download.close();download.close()

    def test_11_zip_unique_binaries_all_types_manifest_crc_and_no_disk_copy(self):
        first = self.add()
        self.add(raw=first['raw'],pid='p2')
        second = self.add(raw=b'Zelle;Wert\nA1;23\n',url='https://example.test/Tabelle.csv',extension='.csv')
        before = {p.relative_to(store.DATA).as_posix() for p in store.DATA.rglob('*') if p.is_file()}
        download = files.prepare_zip()
        raw = b''.join(download.iterator())
        after = {p.relative_to(store.DATA).as_posix() for p in store.DATA.rglob('*') if p.is_file()}
        self.assertEqual(after-before,{'.storage.lock'} if '.storage.lock' not in before else set())
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertIsNone(archive.testzip())
            originals = [archive.read(name) for name in archive.namelist() if name!='dateien.json']
            self.assertEqual({store.digest(value) for value in originals},{first['sha256'],second['sha256']})
            manifest = json.loads(archive.read('dateien.json'))
            self.assertEqual(manifest['unique_files'],2)
            self.assertEqual(manifest['archive_files'],3)
            self.assertEqual(len(originals),3)
            self.assertEqual({PurePosixPath(name).parts[0] for name in archive.namelist()
                              if name!='dateien.json'}, {'Planung Alpha','Bau Beta'})
            for row in manifest['documents']:
                self.assertEqual(store.digest(archive.read(row['filename'])),row['sha256'])
            self.assertEqual(len(manifest['documents']),3)
            self.assertNotIn('CONTENT_ONLY_SECRET',json.dumps(manifest))
            self.assertNotIn('PROMPT_ONLY_SECRET',json.dumps(manifest))

    def test_12_zip_filename_collisions_and_portable_paths(self):
        self.add(url='https://example.test/CON.pdf')
        self.add(raw=pdf('Andere Fassung'),url='https://else.test/CON.pdf')
        self.add(raw=pdf('Dritte Fassung'),url='https://example.test/%2e%2e%2fsecret.pdf')
        download = files.prepare_zip()
        with zipfile.ZipFile(io.BytesIO(b''.join(download.iterator()))) as archive:
            names = [name for name in archive.namelist() if name!='dateien.json']
            self.assertEqual(len(names),len({name.casefold() for name in names}))
            for name in names:
                path = PurePosixPath(name)
                self.assertEqual(len(path.parts),2)
                self.assertNotIn('..',path.parts)
                self.assertNotIn('\\',name)
                self.assertFalse(path.name.upper().startswith('CON.'))
        sha = 'a'*64
        used = {'same.pdf','same-'+sha[:12]+'.pdf',sha+'.pdf'}
        self.assertNotIn(files._zip_name('same.pdf','.pdf',sha,used).casefold(),used)
        self.assertEqual(files.safe_filename('abc\u202eexe.pdf','.pdf'),'abcexe.pdf')
        self.assertNotIn('\r',files.safe_filename('abc\r\n.pdf','.pdf'))

    def test_13_office_core_metadata_only_no_worksheet_content_or_entities(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('docProps/core.xml','<core><title>Netzanschluss Berlin</title><creator>Fachautor</creator></core>')
            archive.writestr('xl/worksheets/sheet1.xml','<text>WORKSHEET_ONLY_SECRET</text>')
        for extension in ('.xlsx','.dotx','.pptx'):
            self.add(raw=buffer.getvalue(),url='https://example.test/Anschluss'+extension,extension=extension)
        self.assertEqual(files.list_files('netz* fachautor')['matched'],3)
        self.assertEqual(files.list_files('WORKSHEET_ONLY_SECRET')['matched'],0)
        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe,'w') as archive:
            archive.writestr('docProps/core.xml','<!DOCTYPE core [<!ENTITY secret SYSTEM "file:///secrets/credentials.json">]><core><title>&secret;</title></core>')
        self.assertEqual(files._metadata(unsafe.getvalue(),'.xlsx'),{})
        for encoding in ('utf-16','utf-32'):
            encoded = io.BytesIO()
            xml = '<?xml version="1.0"?><!DOCTYPE core [<!ENTITY x "EXPANDED">]><core><title>&x;</title></core>'
            with zipfile.ZipFile(encoded,'w') as archive:
                archive.writestr('docProps/core.xml',xml.encode(encoding))
            self.assertEqual(files._metadata(encoded.getvalue(),'.xlsx'),{},encoding)

    def test_14_empty_archive_and_idempotent_cancel(self):
        with self.assertRaises(files.FileUnavailable):
            files.prepare_zip()
        item = self.add()
        download = files.prepare_download(item['id'])
        download.close();download.close()
        with retention.maintenance_lock():
            pass

    def test_15_office_core_read_is_bounded_even_when_header_claims_one_byte(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes,'w') as archive:
            archive.writestr('docProps/core.xml',b'x')
        requests = []
        class ForgedInflatedStream(io.BytesIO):
            def read(self, size=-1):
                requests.append(size)
                self_check.assertGreater(size,0)
                self_check.assertLessEqual(size,files.MAX_CORE_XML_BYTES+1)
                return b'x'*(files.MAX_CORE_XML_BYTES+1)
        self_check = self
        # The central directory advertises one byte. Model the inflater yielding
        # excess bytes despite that claim, without allocating a large payload.
        with patch.object(zipfile.ZipFile,'open',return_value=ForgedInflatedStream()):
            metadata = files._metadata(archive_bytes.getvalue(),'.xlsx')
        self.assertEqual(metadata,{})
        self.assertEqual(requests,[files.MAX_CORE_XML_BYTES+1])

    def test_16_additional_office_odf_and_zip_originals_are_listed_and_streamed_unchanged(self):
        extensions=('.doc','.dot','.xls','.xlt','.xlsm','.xltx','.xltm','.ppt','.pps','.ppsx','.rtf','.odt','.ods','.odp','.zip')
        originals=[]
        for extension in extensions:
            raw=b'Opaque original bytes for '+extension.encode()
            item=self.add(raw=raw,url='https://example.test/Original'+extension,extension=extension)
            originals.append(item)
            descriptor=files.get_file(item['id'])
            self.assertEqual(descriptor['media_type'],files.MEDIA[extension])
            self.assertTrue(descriptor['filename'].endswith(extension))
            self.assertEqual(b''.join(files.prepare_download(item['id']).iterator()),raw)
        self.assertEqual(files.list_files()['total'],len(extensions))
        with zipfile.ZipFile(io.BytesIO(b''.join(files.prepare_zip().iterator()))) as archive:
            self.assertIsNone(archive.testzip())
            hashes={store.digest(archive.read(name)) for name in archive.namelist() if name!='dateien.json'}
            self.assertEqual(hashes,{item['sha256'] for item in originals})

    def test_17_extended_export_reimport_restores_legacy_office_and_nested_zip_as_inert_bytes(self):
        import transfer
        nested=io.BytesIO()
        with zipfile.ZipFile(nested,'w') as archive:archive.writestr('inert.exe',b'Never executed or extracted')
        originals=[self.add(raw=b'Legacy Word original',url='https://example.test/Original.doc',extension='.doc'),
                   self.add(raw=b'Legacy Excel original',url='https://example.test/Original.xls',extension='.xls'),
                   self.add(raw=nested.getvalue(),url='https://example.test/Original.zip',extension='.zip')]
        package=store.DATA/'transfer-test.zip'
        transfer.export_to(package,history=True)
        payload=transfer.read_package(package)
        for item in originals:
            self.assertIn(item['file'],payload['files'])
            (store.DATA/item['file']).unlink()
        transfer.import_package(package,payload,apply_settings=False)
        for item in originals:
            self.assertEqual((store.DATA/item['file']).read_bytes(),item['raw'])
            self.assertEqual(b''.join(files.prepare_download(item['id']).iterator()),item['raw'])
        self.assertFalse((store.DATA/'inert.exe').exists())

    def test_18_project_folders_reject_windows_devices_and_forbidden_characters(self):
        names = ['CON','prn.txt','AUX','nul','COM1','LPT9','COM\u00b9','CONIN$','CONOUT$',
                 ' ../A:B/C\\D<e>"f|g?h*\r\n. ','.','   ','\u202e','Ordner. ']
        rows = [{'project_id':str(i),'project_name':name} for i,name in enumerate(names)]
        folders = files._zip_folders(rows)
        self.assertEqual(len(folders),len(names))
        for folder in folders.values():
            self.assertTrue(folder)
            self.assertNotIn(folder,('.', '..'))
            self.assertEqual(folder,folder.rstrip(' .'))
            self.assertFalse(files._RESERVED.fullmatch(folder))
            self.assertFalse(any(c in folder for c in '<>:"/\\|?*\r\n'))
            self.assertNotIn('\u202e',folder)
        self.assertEqual(files.safe_filename('CONIN$.pdf','.pdf'),'_CONIN$.pdf')

    def test_19_folder_collisions_case_unicode_truncation_and_root_manifest(self):
        names = ['Arbeit','arbeit','A/B','A:B','Caf\u00e9','Cafe\u0301','dateien.json',
                 '\U0001f600'*100+'A','\U0001f600'*100+'B']
        rows = [{'project_id':str(i),'project_name':name} for i,name in enumerate(names)]
        folders = files._zip_folders(rows)
        self.assertEqual(folders,files._zip_folders(list(reversed(rows))))
        self.assertEqual(len(set(files._normal(n) for n in folders.values())),len(rows))
        self.assertNotIn('dateien.json',set(files._normal(n) for n in folders.values()))
        self.assertTrue(all(len(n.encode('utf-8'))<=80 for n in folders.values()))

    def test_20_long_unicode_member_paths_stay_short_and_keep_extensions(self):
        store.execute('UPDATE projects SET name=? WHERE id=?',('\U0001f680'*100,'p1'))
        self.add(url='https://example.test/'+'\u754c'*180+'.pdf')
        self.add(raw=pdf('Different'),url='https://else.test/'+'\u754c'*180+'.pdf')
        with zipfile.ZipFile(io.BytesIO(b''.join(files.prepare_zip().iterator()))) as archive:
            for name in archive.namelist():
                if name=='dateien.json':
                    continue
                folder,filename=PurePosixPath(name).parts
                self.assertLessEqual(len(folder.encode('utf-8')),80)
                self.assertLessEqual(len(filename.encode('utf-8')),120)
                self.assertLessEqual(len(name.encode('utf-8')),201)
                self.assertTrue(filename.endswith('.pdf'))
            self.assertIsNone(archive.testzip())

    def test_21_shared_bytes_export_once_per_project_with_correct_manifest_paths(self):
        first=self.add()
        self.add(raw=first['raw'],url='https://example.test/alias.pdf')
        self.add(raw=first['raw'],pid='p2')
        with zipfile.ZipFile(io.BytesIO(b''.join(files.prepare_zip().iterator()))) as archive:
            manifest=json.loads(archive.read('dateien.json'))
            self.assertEqual(manifest['unique_files'],1)
            self.assertEqual(manifest['archive_files'],2)
            self.assertEqual(manifest['format_version'],2)
            self.assertEqual(manifest['layout'],'project-folders')
            paths={row['project_id']:row['filename'] for row in manifest['documents']}
            self.assertNotEqual(paths['p1'],paths['p2'])
            self.assertTrue(paths['p1'].startswith('Planung Alpha/'))
            self.assertTrue(paths['p2'].startswith('Bau Beta/'))
            for row in manifest['documents']:
                self.assertEqual(archive.read(row['filename']),first['raw'])

    def test_22_archive_limits_count_cross_project_copies_and_release_the_lock(self):
        first=self.add()
        self.add(raw=first['raw'],pid='p2')
        for limits in ({'MAX_ZIP_FILES':1},{'MAX_ZIP_BYTES':len(first['raw'])}):
            with patch.multiple(files,**limits):
                with self.assertRaises(files.ArchiveTooLarge):
                    files.prepare_zip()
        with retention.maintenance_lock():
            pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
