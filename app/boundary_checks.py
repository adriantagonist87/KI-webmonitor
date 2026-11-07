"""Isolated LLM/document privacy boundary checks with synthetic credentials."""
import asyncio
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

scratch=tempfile.TemporaryDirectory(prefix='.boundary-check-',dir='/data')
os.environ['DATA_DIR']=scratch.name
import store
store.CONFIG=Path(scratch.name)/'settings.json'
store.SECRETS=Path(scratch.name)/'credentials.json'
import engine
import site_privacy
from fixture import pdf
import httpx
from pypdf import PageObject

NUMBER='987654321'
POSTCODE='54321'
SECRET={'abonummer':NUMBER,'postcode':POSTCODE}


class DownloadResponse:
    status_code=200
    def __init__(self,raw):
        self.raw=raw
        self.headers={'content-type':'application/pdf','content-length':str(len(raw))}
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    def raise_for_status(self):return None
    async def aiter_bytes(self):yield self.raw


class DownloadClient:
    def __init__(self,raw):self.response=DownloadResponse(raw)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    def stream(self,method,url,headers):
        if method!='GET':raise AssertionError('The document fetch must stay read-only.')
        self.response.url=httpx.URL(url)
        return self.response


class Context:
    pages=[]
    async def cookies(self,url):return []


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=scratch.name)
        self.data=Path(self.directory.name)
        store.DATA=engine.DATA=self.data
        store.CONFIG=self.data/'settings.json'
        store.SECRETS=self.data/'credentials.json'
        store.init()
        self.token=site_privacy.bind(SECRET)
    def tearDown(self):
        site_privacy.reset(self.token)
        self.directory.cleanup()
    def test_01_llm_sanitizes_text_and_tool_arguments_but_keeps_image_blob(self):
        # Synthetic URI: the digits deliberately occur in an opaque image-data
        # field. They must not be treated as visible text and corrupt the blob.
        image_blob='data:image/png;base64,987654321AAAB54321=='
        messages=[
            {'role':'system','content':'Kunde '+NUMBER+'; PLZ: '+POSTCODE+'; Leistung 54321 Watt.'},
            {'role':'assistant','content':'Werkzeugaufruf','tool_calls':[{
                'id':'fake-call','type':'function','function':{'name':'read_sources',
                    'arguments':json.dumps({'subscriber':NUMBER,'postcode':POSTCODE,'amount':54321})}}]},
            {'role':'user','content':[
                {'type':'text','text':'Abonummer '+NUMBER+'; Postleitzahl='+POSTCODE+'; 54321 Watt'},
                {'type':'image_url','image_url':{'url':image_blob,'detail':'auto'}}]}
        ]
        original=copy.deepcopy(messages)
        client=MagicMock()
        client.__aenter__=AsyncMock(return_value=client)
        client.__aexit__=AsyncMock(return_value=False)
        client.post=AsyncMock(return_value=httpx.Response(200,json={
            'choices':[{'finish_reason':'stop','message':{'content':'BOUNDARY_OK'}}]}))
        with patch('engine.httpx.AsyncClient',return_value=client):
            response=asyncio.run(engine.llm(messages,'local-test-model',temperature=0))
        self.assertEqual(response['choices'][0]['message']['content'],'BOUNDARY_OK')
        sent=client.post.call_args.kwargs['json']
        self.assertEqual(sent['messages'][2]['content'][1]['image_url']['url'],image_blob)
        self.assertEqual(sent['messages'][2]['content'][1]['image_url']['detail'],'auto')
        self.assertIn('54321 Watt',sent['messages'][0]['content'])
        self.assertIn('54321 Watt',sent['messages'][2]['content'][0]['text'])
        self.assertNotIn('PLZ: '+POSTCODE,sent['messages'][0]['content'])
        self.assertNotIn('Postleitzahl='+POSTCODE,sent['messages'][2]['content'][0]['text'])
        arguments=json.loads(sent['messages'][1]['tool_calls'][0]['function']['arguments'])
        self.assertNotEqual(arguments['subscriber'],NUMBER)
        self.assertNotEqual(arguments['postcode'],POSTCODE)
        self.assertEqual(arguments['amount'],54321)
        text_payload=copy.deepcopy(sent)
        text_payload['messages'][2]['content'][1]['image_url']['url']='[OPAQUE IMAGE BLOB]'
        self.assertNotIn(NUMBER,json.dumps(text_payload,ensure_ascii=False))
        self.assertEqual(messages,original)
    def test_02_download_withholds_all_body_text_and_archives_original_document_hash(self):
        project={'id':'boundary-project','url':'https://www.elektro.net/praxisprobleme/'}
        rid='boundary-run'
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (project['id'],'Boundary fixture',project['url'],'Synthetic privacy check',
                       'www.elektro.net','0 6 * * *',0,store.next_due('0 6 * * *'),store.now()))
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid,project['id'],store.now(),store.now(),'success','test'))
        source_text='Customer '+NUMBER+'; PLZ: '+POSTCODE+'; Leistung 54321 Watt.'
        raw=pdf(source_text)
        with patch('engine.httpx.AsyncClient',return_value=DownloadClient(raw)), \
             patch.object(PageObject,'extract_text',side_effect=AssertionError('No PDF body extraction')), \
             patch.object(engine,'llm',new=AsyncMock(side_effect=AssertionError('No document model call'))):
            returned=asyncio.run(engine.download_document(Context(),project,rid,
                        'https://www.elektro.net/synthetic-boundary.pdf',['www.elektro.net']))
        self.assertEqual(returned,'')
        snapshot=store.one('SELECT * FROM snapshots WHERE run_id=?',(rid,))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['text'],'')
        self.assertEqual(snapshot['text_sha256'],hashlib.sha256(b'').hexdigest())
        self.assertEqual(snapshot['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual((self.data/snapshot['file']).read_bytes(),raw)

    def test_03_public_berlin_asset_never_reads_browser_cookies_or_user_agent(self):
        project={'id':'public-berlin-project','url':'https://www.berlin.de/sen/stadt/service/rechtsvorschriften/bereich-bauen/bauaufsicht/gesetze/'}
        url='https://www.berlin.de/sen/stadt/_assets/service/rechtsvorschriften/bereich-bauen/synthetic-boundary.pdf?ts=1791887496'
        rid='public-berlin-run'
        store.execute('INSERT INTO projects(id,name,url,prompt,domains,cron,enabled,next_due,created) VALUES(?,?,?,?,?,?,?,?,?)',
                      (project['id'],'Public Berlin fixture',project['url'],'Synthetic anonymous transport check',
                       'www.berlin.de','0 6 * * *',0,store.next_due('0 6 * * *'),store.now()))
        store.execute('INSERT INTO runs(id,project_id,started,finished,status,origin) VALUES(?,?,?,?,?,?)',
                      (rid,project['id'],store.now(),store.now(),'success','test'))
        source_text='Public Berlin norm. Version 2026-01. Deadline 30 days.'
        raw=pdf(source_text)
        page=MagicMock()
        page.evaluate=AsyncMock(side_effect=AssertionError('Public assets must not read the browser user agent.'))
        ctx=Context()
        ctx.pages=[page]
        ctx.cookies=AsyncMock(side_effect=AssertionError('Public assets must not read browser cookies.'))
        client=DownloadClient(raw)
        client.stream=MagicMock(wraps=client.stream)
        with patch('engine.httpx.AsyncClient',return_value=client):
            returned=asyncio.run(engine.download_document(ctx,project,rid,url,['www.berlin.de']))
        ctx.cookies.assert_not_awaited()
        page.evaluate.assert_not_awaited()
        client.stream.assert_called_once()
        method,requested_url=client.stream.call_args.args
        self.assertEqual((method,requested_url),('GET',url))
        headers={key.lower():value for key,value in client.stream.call_args.kwargs['headers'].items()}
        self.assertEqual(headers,{'user-agent':'KI-Webmonitor/1.0','referer':project['url']})
        self.assertEqual(returned,'')
        snapshot=store.one('SELECT * FROM snapshots WHERE run_id=?',(rid,))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['url'],url)
        self.assertEqual(snapshot['kind'],'document')
        self.assertEqual(snapshot['text'],returned.strip())
        self.assertEqual(snapshot['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(snapshot['text_sha256'],hashlib.sha256(snapshot['text'].encode()).hexdigest())
        self.assertEqual((self.data/snapshot['file']).read_bytes(),raw)


if __name__=='__main__':unittest.main(verbosity=2)
