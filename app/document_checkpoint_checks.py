"""Private atomic document ledgers; preserve legacy evidence without extraction."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

scratch=tempfile.TemporaryDirectory(prefix='.document-checkpoint-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import engine


class DocumentCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=scratch.name)
        self.root=Path(self.tmp.name)
        self.old_data=engine.DATA
        engine.DATA=self.root
        self.events=[]
        self.patches=[patch.object(engine,'event',side_effect=lambda rid,message:self.events.append((rid,message))),
                      patch.object(engine,'redact_text',side_effect=lambda text:text.replace('PRIVATE_MARKER','[redacted]')),
                      patch.object(engine,'llm',new=AsyncMock(side_effect=AssertionError('Checkpoint must not call a model')))]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        engine.DATA=self.old_data
        self.tmp.cleanup()

    def inventory(self):
        source='https://source.test/unreadable.pdf'
        return {'found':2,'checked':1,'documents':[
            {'url':'https://source.test/good.pdf','title':'Good','status':'checked',
             'hashes':{'id':'s1','sha256':'a'*64,'text_sha256':'b'*64},
             'extraction':{'method':'vision_ocr','provenance':{'pages':[{'page':n,'sha256':'c'*64} for n in range(500)]}}},
            {'url':source,'title':'Missing','status':'failed','error':'PRIVATE_MARKER '+'Timeout '*40,'hashes':None}],
            'failed':[{'url':source,'error':'PRIVATE_MARKER '+'Timeout '*40}], 'unverified':[]}

    def test_48_full_error_checkpoint_is_atomic_private_nonmutating_and_overwrites_one_run_file(self):
        full=self.inventory();before=copy.deepcopy(full);rid='a'*32
        path=engine.checkpoint_document_check(rid,full)
        self.assertEqual(json.loads(path.read_text()),before);self.assertEqual(full,before)
        self.assertEqual(len(json.loads(path.read_text())['documents'][0]['extraction']['provenance']['pages']),500)
        self.assertFalse(path.with_suffix('.tmp').exists())
        if os.name=='posix':self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertIn('1 fehlgeschlagen',self.events[0][1]);self.assertIn('unreadable.pdf',self.events[1][1])
        self.assertNotIn('PRIVATE_MARKER',self.events[1][1]);self.assertTrue(all(len(e[1])<=500 for e in self.events))
        compact=engine.compact_document_check(full)
        self.assertEqual(json.loads(path.read_text()),before);self.assertEqual(full,before)
        self.assertNotIn('hashes',compact['documents'][0])
        updated=copy.deepcopy(full);updated['checked']=0
        engine.checkpoint_document_check(rid,updated)
        self.assertEqual(json.loads(path.read_text()),updated)
        self.assertEqual([p.name for p in path.parent.iterdir()],['document-check.json'])
        engine.llm.assert_not_awaited()

    def test_49_legacy_safe_ids_work_but_traversal_encoded_absolute_and_unbounded_ids_write_nothing(self):
        for rid in ('legacy-run_1','a'*32,'A_B-9'):
            self.assertTrue(engine.checkpoint_document_check(rid,self.inventory()).is_file())
        for rid in ('','../escape','a/b',r'a\b','/absolute','C:drive','a.b','a%2fb','a%5cb','a%2eb','x'*81,None):
            with self.subTest(rid=rid),self.assertRaises(ValueError):engine.checkpoint_document_check(rid,self.inventory())
        self.assertFalse((self.root/'escape').exists())
        with self.assertRaises(ValueError):engine._private_run_file('valid','../evidence.json')

    @unittest.skipUnless(os.name=='posix','Symlink permissions are exercised in the Linux app container')
    def test_50_checkpoint_rejects_symlinked_data_artifact_run_final_and_temporary_paths(self):
        outside=self.root/'outside';outside.mkdir();sentinel=outside/'sentinel';sentinel.write_text('UNCHANGED')
        for kind in ('data','artifacts','run','final','temporary'):
            case=self.root/kind
            if kind=='data':case.symlink_to(outside,target_is_directory=True)
            else:case.mkdir()
            engine.DATA=case
            if kind=='artifacts':(case/'artifacts').symlink_to(outside,target_is_directory=True)
            elif kind!='data':
                (case/'artifacts').mkdir()
                if kind=='run':(case/'artifacts/safe').symlink_to(outside,target_is_directory=True)
                else:
                    folder=case/'artifacts/safe';folder.mkdir()
                    (folder/('document-check.json' if kind=='final' else 'document-check.tmp')).symlink_to(sentinel)
            with self.subTest(kind=kind),self.assertRaises(ValueError):engine.checkpoint_document_check('safe',self.inventory())
            self.assertEqual(sentinel.read_text(),'UNCHANGED')
        engine.DATA=self.root

    def test_51_checkpoint_survives_failed_final_write_and_is_removed_only_after_success(self):
        rid='safe-run';full=self.inventory();path=engine.checkpoint_document_check(rid,full)
        with patch.object(engine,'save_json',side_effect=OSError('Disk write failed')):
            with self.assertRaises(OSError):engine.save_final_evidence(rid,{'document_check':full})
        self.assertEqual(json.loads(path.read_text()),full)
        self.assertFalse((path.parent/'evidence.json').exists())
        final=engine.save_final_evidence(rid,{'document_check':full})
        self.assertEqual(json.loads(final.read_text()),{'document_check':full})
        self.assertFalse(path.exists());self.assertFalse(final.with_suffix('.tmp').exists())
        if os.name=='posix':self.assertEqual(final.stat().st_mode&0o777,0o600)

    def test_52_checkpoint_cleanup_error_does_not_undo_complete_final_evidence(self):
        rid='safe-run';full=self.inventory();checkpoint=engine.checkpoint_document_check(rid,full)
        with patch.object(Path,'unlink',side_effect=PermissionError('Cleanup temporarily denied')):
            final=engine.save_final_evidence(rid,{'document_check':full})
        self.assertEqual(json.loads(final.read_text()),{'document_check':full})
        self.assertTrue(checkpoint.is_file());engine.llm.assert_not_awaited()


if __name__=='__main__':unittest.main(verbosity=2)
