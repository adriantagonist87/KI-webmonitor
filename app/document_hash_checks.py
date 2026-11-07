"""Current document runtime: real byte downloads/archives, no content processing."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from pypdf import PdfWriter, PageObject

scratch=tempfile.TemporaryDirectory(prefix='.document-hash-check-',dir=os.environ.get('DATA_DIR','/data'))
os.environ['DATA_DIR']=scratch.name
import store
import engine
import change_review
import relevance_review
import attention


SOURCE='https://source.test/file.pdf'


class Context:
    def __init__(self):
        self.cookies=AsyncMock(return_value=[{'name':'session','value':'PRIVATE_SESSION_CANARY'}])
        self.pages=[type('Page',(),{'evaluate':AsyncMock(return_value='Test Browser')})()]


class DocumentHashChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        store.init()
        with store.db() as connection:
            for table in ('change_reviews','changes','snapshots','events','runs','projects'):
                connection.execute('DELETE FROM '+table)
        self.project={'id':'hash-project','name':'Dateiüberwachung','url':'https://source.test/',
                      'prompt':'Überwache die verlinkten Originaldateien.','domains':'source.test'}
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,next_due,created) VALUES(?,?,?,?,?,?,?,?)',
            (self.project['id'],self.project['name'],self.project['url'],self.project['prompt'],self.project['domains'],
             '0 6 * * *',store.now(),store.now()))
        self.context=Context();self.responses=[];self.requests=[];self.counter=0
        real_client=httpx.AsyncClient
        def handler(request):
            self.requests.append(request)
            status,raw,headers=self.responses.pop(0)
            return httpx.Response(status,stream=raw,headers=headers) if isinstance(raw,httpx.AsyncByteStream) else httpx.Response(status,content=raw,headers=headers)
        self.client=patch.object(engine.httpx,'AsyncClient',side_effect=lambda **kwargs:
            real_client(transport=httpx.MockTransport(handler),follow_redirects=False,trust_env=False,timeout=5))
        self.client.start();self.addCleanup(self.client.stop)
        parser=patch.object(PageObject,'extract_text',side_effect=AssertionError('No PDF body extraction'))
        parser.start();self.addCleanup(parser.stop)
        runtime_model=patch.object(engine,'llm',new=AsyncMock(side_effect=AssertionError('No document model call')))
        runtime_model.start();self.addCleanup(runtime_model.stop)
        self.model=AsyncMock(side_effect=AssertionError('No document model call'))

    def run_row(self,rid):
        store.execute('INSERT INTO runs(id,project_id,started,status,origin) VALUES(?,?,?,?,?)',
                      (rid,self.project['id'],store.now(),'running','test'))

    async def download(self,raw,*,url=SOURCE,media='application/pdf',headers=None,rid=None):
        self.counter+=1;rid=rid or 'hash-run-'+str(self.counter);self.run_row(rid)
        selected={'content-type':media,**(headers or {})}
        self.responses.append((200,raw,selected))
        await engine.download_document(self.context,self.project,rid,url,[httpx.URL(url).host])
        store.execute('UPDATE runs SET status=?,finished=? WHERE id=?',('success',store.now(),rid))
        return store.one("SELECT * FROM snapshots WHERE run_id=? AND kind='document'",(rid,))

    def info(self,snapshot):
        return json.loads((store.DATA/'artifacts'/snapshot['run_id']/(snapshot['id']+'.extraction.json')).read_text())

    async def test_01_complete_original_saved_without_any_content_parser(self):
        raw=b'%PDF-1.7\nUnparseable public original; content bytes still monitored.\n'
        snap=await self.download(raw)
        self.assertEqual((store.DATA/snap['file']).read_bytes(),raw)
        self.assertEqual(snap['sha256'],store.digest(raw));self.assertEqual(snap['text'],'')
        self.assertEqual(snap['text_sha256'],store.digest(''));self.assertEqual(snap['links'],'[]')
        info=self.info(snap)
        self.assertEqual(info['method'],'hash_only');self.assertEqual(info['comparison_mode'],'sha256')
        self.assertFalse(info['content_analysis_performed']);self.assertTrue(info['original_sha256_verified'])
        self.assertEqual(info['comparison_status'],'baseline');self.assertIsNone(info['previous_snapshot_id'])
        self.assertNotIn('text_complete',info);self.assertNotIn('unreadable_pages',info)
        self.assertEqual(len(self.requests),1);self.assertEqual(self.responses,[])

    async def test_02_identical_fresh_bytes_ignore_header_changes(self):
        raw=b'%PDF-1.7\nunchanged'
        before=await self.download(raw,headers={'etag':'one','last-modified':'Thu, 05 Nov 2026 08:00:00 GMT'})
        after=await self.download(raw,headers={'etag':'two','last-modified':'Fri, 06 Nov 2026 09:00:00 GMT'})
        self.assertEqual(len(self.requests),2)
        self.assertEqual(before['file'],after['file'])
        self.assertEqual(store.rows('SELECT * FROM changes'),[])
        self.assertEqual(self.info(after)['comparison_status'],'unchanged')
        self.assertEqual(self.info(after)['previous_snapshot_id'],before['id'])
        self.assertEqual(self.info(after)['document_metadata']['http']['etag'],'two')
        self.assertEqual(self.info(after)['document_metadata']['http']['last_modified']['status'],'exact')

    async def test_03_same_url_different_bytes_is_relevant_without_model_gate(self):
        before=await self.download(b'%PDF-1.7\nversion 1')
        after=await self.download(b'%PDF-1.7\nversion 2')
        change=store.one('SELECT * FROM changes WHERE current_id=?',(after['id'],))
        self.assertEqual(change['kind'],'Datei geändert')
        detail=json.loads(change['evidence'])
        self.assertNotIn('text_diff',detail)
        self.assertEqual(detail['document_comparison']['previous_sha256'],before['sha256'])
        self.assertEqual(detail['document_comparison']['current_sha256'],after['sha256'])
        self.assertEqual(detail['ai_assessment']['method'],'deterministic')
        self.assertEqual(attention.priority(change['kind'],detail),'attention')
        result=await relevance_review.review_run(self.project,after['run_id'],self.model,'not-needed')
        self.assertEqual(result,{'reviewed':1,'count':1,'uncertain':0,'errors':[]})
        self.model.assert_not_awaited()
        self.assertEqual(attention.summary(self.project['id'])['attention_count'],1)

    async def test_04_legacy_ocr_text_policy_transition_is_not_a_source_change(self):
        raw=b'%PDF-1.7\nlegacy bytes'
        before=await self.download(raw)
        legacy='PRIVATE_LEGACY_DOCUMENT_TEXT'
        store.execute('UPDATE snapshots SET text=?,text_sha256=? WHERE id=?',(legacy,store.digest(legacy),before['id']))
        (store.DATA/'artifacts'/before['run_id']/(before['id']+'.extraction.json')).write_text(json.dumps({
            'method':'vision_ocr','text_complete':False,'extraction_policy':'old-policy','unreadable_pages':[40]}))
        after=await self.download(raw)
        self.assertEqual(after['text'],'');self.assertEqual(store.rows('SELECT * FROM changes'),[])
        self.assertEqual(store.one('SELECT text FROM snapshots WHERE id=?',(before['id'],))['text'],legacy)
        self.assertEqual(self.info(after)['comparison_status'],'unchanged')

    async def test_05_new_original_formats_never_require_decoding_or_passwords(self):
        cases=[('https://source.test/encrypted.pdf',b'%PDF-1.7\nEncrypted unable to parse','application/pdf','.pdf'),
               ('https://source.test/corrupt.docx',b'PK\x03\x04not-a-valid-office-package',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document','.docx'),
               ('https://source.test/attachment.unknown',b'\x00\xff\xfe\x80opaque bytes','application/octet-stream','.bin')]
        for url,raw,media,suffix in cases:
            with self.subTest(url=url):
                snap=await self.download(raw,url=url,media=media)
                self.assertTrue(snap['file'].endswith(suffix));self.assertEqual((store.DATA/snap['file']).read_bytes(),raw)

    async def test_06_html_login_or_error_page_is_not_archived_as_a_file(self):
        for media,raw in [('text/html',b'<html>Login required</html>'),
                          ('application/pdf',b'<!doctype html><html>Server error</html>')]:
            with self.subTest(media=media):
                with self.assertRaises(engine.HTMLDocumentURL):await self.download(raw,media=media)
        self.assertEqual(store.rows('SELECT * FROM snapshots'),[])

    async def test_07_foreign_redirect_and_sensitive_query_fail_before_second_download(self):
        for location in ('https://other.test/file.pdf','https://source.test/file.pdf?password=private'):
            self.counter+=1;rid='redirect-'+str(self.counter);self.run_row(rid)
            self.responses.append((302,b'',{'location':location}))
            with self.assertRaises(ValueError):
                await engine.download_document(self.context,self.project,rid,SOURCE,['source.test'])
        self.assertEqual(len(self.requests),2);self.assertEqual(store.rows('SELECT * FROM snapshots'),[])

    async def test_08_30_mib_limit_is_checked_for_header_and_actual_stream(self):
        with self.assertRaisesRegex(ValueError,'30 MB'):
            await self.download(b'small',headers={'content-length':str(30*1024**2+1)})
        with self.assertRaisesRegex(ValueError,'30 MB'):
            await self.download(b'x'*(30*1024**2+1))
        self.assertEqual(store.rows('SELECT * FROM snapshots'),[])

    async def test_09_http_failure_cannot_produce_a_successful_original(self):
        for code in (401,404,500):
            rid='http-'+str(code);self.run_row(rid);self.responses.append((code,b'error',{'content-type':'application/pdf'}))
            with self.assertRaises(httpx.HTTPStatusError):
                await engine.download_document(self.context,self.project,rid,SOURCE,['source.test'])
        self.assertEqual(store.rows('SELECT * FROM snapshots'),[])

    async def test_10_original_file_tamper_prevents_deterministic_relevance(self):
        before=await self.download(b'%PDF-1.7\noriginal')
        after=await self.download(b'%PDF-1.7\nnew original')
        change=store.one('SELECT * FROM changes WHERE current_id=?',(after['id'],))
        (store.DATA/before['file']).write_bytes(b'corrupt')
        self.assertIsNone(change_review.review_document_hash_change(change['id'],after['run_id']))
        result=await relevance_review.review_run(self.project,after['run_id'],self.model,'not-needed')
        self.assertEqual(result['reviewed'],0);self.assertEqual(result['uncertain'],1);self.assertTrue(result['errors'])
        self.model.assert_not_awaited()

    async def test_11_inert_hash_descriptor_never_exposes_legacy_text_or_diff(self):
        snap=await self.download(b'%PDF-1.7\nfile')
        canary='LEGACY_DOCUMENT_CONTENT_MUST_NOT_REACH_LLM'
        legacy={'task':'Monitor originals','observations':[{'url':SOURCE,'kind':'document','snapshot_id':snap['id'],
            'text':canary,'metadata':{'paragraphs':[canary],'extraction':{'method':'vision_ocr','text':canary}}}],
            'changes':[{'url':SOURCE,'kind':'Inhalt geändert','evidence':{'text_diff':canary,
                'ai_assessment':{'relevance':'relevant','reason':canary},'assessment_history':[{'text':canary}]}}],
            'agent':{'complete':True,'findings':canary,'checked':[canary]},
            'vision':[{'url':SOURCE,'interpretation':canary,'comparison_available':False}],
            'document_check':{'documents':[{'url':SOURCE,'status':'checked','changes':[{'text_diff_excerpt':canary}],
                'extraction':{'method':'vision_ocr','text':canary}}]}}
        original=copy.deepcopy(legacy);model_view=engine.summary_evidence(legacy)
        self.assertNotIn(canary,json.dumps(model_view));self.assertEqual(legacy,original)
        self.assertEqual(model_view['observations'][0]['text'],'')
        self.assertFalse(model_view['observations'][0]['content_analysis_performed'])
        store.execute('UPDATE snapshots SET text=?,text_sha256=? WHERE id=?',(canary,store.digest(canary),snap['id']))
        descriptor=engine.document_source_result(self.project['id'],snap['run_id'],SOURCE)
        self.assertNotIn(canary,json.dumps(descriptor));self.assertEqual(descriptor['text'],'')

    async def test_12_top_level_document_dom_never_reaches_screenshot(self):
        page=type('Page',(),{})();page.evaluate=AsyncMock(return_value={
            'content_type':'text/html','pdf':True,'viewer':False})
        page.screenshot=AsyncMock(side_effect=AssertionError('Document pixels must not be captured'))
        with self.assertRaisesRegex(RuntimeError,'Dokumentbilder'):
            await engine.safe_screenshot(page)
        with self.assertRaisesRegex(RuntimeError,'Dokumentbilder'):
            await engine.safe_content_screenshot(page,'body')
        page.screenshot.assert_not_awaited()
        self.assertTrue(await engine.browser_document_state(page,'application/pdf'))
        self.assertTrue(await engine.browser_document_state(page,'text/plain'))

    async def test_13_public_berlin_download_sends_no_browser_cookie_and_no_anonymous_recheck(self):
        url='https://www.berlin.de/sen/stadt/_assets/guideline.pdf'
        await self.download(b'%PDF-1.7\npublic bytes',url=url)
        self.assertNotIn('cookie',self.requests[0].headers)
        self.context.cookies.assert_not_awaited();self.assertEqual(len(self.requests),1)

    async def test_14_hash_relevance_marker_cannot_be_forged_by_model_label(self):
        before=await self.download(b'%PDF-1.7\nbefore')
        after=await self.download(b'%PDF-1.7\nafter')
        change=store.one('SELECT * FROM changes WHERE current_id=?',(after['id'],))
        detail=json.loads(change['evidence']);detail['document_comparison']['previous_sha256']='0'*64
        store.execute('UPDATE changes SET evidence=? WHERE id=?',(json.dumps(detail),change['id']))
        self.assertIsNone(change_review.review_document_hash_change(change['id']))
        result=await relevance_review.review_run(self.project,after['run_id'],self.model,'not-needed')
        self.assertTrue(result['errors']);self.model.assert_not_awaited()

    async def test_15_pdf_dates_are_ancillary_evidence_never_a_content_comparison(self):
        def pdf(date):
            writer=PdfWriter();writer.add_blank_page(width=50,height=50)
            writer.add_metadata({'/CreationDate':"D:20261101090000+01'00'",'/ModDate':date})
            output=io.BytesIO();writer.write(output);return output.getvalue()
        before=await self.download(pdf("D:20261105081011+01'00'"))
        after=await self.download(pdf("D:20261106121314+01'00'"))
        info=self.info(after)
        self.assertEqual(info['document_metadata']['pdf']['modification_date']['europe_berlin'],'2026-11-06T12:13:14+01:00')
        self.assertFalse(info['content_analysis_performed']);self.assertEqual(after['text'],'')
        change=store.one('SELECT * FROM changes WHERE current_id=?',(after['id'],))
        detail=json.loads(change['evidence']);dates=detail['document_metadata_comparison']
        self.assertEqual(dates['previous']['pdf']['modification_date']['europe_berlin'],'2026-11-05T08:10:11+01:00')
        self.assertEqual(dates['current'],info['document_metadata'])
        self.assertEqual(attention.priority(change['kind'],detail),'attention')
        self.assertIn('2026-11-06T12:13:14',json.dumps(engine.document_change_view(change)))
        self.assertEqual(engine.document_source_result(self.project['id'],after['run_id'],SOURCE)['document_metadata'],info['document_metadata'])
        self.assertEqual((store.DATA/before['file']).read_bytes(),pdf("D:20261105081011+01'00'"))

    async def test_16_metadata_parser_failure_does_not_fail_original_monitoring(self):
        with patch.object(engine,'extract_document_metadata',side_effect=RuntimeError('PRIVATE_BODY_MUST_NOT_LEAK')):
            snap=await self.download(b'%PDF-1.7\nmetadata parser unsupported')
        info=self.info(snap)
        self.assertEqual(snap['text'],'');self.assertTrue(info['original_sha256_verified'])
        self.assertTrue(info['document_metadata']['warnings'])
        self.assertNotIn('PRIVATE_BODY',json.dumps(info))

    async def test_17_empty_200_or_204_is_not_a_successful_original(self):
        with self.assertRaises(ValueError):
            await self.download(b'')
        rid='empty-204';self.run_row(rid)
        self.responses.append((204,b'',{'content-type':'application/pdf'}))
        with self.assertRaises(ValueError):
            await engine.download_document(self.context,self.project,rid,SOURCE,['source.test'])
        self.assertEqual(store.rows('SELECT * FROM snapshots'),[])
        self.assertEqual(store.rows('SELECT * FROM changes'),[])

    async def test_18_interrupted_http_stream_never_archives_partial_bytes(self):
        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'%PDF-1.7\nOnly an incomplete transport prefix'
                raise httpx.ReadError('Simulated upstream interruption')
        objects=store.DATA/'artifacts/objects'
        before=set(objects.iterdir()) if objects.exists() else set()
        rid='interrupted-stream';self.run_row(rid)
        self.responses.append((200,Interrupted(),{'content-type':'application/pdf'}))
        with self.assertRaises(httpx.ReadError):
            await engine.download_document(self.context,self.project,rid,SOURCE,['source.test'])
        self.assertEqual(store.rows('SELECT * FROM snapshots'),[])
        self.assertEqual(store.rows('SELECT * FROM changes'),[])
        self.assertEqual(set(objects.iterdir()) if objects.exists() else set(),before)


if __name__=='__main__':unittest.main(verbosity=2)
