"""Bound administrative capture exclusions; no network, model or deletion."""
import json
import os
import tempfile
import unittest

scratch=tempfile.TemporaryDirectory(prefix='.kiwm-comparison-validity-',dir=os.environ.get('DATA_DIR','/data'))
os.environ['DATA_DIR']=scratch.name
import store
from comparison_validity import exclude_snapshot,usable_snapshot,PREFIX,POLICY


SOURCE='https://sources.test/publications?category=fire'
REASON='Der vollständige Katalog wurde nachweislich nicht erfasst; die Aufnahme enthält nur ein Karussell.'
VERIFICATION={'observed_cards':8,'verified_total_cards':24,'method':'administrator_capture_comparison'}


class ComparisonValidityChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init()
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)',
                      ('project','Capture checks',SOURCE,'Monitor publications','sources.test','0 6 * * *',store.now(),store.now()))

    def setUp(self):
        with store.db() as connection:
            for table in ('change_reviews','changes','snapshots','events','runs'):
                connection.execute('DELETE FROM '+table)
            connection.execute('DELETE FROM state WHERE key LIKE ?',(PREFIX+'%',))
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      ('run','project',store.now(),store.now(),'success','test'))
        for sid in ('capture','other'):
            store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (sid,'run','project',SOURCE,'page',store.now(),store.digest('<main>'+sid+'</main>'),
                           store.digest('Guideline 10'), 'Guideline 10','[]',None,None,None))

    def snapshot(self,sid='capture'):
        return store.one('SELECT * FROM snapshots WHERE id=?',(sid,))

    def mark(self,sid='capture'):
        return json.loads(store.one('SELECT value FROM state WHERE key=?',(PREFIX+sid,))['value'])

    def save_mark(self,mark,sid='capture'):
        store.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(PREFIX+sid,json.dumps(mark)))

    def test_01_explicit_exclusion_is_bound_audited_and_preserves_archive(self):
        original=self.snapshot()
        self.assertTrue(usable_snapshot(original))
        result=exclude_snapshot('capture',REASON,VERIFICATION)
        self.assertTrue(result['updated'])
        self.assertFalse(usable_snapshot(original))
        mark=self.mark()
        self.assertEqual(mark['policy'],POLICY)
        for saved,field in (('snapshot_id','id'),('project_id','project_id'),('url','url'),
                            ('sha256','sha256'),('text_sha256','text_sha256')):
            self.assertEqual(mark[saved],original[field])
        self.assertEqual(mark['verification'],VERIFICATION)
        self.assertEqual(self.snapshot(),original)
        self.assertEqual(len(store.rows('SELECT * FROM snapshots')),2)
        self.assertEqual(store.rows('SELECT * FROM change_reviews'),[])
        events=store.rows('SELECT * FROM events')
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['run_id'],'run')
        self.assertIn(REASON,events[0]['message'])

    def test_02_identical_repeat_keeps_timestamp_and_has_no_duplicate_event(self):
        first=exclude_snapshot('capture',REASON,VERIFICATION)
        raw=store.one('SELECT value FROM state WHERE key=?',(PREFIX+'capture',))
        second=exclude_snapshot('capture','  '+REASON+'  ',dict(VERIFICATION))
        self.assertFalse(second['updated'])
        self.assertEqual(second['excluded_at'],first['excluded_at'])
        self.assertEqual(store.one('SELECT value FROM state WHERE key=?',(PREFIX+'capture',)),raw)
        self.assertEqual(len(store.rows('SELECT * FROM events')),1)

    def test_03_another_snapshot_hash_url_or_project_never_inherits_exclusion(self):
        exclude_snapshot('capture',REASON,VERIFICATION)
        self.assertTrue(usable_snapshot(self.snapshot('other')))
        original=self.snapshot()
        for field,value in (('id','other'),('project_id','another-project'),('url',SOURCE+'&different=1'),
                            ('sha256',store.digest('changed original')),('text_sha256',store.digest('Guideline 11')),
                            ('text','Guideline 11'),('kind','document')):
            with self.subTest(field=field):
                self.assertTrue(usable_snapshot({**original,field:value}))

    def test_04_invalid_policy_json_fields_and_verification_exclude_nothing(self):
        exclude_snapshot('capture',REASON,VERIFICATION)
        valid=self.mark()
        variants=[None,[],{**valid,'policy':'unverified-policy'},
                  {k:v for k,v in valid.items() if k!='sha256'},
                  {**valid,'unexpected':'metadata'}, {**valid,'reason':'too short'},
                  {**valid,'verification':{}}, {**valid,'verification':[]},
                  {**valid,'excluded_at':'not-a-time'}, {**valid,'excluded_at':'2026-11-06T10:00:00'}]
        for value in variants:
            with self.subTest(value=value):
                self.save_mark(value)
                self.assertTrue(usable_snapshot(self.snapshot()))
        store.execute('UPDATE state SET value=? WHERE key=?',('{broken JSON',PREFIX+'capture'))
        self.assertTrue(usable_snapshot(self.snapshot()))

    def test_05_missing_document_and_incomplete_snapshots_cannot_be_marked(self):
        with self.assertRaises(ValueError):exclude_snapshot('missing',REASON,VERIFICATION)
        for field,value in (('kind','document'),('sha256','invalid'),('text_sha256',''),
                            ('text',''),('url','not-an-http-source')):
            original=self.snapshot()
            store.execute('UPDATE snapshots SET '+field+'=? WHERE id=?',(value,'capture'))
            with self.subTest(field=field),self.assertRaises(ValueError):
                exclude_snapshot('capture',REASON,VERIFICATION)
            store.execute('UPDATE snapshots SET '+field+'=? WHERE id=?',(original[field],'capture'))
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?',(PREFIX+'capture',)))
        self.assertEqual(store.rows('SELECT * FROM events'),[])

    def test_06_invalid_administrative_input_is_rejected_without_state_changes(self):
        for reason,verification in (('short',VERIFICATION),(REASON,None),(REASON,{}),(REASON,[]),
                                    (REASON,{'unsupported':object()}),(REASON,{'number':float('nan')})):
            with self.subTest(reason=reason,verification_type=type(verification).__name__),self.assertRaises(ValueError):
                exclude_snapshot('capture',reason,verification)
        self.assertIsNone(store.one('SELECT value FROM state WHERE key=?',(PREFIX+'capture',)))
        self.assertEqual(store.rows('SELECT * FROM events'),[])

    def test_07_changed_admin_verification_is_explicit_and_does_not_change_capture(self):
        before=self.snapshot()
        exclude_snapshot('capture',REASON,VERIFICATION)
        newer={**VERIFICATION,'additional_proof':'Both independent categories were inspected.'}
        self.assertTrue(exclude_snapshot('capture',REASON,newer)['updated'])
        self.assertEqual(self.mark()['verification'],newer)
        self.assertEqual(len(store.rows('SELECT * FROM events')),2)
        self.assertEqual(self.snapshot(),before)

    def test_08_unmarked_and_invalid_snapshot_arguments_default_to_usable(self):
        for snapshot in (None,{}, {'id':'missing'},self.snapshot()):
            self.assertTrue(usable_snapshot(snapshot))
        exclude_snapshot('capture',REASON,VERIFICATION)
        # A marker copied under another key cannot exclude that other row.
        self.save_mark(self.mark(),'other')
        self.assertTrue(usable_snapshot(self.snapshot('other')))


if __name__=='__main__':unittest.main(verbosity=2)
