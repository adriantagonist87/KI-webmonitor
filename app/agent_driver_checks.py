"""Deterministic agent-control checks; no provider, browser or private input."""
import asyncio
import copy
import json
import unittest
from agent_driver import run_agent,TOOLS,AGENT_PROMPT,REPORT_PROMPT,_tool_error
from disclosure_actions import DisclosureError

CANARY='PRIVATE-CONTENT-AND-ARGUMENT-CANARY'


def answer(name=None,args=None,reason='tool_calls',content=CANARY):
    message={'content':content}
    if name is not None:
        message['tool_calls']=[{'id':'fake-call','type':'function','function':{
            'name':name,'arguments':args if isinstance(args,str) else json.dumps(args)}}]
    return {'choices':[{'finish_reason':reason,'message':message}],
            'usage':{'completion_tokens':1800}}


def finish():
    return answer('finish',{'complete':True,'checked':['Sources checked'],
                            'pending':[],'findings':'Prüfung abgeschlossen.'})


class AgentDriverTests(unittest.TestCase):
    def run_dialogue(self,responses,dispatch=None,model='local-test'):
        requests=[];logs=[];actions=[];screens=[]
        async def llm(messages,model,**kwargs):
            requests.append({'messages':copy.deepcopy(messages),**kwargs})
            if len(requests)>len(responses):
                raise AssertionError('The controller exceeded the simulated response budget.')
            return copy.deepcopy(responses[len(requests)-1])
        async def perform(name,args):
            actions.append((name,args))
            if dispatch is not None:return await dispatch(name,args)
            return {'sources':[{'url':'https://example.org/source','text':'Read source'}]}
        async def screen():
            screens.append(True)
            return {'type':'text','text':'Synthetic viewport'}
        result=asyncio.run(run_agent('Read requested sources. '+CANARY,{},model,
                                    llm,perform,screen,logs.append,max_steps=24))
        self.assertNotIn(CANARY,'\n'.join(logs))
        return result,requests,logs,actions,screens
    def test_01_three_length_responses_stop_after_three_calls_without_dispatch(self):
        cut=answer('read_sources',{'urls':['https://example.org/'+CANARY]},reason='length')
        result,requests,logs,actions,_=self.run_dialogue([cut,cut,cut])
        self.assertEqual(len(requests),3)
        self.assertEqual([r['max_tokens'] for r in requests],[1800,3600,3600])
        self.assertEqual(actions,[])
        self.assertFalse(result['complete'])
        self.assertTrue(result['pending'])
        self.assertIn('drei aufeinanderfolgenden',' '.join(result['pending']))
        self.assertTrue(any('Abschluss=length' in line for line in logs))
        prompts=[m['content'] for m in requests[-1]['messages'] if m['role']=='user' and isinstance(m['content'],str)]
        self.assertEqual(sum('vorige Werkzeugantwort war abgeschnitten' in text for text in prompts),1)
    def test_02_length_then_short_finish_recovers(self):
        result,requests,logs,actions,_=self.run_dialogue([answer(reason='length'),finish()],model='mlx-community/Qwen3.8-27B-4bit')
        self.assertTrue(result['complete']);self.assertEqual(result['pending'],[])
        self.assertEqual(len(requests),2);self.assertEqual(actions,[])
        self.assertEqual(requests[1]['max_tokens'],3600)
        self.assertNotIn('chat_template_kwargs',requests[0])
        self.assertEqual(requests[1]['chat_template_kwargs'],{'enable_thinking':False})
        self.assertTrue(any('KI-Werkzeug: finish'==line for line in logs))
    def test_03_normal_read_and_finish_are_unchanged(self):
        args={'urls':['https://example.org/source']}
        result,requests,_,actions,screens=self.run_dialogue([answer('read_sources',args),finish()])
        self.assertTrue(result['complete']);self.assertEqual(len(requests),2)
        self.assertEqual(actions,[('read_sources',args)])
        self.assertEqual(len(result['actions']),2)
        self.assertEqual(len(screens),2)
        self.assertTrue(any(m['role']=='tool' and 'Read source' in m.get('content','') for m in requests[1]['messages']))
    def test_04_unknown_and_invalid_tools_count_toward_the_limit(self):
        foreign=answer('unknown_'+CANARY,{'private':CANARY})
        broken=answer('read_sources','{')
        result,requests,logs,actions,_=self.run_dialogue([foreign,broken,foreign])
        self.assertFalse(result['complete']);self.assertEqual(len(requests),3)
        self.assertEqual(actions,[])
        self.assertEqual(logs.count('KI-Werkzeug: UnbekanntesWerkzeug'),2)
        self.assertTrue(any(m['role']=='tool' and 'error' in m.get('content','') for m in requests[1]['messages']))
    def test_05_failed_dispatches_and_returned_errors_do_not_reset_the_counter(self):
        async def raised(name,args):raise RuntimeError(CANARY)
        async def returned(name,args):return {'sources':[{'url':'https://example.org/source','error':CANARY}]}
        response=answer('read_sources',{'urls':['https://example.org/source']})
        for dispatch in (raised,returned):
            with self.subTest(dispatch=dispatch.__name__):
                result,requests,_,actions,_=self.run_dialogue([response,response,response],dispatch)
                self.assertFalse(result['complete']);self.assertEqual(len(requests),3)
                self.assertEqual(len(actions),3)
                self.assertTrue(any(m['role']=='tool' and CANARY in m.get('content','') for m in requests[1]['messages']))
    def test_06_successful_plan_and_read_reset_consecutive_failures(self):
        responses=[answer(reason='length'),answer(reason='length'),
                   answer('plan',{'items':[CANARY]}),
                   answer('unknown_'+CANARY,{'private':CANARY}),
                   answer('read_sources',{'urls':['https://example.org/source']}),finish()]
        result,requests,logs,actions,_=self.run_dialogue(responses)
        self.assertTrue(result['complete']);self.assertEqual(len(requests),6)
        self.assertEqual(len(actions),1)
        self.assertTrue(any('KI-Prüfplan gespeichert (1 Punkte).'==line for line in logs))
        self.assertTrue(all(request['max_tokens']<=3600 for request in requests))
    def test_07_other_models_keep_their_template_options_unchanged(self):
        result,requests,_,_,_=self.run_dialogue([answer(reason='length'),finish()],model='other-provider/Local-Vision-27B')
        self.assertTrue(result['complete'])
        self.assertTrue(all('chat_template_kwargs' not in request for request in requests))

    def test_08_deadline_cancels_waiting_model_and_tool_and_reports_incomplete(self):
        for blocked in ('model','tool'):
            with self.subTest(blocked=blocked):
                cancelled=[]
                async def waiting():
                    try:await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        cancelled.append(blocked)
                        raise
                async def llm(*args,**kwargs):
                    if blocked=='model':await waiting()
                    return answer('read_sources',{'urls':['https://example.org/source']})
                async def dispatch(*args,**kwargs):await waiting()
                async def screen():return {'type':'text','text':'Synthetic viewport'}
                result=asyncio.run(run_agent('Read the source',{},'local-test',llm,dispatch,screen,
                                            lambda text:None,deadline=0.03))
                self.assertFalse(result['complete'])
                self.assertEqual(cancelled,[blocked])
                self.assertIn('Zeitgrenze',' '.join(result['pending']))

    def test_09_source_reads_finish_without_per_change_review_rounds(self):
        offered={tool['function']['name'] for tool in TOOLS}
        self.assertNotIn('assess_change',offered)
        self.assertNotIn('assess_change',AGENT_PROMPT)
        self.assertTrue({'read_sources','read_archive','check_documents','finish'}.issubset(offered))
        source={'urls':['https://example.org/source']}
        archived={'url':'https://example.org/source'}
        result,requests,_,actions,_=self.run_dialogue([
            answer('read_sources',source),answer('read_archive',archived),answer('check_documents',{}),finish()])
        self.assertTrue(result['complete'])
        self.assertEqual(result['pending'],[])
        self.assertEqual(len(requests),4)
        self.assertEqual(actions,[('read_sources',source),('read_archive',archived),('check_documents',{})])
        self.assertEqual([action['tool'] for action in result['actions']],
                         ['read_sources','read_archive','check_documents','finish'])
        for request in requests:
            self.assertNotIn('assess_change',{tool['function']['name'] for tool in request['tools']})
        # A model cannot dispatch the retired tool even if it hallucinates it.
        rejected=answer('assess_change',{'change_id':'not-a-browser-task','relevance':'irrelevant',
                                       'reason':'A synthetic retired tool call.','evidence_quote':'No source quote.'})
        recovered,_,_,dispatched,_=self.run_dialogue([rejected,answer('read_sources',source),finish()])
        self.assertTrue(recovered['complete'])
        self.assertEqual(dispatched,[('read_sources',source)])

    def test_10_observed_context_survives_short_error_and_can_be_used_without_guessing(self):
        selector='#'+('observed-container-'+'a'*200)
        option={'label':'Technical documents','container_selector':selector,'kind':'disclosure',
                'disclosure_type':'aria','expanded':False,'heading':'Actual section'}
        async def dispatch(name,args):
            if args.get('container_selector')!=selector:
                raise DisclosureError('Context is ambiguous. '*30,{'contexts':[option],'context_count':1,
                    'contexts_truncated':False,'private':CANARY})
            return {'source':{'url':'https://example.org/source','text':'Archived confirmed section'}}
        result,requests,_,actions,_=self.run_dialogue([
            answer('expand_section',{'label':'Technical documents'}),
            answer('expand_section',{'label':'Technical documents','container_selector':selector}),finish()],dispatch)
        self.assertTrue(result['complete'])
        errors=[json.loads(m['content']) for m in requests[1]['messages'] if m['role']=='tool']
        self.assertEqual(errors[0]['contexts'],[option])
        self.assertEqual(errors[0]['context_count'],1)
        self.assertFalse(errors[0]['contexts_truncated'])
        self.assertEqual(len(errors[0]['error']),240)
        self.assertGreater(len(json.dumps(errors[0]['contexts'])),240)
        self.assertNotIn('private',errors[0])
        self.assertEqual(actions[1][1]['container_selector'],selector)

    def test_11_valid_but_failed_disclosures_are_not_misreported_as_model_truncation(self):
        async def blocked(name,args):raise DisclosureError('Kein bestätigter Klappbereich.')
        response=answer('expand_section',{'label':'Observed heading is not a control'})
        result,requests,_,actions,_=self.run_dialogue([response,response,response],blocked)
        self.assertFalse(result['complete']);self.assertEqual(len(requests),3)
        self.assertEqual(len(actions),3)
        pending=' '.join(result['pending'])
        self.assertIn('fehlgeschlagenen Werkzeugaktionen',pending)
        self.assertIn('expand_section',pending)
        self.assertNotIn('abgeschnitten',pending)
        self.assertNotIn('ungültig',pending)
        self.assertEqual(result['stop_cause'],{'kind':'tool_error','tool':'expand_section'})

    def test_12_mixed_failures_preserve_last_actual_cause_without_increasing_budget(self):
        async def blocked(name,args):raise DisclosureError('Kein bestätigter Klappbereich.')
        response=answer('expand_section',{'label':'No control'})
        result,requests,_,_,_=self.run_dialogue([response,response,answer(reason='length')],blocked)
        self.assertFalse(result['complete']);self.assertEqual(len(requests),3)
        self.assertEqual(result['stop_cause'],{'kind':'model_length'})
        self.assertIn('Steuerungs- oder Werkzeugrunden',' '.join(result['pending']))
        self.assertIn('Letzte Ursache: Modellantwort abgeschnitten',' '.join(result['pending']))
        self.assertEqual([r['max_tokens'] for r in requests],[1800,1800,1800])

    def test_13_structured_error_options_are_bounded_and_unknown_fields_never_forwarded(self):
        option={'label':'Documents','container_selector':'#observed','kind':'arbitrary',
                'disclosure_type':'unsafe','expanded':'not-a-boolean','heading':'h'*300,'secret':CANARY}
        options=[{'label':'x','container_selector':'x'*241}]+[dict(option) for _ in range(12)]
        error=_tool_error(DisclosureError('Short error',{'contexts':options,'context_count':120,
                                                         'contexts_truncated':True,'secret':CANARY}))
        self.assertLessEqual(len(error['contexts']),8)
        self.assertTrue(error['contexts_truncated']);self.assertEqual(error['context_count'],120)
        for item in error['contexts']:
            self.assertEqual(item['kind'],'disclosure');self.assertEqual(len(item['heading']),160)
            self.assertNotIn('disclosure_type',item);self.assertNotIn('expanded',item)
        self.assertNotIn(CANARY,json.dumps(error))

    def test_14_hash_only_documents_do_not_turn_into_ocr_or_document_text_read_tasks(self):
        for prompt in (AGENT_PROMPT,REPORT_PROMPT):
            self.assertIn('ausschliesslich als vollstaendige Originaldateien',prompt)
            self.assertIn('Keine automatische Dokumenttextanalyse, OCR oder Bilderkennung',prompt)
            self.assertIn('Webseiten und HTML-Artikel weiterhin',prompt)
            self.assertIn('Ein Texthash ist dafuer nicht erforderlich',prompt)
            self.assertNotIn('original_sha256_present=true UND text_sha256_present=true',prompt)
            self.assertNotIn('extraction.method=ocr_budget_limited',prompt)
        descriptions={tool['function']['name']:tool['function']['description'] for tool in TOOLS}
        self.assertIn('No document text analysis or OCR',descriptions['check_documents'])
        self.assertIn('For downloadable files use check_documents',descriptions['read_sources'])
        self.assertIn('HTML source',descriptions['read_archive'])


if __name__=='__main__':unittest.main(verbosity=2)
