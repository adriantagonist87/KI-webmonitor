"""Conservative self-link classification against an isolated real archive DB."""
import json
import os
from pathlib import Path
import tempfile
import unittest

scratch=tempfile.TemporaryDirectory(prefix='.kiwm-change-review-',dir=os.environ.get('DATA_DIR','/data'))
os.environ['DATA_DIR']=scratch.name
import store
from change_review import assess_change,review_self_link_change,review_self_link_changes,SELF_LINK_POLICY


SOURCE='https://sources.test/publications?category=fire'
DOCUMENT='https://sources.test/guideline.pdf'


class ChangeReviewChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init()
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)',
                      ('project','Source review',SOURCE,'Monitor guideline content','sources.test','0 6 * * *',store.now(),store.now()))

    def setUp(self):
        self.counter=0
        with store.db() as c:
            for table in ('change_reviews','changes','snapshots','events','runs'):
                c.execute('DELETE FROM '+table)

    def change(self, *, old_text='Stand 2026-11. Frist 10 Tage.', new_text=None,
               old_links=None,new_links=None,source=SOURCE,kind='Links geändert',
               old_kind='page',new_kind='page',evidence=None):
        self.counter+=1
        suffix=str(self.counter)
        before_run,after_run='before-'+suffix,'after-'+suffix
        old_links=[DOCUMENT] if old_links is None else old_links
        new_links=[DOCUMENT,source+'#top'] if new_links is None else new_links
        if new_text is None:new_text=old_text
        for rid in (before_run,after_run):
            store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                          (rid,'project',store.now(),store.now(),'success','test'))
        for sid,rid,text,links,snapshot_kind in (('old-'+suffix,before_run,old_text,old_links,old_kind),
                                                ('new-'+suffix,after_run,new_text,new_links,new_kind)):
            store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (sid,rid,'project',source,snapshot_kind,store.now(),store.digest('raw-'+sid),
                           store.digest(text),text,json.dumps(links),None,None,None))
        if evidence is None:
            evidence={'links_added':sorted(set(new_links)-set(old_links)),
                      'links_removed':sorted(set(old_links)-set(new_links))}
        cid='change-'+suffix
        store.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (cid,'project',after_run,source,kind,store.now(),'old-'+suffix,'new-'+suffix,
                       json.dumps(evidence,ensure_ascii=False),'fingerprint-'+suffix))
        return cid,after_run

    def detail(self,cid):
        return json.loads(store.one('SELECT evidence FROM changes WHERE id=?',(cid,))['evidence'])

    def assert_untouched(self,cid):
        before=store.one('SELECT * FROM changes WHERE id=?',(cid,))
        self.assertIsNone(review_self_link_change(cid))
        self.assertEqual(store.one('SELECT * FROM changes WHERE id=?',(cid,)),before)
        self.assertEqual(store.rows('SELECT * FROM events'),[])

    def test_01_added_self_link_is_proven_and_retains_raw_evidence(self):
        cid,rid=self.change()
        before=store.one('SELECT * FROM changes WHERE id=?',(cid,))
        snapshots=store.rows('SELECT * FROM snapshots ORDER BY id')
        result=review_self_link_change(cid,rid)
        self.assertTrue(result['updated'])
        detail=self.detail(cid)
        self.assertEqual(detail['ai_assessment']['relevance'],'irrelevant')
        self.assertEqual(detail['ai_assessment']['policy'],SELF_LINK_POLICY)
        self.assertEqual(detail['ai_assessment']['method'],'deterministic')
        self.assertEqual(detail['ai_assessment']['evidence_quote'],SOURCE+'#top')
        self.assertEqual({k:v for k,v in detail.items() if k!='ai_assessment'},json.loads(before['evidence']))
        after=store.one('SELECT * FROM changes WHERE id=?',(cid,))
        self.assertEqual({k:v for k,v in before.items() if k!='evidence'},
                         {k:v for k,v in after.items() if k!='evidence'})
        self.assertEqual(store.rows('SELECT * FROM snapshots ORDER BY id'),snapshots)
        self.assertEqual(store.rows('SELECT * FROM change_reviews'),[])

    def test_02_removed_or_changed_fragment_self_link_is_equivalent(self):
        for old,new in (([DOCUMENT,SOURCE],[DOCUMENT]),
                        ([DOCUMENT,SOURCE+'#before'],[DOCUMENT,SOURCE+'#after'])):
            with self.subTest(old=old,new=new):
                cid,_=self.change(old_links=old,new_links=new)
                self.assertTrue(review_self_link_change(cid)['updated'])

    def test_03_query_path_scheme_host_and_trailing_slash_changes_stay_visible(self):
        for target in ('https://sources.test/publications?category=security',
                       'https://sources.test/other?category=fire',
                       'http://sources.test/publications?category=fire',
                       'https://other.test/publications?category=fire',
                       'https://sources.test/publications/?category=fire'):
            with self.subTest(target=target):
                cid,_=self.change(new_links=[DOCUMENT,target])
                self.assert_untouched(cid)

    def test_04_real_added_or_removed_document_is_not_hidden_with_self_link(self):
        for old,new in (([DOCUMENT],[DOCUMENT,SOURCE,DOCUMENT+'?version=2']),
                        ([DOCUMENT],[SOURCE])):
            cid,_=self.change(old_links=old,new_links=new)
            self.assert_untouched(cid)

    def test_05_single_digit_text_change_cannot_be_hidden(self):
        cid,_=self.change(new_text='Stand 2026-11. Frist 11 Tage.')
        self.assert_untouched(cid)

    def test_06_document_or_visual_or_other_evidence_cannot_be_hidden(self):
        for kwargs in ({'old_kind':'document'},{'new_kind':'document'},
                       {'kind':'Links geändert; Visuelle Abweichung'},
                       {'evidence':{'links_added':[SOURCE+'#top'],'links_removed':[],'visual_ratio':0.2}},
                       {'evidence':{'links_added':[SOURCE+'#top'],'links_removed':[],'text_diff':'-10\n+11'}},
                       {'evidence':{'links_added':[SOURCE+'#top'],'links_removed':[],'collection':{'added':[]}}}):
            with self.subTest(kwargs=kwargs):
                cid,_=self.change(**kwargs)
                self.assert_untouched(cid)

    def test_07_exact_snapshot_link_deltas_are_required(self):
        for evidence in ({'links_added':[SOURCE+'#forged'],'links_removed':[]},
                         {'links_added':[],'links_removed':[]},
                         {'links_added':[SOURCE+'#top',DOCUMENT],'links_removed':[]}):
            cid,_=self.change(evidence=evidence)
            self.assert_untouched(cid)

    def test_08_previous_ai_decision_is_preserved_and_recheck_is_idempotent(self):
        cid,rid=self.change()
        old={'relevance':'relevant','reason':'A model called this a content update.',
             'reviewed_at':'2026-11-05T10:00:00+00:00','evidence_quote':SOURCE+'#top'}
        detail=self.detail(cid);detail['ai_assessment']=old
        detail['assessment_history']=[{'assessment':{'relevance':'uncertain'},'legacy':True}]
        store.execute('UPDATE changes SET evidence=? WHERE id=?',(json.dumps(detail),cid))
        self.assertTrue(review_self_link_change(cid)['updated'])
        updated=self.detail(cid)
        self.assertEqual(updated['assessment_history'][0],detail['assessment_history'][0])
        self.assertEqual(updated['assessment_history'][-1]['assessment'],old)
        self.assertEqual(updated['assessment_history'][-1]['replaced_by_policy'],SELF_LINK_POLICY)
        before=store.one('SELECT evidence FROM changes WHERE id=?',(cid,))
        events=store.rows('SELECT * FROM events')
        self.assertFalse(review_self_link_change(cid,rid)['updated'])
        self.assertEqual(review_self_link_changes(rid),[])
        self.assertEqual(store.one('SELECT evidence FROM changes WHERE id=?',(cid,)),before)
        self.assertEqual(store.rows('SELECT * FROM events'),events)

    def test_09_wrong_run_missing_snapshot_and_source_mismatch_do_not_apply(self):
        cid,rid=self.change()
        self.assertIsNone(review_self_link_change(cid,'another-run'))
        self.assertIsNone(review_self_link_change('missing'))
        store.execute('UPDATE snapshots SET url=? WHERE id=?',(SOURCE+'&other=1','old-1'))
        self.assert_untouched(cid)
        store.execute('DELETE FROM snapshots WHERE id=?',('old-1',))
        self.assert_untouched(cid)

    def test_10_exact_link_url_is_valid_model_irrelevance_quote(self):
        for old,new in (([DOCUMENT],[DOCUMENT,SOURCE+'#top']),
                        ([DOCUMENT,SOURCE+'#top'],[DOCUMENT])):
            cid,rid=self.change(old_links=old,new_links=new)
            assess_change(rid,cid,'irrelevant','Only a self-link changed on the existing page.',SOURCE+'#top')
            self.assertEqual(self.detail(cid)['ai_assessment']['relevance'],'irrelevant')
            with self.assertRaises(ValueError):
                assess_change(rid,cid,'irrelevant','A model must not invent its evidence link.',SOURCE+'#invented')

    def test_11_run_wrapper_changes_only_proven_rows(self):
        cid,rid=self.change()
        other,_=self.change(new_text='Stand 2026-11. Frist 11 Tage.')
        store.execute('UPDATE changes SET run_id=? WHERE id=?',(rid,other))
        store.execute('UPDATE snapshots SET run_id=? WHERE id=?',(rid,'new-2'))
        result=review_self_link_changes(rid)
        self.assertEqual([r['change_id'] for r in result],[cid])
        self.assertNotIn('ai_assessment',self.detail(other))
        self.assertEqual(review_self_link_changes(rid),[])

    def test_12_only_archive_whitespace_normalization_is_allowed(self):
        cid,_=self.change(old_text='Stand 2026-11.\r\nFrist 10 Tage.  ',new_text='Stand 2026-11.\nFrist 10 Tage.')
        self.assertTrue(review_self_link_change(cid)['updated'])
        other,_=self.change(old_text='Stand 2026-11. Frist 10 Tage.',new_text='Frist 10 Tage. Stand 2026-11.')
        before=store.one('SELECT * FROM changes WHERE id=?',(other,))
        self.assertIsNone(review_self_link_change(other))
        self.assertEqual(store.one('SELECT * FROM changes WHERE id=?',(other,)),before)

    def test_13_forged_policy_does_not_skip_snapshot_validation(self):
        cid,_=self.change(new_text='Stand 2026-11. Frist 11 Tage.')
        detail=self.detail(cid);detail['ai_assessment']={'relevance':'irrelevant','method':'deterministic',
            'policy':SELF_LINK_POLICY,'evidence_quote':SOURCE+'#top'}
        store.execute('UPDATE changes SET evidence=? WHERE id=?',(json.dumps(detail),cid))
        self.assert_untouched(cid)

    def test_14_invalid_shapes_and_unrecognized_history_remain_untouched(self):
        for evidence in ({'links_added':'not-a-list'},
                         {'links_added':[SOURCE+'#top'],'links_removed':[],'assessment_history':{'legacy':True}},
                         ['not-an-evidence-object']):
            cid,_=self.change(evidence=evidence)
            self.assert_untouched(cid)


if __name__=='__main__':unittest.main(verbosity=2)
