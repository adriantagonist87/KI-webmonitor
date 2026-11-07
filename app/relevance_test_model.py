"""Offline model fixture for the real final-review pipeline, never a provider.

Only the exact final-review contract is accepted. Browser, report, vision or
other model requests fail instead of silently reaching an external service.
"""
import json

from relevance_review import REVIEW_TOOL,SYSTEM_PROMPT,POLICY,BATCH_SIZE


async def review_only_model(messages,model,**kwargs):
    assert model=='synthetic-no-model','Only the synthetic test model is permitted.'
    assert kwargs=={'tools':[REVIEW_TOOL],'tool_choice':'required','temperature':0,'max_tokens':3600}, \
        'Unexpected model operation: only the final assessment tool is permitted.'
    assert REVIEW_TOOL['function']['name']=='record_change_assessments'
    assert isinstance(messages,list) and len(messages)==2
    assert messages[0]=={'role':'system','content':SYSTEM_PROMPT}
    assert messages[1]['role']=='user' and isinstance(messages[1]['content'],str)
    payload=json.loads(messages[1]['content'])
    assert set(payload)=={'project_task','project_task_truncated','changes'}
    assert isinstance(payload['project_task'],str) and payload['project_task_truncated'] is False
    changes=payload['changes']
    assert isinstance(changes,list) and 1<=len(changes)<=BATCH_SIZE
    assert len({change['change_id'] for change in changes})==len(changes)
    assessments=[]
    for change in changes:
        assert change['snapshots_available'] is True
        proof=change['technical_proof']
        assert isinstance(proof,str)
        lines=proof.splitlines()
        links=[line for line in lines if line.startswith(('https://','http://'))]
        changed=[line for line in lines if line.startswith(('+','-')) and not line.startswith(('+++','---'))]
        numbered=[line for line in changed if any(character.isdigit() for character in line)]
        kinds=set(change['kind'].split('; '))
        quote=(links or numbered or changed or [''])[0]
        # These fixtures deliberately change guideline text, dates or actual
        # document links. Technical-only byte changes without proof stay hints.
        relevant=bool(quote and kinds.intersection({'Inhalt geändert','Links geändert'}))
        if relevant:
            relevance='relevant'
            reason='Der archivierte Unterschied belegt die im synthetischen Prüfauftrag überwachte Text- oder Linkänderung.'
        else:
            relevance='uncertain'
            reason='Der synthetische technische Unterschied belegt allein keine fachliche Inhaltsänderung.'
            quote=''
        assert not quote or quote in proof
        assessments.append({'change_id':change['change_id'],'relevance':relevance,
                            'reason':reason,'evidence_quote':quote})
    return {'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':[
        {'id':'synthetic-final-review','type':'function','function':{
            'name':'record_change_assessments','arguments':json.dumps({'assessments':assessments})}}]}}]}


def assert_final_review(test,evidence,run_id,model,expected_calls,expected_uncertain=0):
    """Assert persisted real review output, independent of the report stub."""
    import store
    from change_review import DOCUMENT_HASH_POLICY,review_document_hash_change
    changes=store.rows('SELECT * FROM changes WHERE run_id=? AND previous_id IS NOT NULL',(run_id,))
    review=evidence['relevance_review']
    test.assertEqual(review,{'count':len(changes),'reviewed':len(changes),
                            'uncertain':expected_uncertain,'errors':[]})
    test.assertEqual(model.await_count,expected_calls)
    if expected_calls==0:model.assert_not_awaited()
    elif expected_calls==1:model.assert_awaited_once()
    for snapshot in store.rows("SELECT * FROM snapshots WHERE run_id=? AND kind='document'",(run_id,)):
        test.assertEqual(snapshot['text'],'');test.assertEqual(snapshot['text_sha256'],store.digest(''))
        test.assertEqual(store.digest((store.DATA/snapshot['file']).read_bytes()),snapshot['sha256'])
        sidecar=store.read_json(store.DATA/'artifacts'/run_id/(snapshot['id']+'.extraction.json'))
        test.assertEqual(sidecar['method'],'hash_only');test.assertEqual(sidecar['source_sha256'],snapshot['sha256'])
        test.assertFalse(sidecar['content_analysis_performed']);test.assertTrue(sidecar['original_sha256_verified'])
    actual_uncertain=0
    document_ids=set()
    for change in changes:
        detail=json.loads(change['evidence'])
        assessment=detail['ai_assessment']
        snapshots=[store.one('SELECT * FROM snapshots WHERE id=?',(change[key],)) for key in ('previous_id','current_id')]
        if any(snapshot and snapshot['kind']=='document' for snapshot in snapshots):
            document_ids.add(change['id'])
            test.assertEqual(change['kind'],'Datei geändert')
            test.assertEqual(assessment['policy'],DOCUMENT_HASH_POLICY)
            test.assertEqual(assessment['method'],'deterministic')
            test.assertEqual(assessment['relevance'],'relevant')
            test.assertNotIn('model',assessment);test.assertNotIn('text_diff',detail)
            for snapshot in snapshots:
                test.assertIsNotNone(snapshot);test.assertEqual(snapshot['kind'],'document')
                test.assertEqual(snapshot['text'],'')
                test.assertEqual(store.digest((store.DATA/snapshot['file']).read_bytes()),snapshot['sha256'])
            test.assertNotEqual(snapshots[0]['sha256'],snapshots[1]['sha256'])
            test.assertEqual(detail['document_comparison']['previous_sha256'],snapshots[0]['sha256'])
            test.assertEqual(detail['document_comparison']['current_sha256'],snapshots[1]['sha256'])
            verified=review_document_hash_change(change['id'],run_id)
            test.assertIsNotNone(verified);test.assertTrue(verified['originals_verified'])
            test.assertFalse(verified['updated'])
            continue
        test.assertEqual(assessment['policy'],POLICY)
        test.assertEqual(assessment['method'],'final_model_review')
        test.assertEqual(assessment['model'],'synthetic-no-model')
        test.assertNotIn('review_error',assessment)
        if assessment['relevance']=='uncertain':
            actual_uncertain+=1
            test.assertEqual(assessment['evidence_quote'],'')
        else:
            test.assertEqual(assessment['relevance'],'relevant')
            test.assertTrue(assessment['evidence_quote'])
            proof=detail.get('text_diff','')+'\n'+'\n'.join(detail.get('links_added',[])+detail.get('links_removed',[]))
            test.assertIn(assessment['evidence_quote'],proof)
    for call in model.await_args_list:
        payload=json.loads(call.args[0][1]['content'])
        test.assertTrue(document_ids.isdisjoint(change['change_id'] for change in payload['changes']))
    test.assertEqual(actual_uncertain,expected_uncertain)
