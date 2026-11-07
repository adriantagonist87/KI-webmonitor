"""Exact report-source checks; synthetic evidence, no model or network calls."""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

scratch=tempfile.TemporaryDirectory(prefix='.report-source-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store
store.CONFIG=Path(scratch.name)/'settings.json'
store.SECRETS=Path(scratch.name)/'credentials.json'
from report_sources import unknown_report_links
import engine

SOURCE='https://example.org/issues/'
ARTICLE='https://example.org/actual-article-id/'
DOCUMENT='https://example.org/document.pdf?version=2&download=1'
FAKE='https://invented.example/fake'


def evidence():
    return {'task':'List the observed titles as Markdown links.',
            'observations':[{'url':SOURCE,'kind':'page','text':'A source page',
                'metadata':{'links':[{'url':ARTICLE,'label':'Actual article title'},DOCUMENT]}}],
            'changes':[],'vision':[]}


def response(text):
    return {'choices':[{'finish_reason':'stop','message':{'content':text}}]}


class ReportSourceTests(unittest.TestCase):
    def test_01_observed_page_article_and_document_urls_are_allowed(self):
        report='[Source]('+SOURCE+')\n\n### [Different visible title]('+ARTICLE+')\n\n[PDF]('+DOCUMENT+')'
        self.assertEqual(unknown_report_links(report,evidence()),[])
    def test_02_technical_change_links_are_allowed_in_dict_and_json(self):
        detail={'links_added':['https://example.org/new/'],
                'links_removed':[{'url':'https://example.org/removed/'}],
                'ai_assessment':{'reason':FAKE}}
        report='[Changed](https://example.org/changed/) [Added](https://example.org/new/) [Removed](https://example.org/removed/) [Unsupported]('+FAKE+')'
        for serialized in (detail,json.dumps(detail)):
            with self.subTest(json_string=isinstance(serialized,str)):
                data={'observations':[],'changes':[{'url':'https://example.org/changed/','evidence':serialized}]}
                self.assertEqual(unknown_report_links(report,data),[FAKE])
    def test_03_href_is_checked_not_visible_label_or_url_prefix(self):
        invented='https://example.org/actual-article-id/invented'
        query=ARTICLE+'?version=1'
        no_slash=ARTICLE.rstrip('/')
        report='['+ARTICLE+']('+invented+') [Query]('+query+') [Slash]('+no_slash+') [Duplicate]('+invented+')'
        self.assertEqual(unknown_report_links(report,evidence()),[invented,query,no_slash])
    def test_04_ai_task_and_embedded_text_cannot_authorize_links(self):
        data={'task':FAKE,'agent':{'findings':FAKE,'checked':[FAKE]},
              'vision':[{'interpretation':FAKE}],FAKE:'This is only a dictionary key',
              'observations':[{'url':' '+FAKE,'text':FAKE,
                               'metadata':{'paragraphs':[FAKE],'links':[{'label':FAKE}]}}],
              'changes':[]}
        self.assertEqual(unknown_report_links('[Invented]('+FAKE+')',data),[FAKE])
    def test_05_local_anchors_are_ignored_and_html_links_are_checked(self):
        report='[Section](#answer) [Archive](/artifacts/source.txt) <a href="'+ARTICLE+'">Known</a> <a href="'+FAKE+'">Other</a>'
        self.assertEqual(unknown_report_links(report,evidence()),[FAKE])
    def test_06_summary_retries_unknown_url_then_accepts_exact_source(self):
        data=evidence();before=copy.deepcopy(data)
        invalid='### [Actual article title](https://example.org/title-derived-guess/)'
        valid='### [Actual article title]('+ARTICLE+')'
        with patch('engine.llm',new=AsyncMock(side_effect=[response(invalid),response(valid)])) as model:
            self.assertEqual(asyncio.run(engine.create_summary(data,'local-test')),valid)
            self.assertEqual(model.await_count,2)
            messages=model.call_args_list[1].args[0]
            self.assertIn('exakten URL-Zeichenfolgen',messages[-1]['content'])
            self.assertIn('title-derived-guess',messages[-1]['content'])
            first_payload=json.loads(messages[1]['content'])
            self.assertEqual(first_payload['observations'][0]['metadata']['links'][0]['url'],ARTICLE)
        self.assertEqual(data,before)
    def test_07_thirty_labeled_source_links_survive_without_mutating_evidence(self):
        links=[{'url':f'https://example.org/source-{number}/','label':f'Observed title {number}'} for number in range(30)]
        data={'observations':[{'url':SOURCE,'text':'List','metadata':{'links':links}}],
              'changes':[],'vision':[]}
        before=copy.deepcopy(data)
        summarized=engine.summary_evidence(data)
        metadata=summarized['observations'][0]['metadata']
        self.assertEqual(metadata['links'],links)
        self.assertEqual(metadata['link_count'],30)
        self.assertFalse(metadata['links_truncated'])
        self.assertEqual(data,before)
        crowded={**data,'observations':[copy.deepcopy(data['observations'][0]) for _ in range(30)]}
        untouched=copy.deepcopy(crowded)
        capped=engine.summary_evidence(crowded)['observations'][0]['metadata']
        self.assertEqual(capped['link_count'],30)
        self.assertTrue(capped['links_truncated'])
        self.assertGreaterEqual(len(capped['links']),6)
        self.assertEqual(crowded,untouched)

    def test_08_attention_reason_remains_visible_to_model_beyond_source_excerpt(self):
        data=evidence()
        data['observations'][0]['text']='Unveränderter Katalog. '*1000
        data['changes']=[{'id':'category-move','url':ARTICLE,'kind':'Inhalt geändert',
            'evidence':{'text_diff':'-Kategorie A\n+Kategorie B',
                        'ai_assessment':{'relevance':'relevant','reason':'Der bekannte Eintrag wurde von Kategorie A nach Kategorie B verschoben.','evidence_quote':'Kategorie B'}}}]
        before=copy.deepcopy(data)
        value=engine.summary_evidence(data)
        self.assertEqual(value['relevant_change_count'],1)
        self.assertEqual(value['relevant_changes'][0]['change_id'],'category-move')
        self.assertIn('Kategorie A nach Kategorie B',value['relevant_changes'][0]['reason'])
        self.assertTrue(value['observations'][0]['text_excerpt_truncated'])
        self.assertEqual(data,before)

    def test_09_irrelevant_uncertain_and_first_capture_are_not_promoted(self):
        data=evidence()
        data['changes']=[{'id':value,'url':ARTICLE,'kind':'Inhalt geändert',
                          'evidence':{'ai_assessment':{'relevance':value,'reason':'Keine gesicherte relevante Neuerung.'}}}
                         for value in ('irrelevant','uncertain')]
        data['changes'].append({'id':'baseline','url':DOCUMENT,'kind':'Neues Dokument','evidence':{}})
        value=engine.summary_evidence(data)
        self.assertEqual(value['relevant_changes'],[])
        self.assertEqual(value['relevant_change_count'],0)

    def test_10_report_instruction_requires_visible_findings_without_site_rules(self):
        self.assertIn('relevant_change_count > 0',engine.REPORT_PROMPT)
        self.assertIn('sichtbaren Berichtseinstieg',engine.REPORT_PROMPT)
        self.assertIn('früherer Läufe',engine.REPORT_PROMPT)

    def test_11_hash_change_is_visible_to_report_and_email_without_document_text(self):
        import attention,email_reports
        detail={'comparison_mode':'sha256','content_analysis_performed':False,
                'note':'Die Originaldatei hat sich geändert. Ihr Inhalt wird nicht analysiert.',
                'ai_assessment':{'relevance':'relevant','reason':'Die vollständig gespeicherten Originale haben unterschiedliche SHA-256-Prüfsummen.'}}
        change={'id':'binary-change','project_id':'project','run_id':'run','url':DOCUMENT,
                'kind':'Datei geändert','detected':'2026-11-06T13:00:00+00:00','evidence':detail}
        self.assertEqual(attention.priority(change['kind'],detail),'attention')
        data=evidence();data['changes']=[change]
        value=engine.summary_evidence(data)
        self.assertEqual(value['relevant_change_count'],1)
        self.assertEqual(value['relevant_changes'][0]['kind'],'Datei geändert')
        project={'id':'project','name':'Hashprojekt','url':SOURCE,'tags':['Arbeit']}
        run={'id':'run','project_id':'project','status':'success','started':'2026-11-06T12:59:00+00:00'}
        text,html=email_reports.compose([project],[run],[change],[],
            '2026-11-01T00:00:00+00:00','2026-11-07T00:00:00+00:00')
        self.assertIn('1 Änderungen',text)
        self.assertIn('Eine bereitgestellte Datei wurde geändert.',html)
        self.assertNotIn('Hinweis · Datei geändert',html)
        self.assertNotIn('SHA-256',html)


if __name__=='__main__':unittest.main(verbosity=2)
