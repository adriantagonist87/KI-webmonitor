"""Finite rotation coverage and real-browser privacy regression checks."""
import asyncio
import copy
import os
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from carousel_content import carousel_html


def state(visible=(0,), *, labels=None):
    labels=labels or ['Norm 2026-01','Norm 2026-02','Norm 2026-03']
    items=[{'order':0,'slot':'heading','identity':'heading','carousel':None,'slide':None,
            'html':'<section class="container"><h1>Neuerscheinungen</h1></section>',
            'content':'<h1>Neuerscheinungen</h1>'}]
    for i,label in enumerate(labels):
        html='<section class="container"><a class="item" href="/'+str(i)+'">'+label+'</a></section>'
        items.append({'order':i+1,'slot':'card-'+str(i),'identity':'id-'+str(i),
            'carousel':'carousel','slide':i,'html':html if i in visible else None,
            'content':'<a href="/'+str(i)+'">'+label+'</a>' if i in visible else None})
    return {'items':items,'carousels':[{'path':'carousel','slides':3}],'title':'Normen'}


class FakePage:
    def __init__(self,states):self.states=states;self.index=0;self.clock=0;self.waits=[]
    async def selection(self,page,selector):
        value=copy.deepcopy(self.states[min(self.index,len(self.states)-1)]);self.index+=1;return value
    async def wait_for_timeout(self,ms):self.waits.append(ms);self.clock+=ms/1000


class CarouselLogicChecks(unittest.TestCase):
    def test_15_preselected_union_keeps_all_main_wrappers_and_both_subject_sections(self):
        from page_content import editorial_content
        html='<html><body><main><h1>Brandschutz</h1></main><main><a href="/a">Richtlinie 10:2026</a></main><main><h1>Security</h1></main><main><a href="/b">Richtlinie 11:2026</a></main></body></html>'
        result=editorial_content(html,'https://sources.test/',preselected=True)
        self.assertEqual(result['links'],['https://sources.test/a','https://sources.test/b'])
        self.assertIn('Brandschutz',result['text'])
        self.assertIn('Security',result['text'])
        self.assertIn('Richtlinie 11:2026',result['text'])

    def test_14_rotating_union_uses_protected_viewport_not_moving_card_screenshots(self):
        import engine
        from unittest.mock import AsyncMock, patch
        page=object()
        with patch.object(engine,'safe_screenshot',new=AsyncMock(return_value=b'protected')) as viewport, \
                patch.object(engine,'safe_content_screenshot',new=AsyncMock(side_effect=AssertionError('Moving cards must not be photographed as a stable union'))) as strips:
            self.assertEqual(asyncio.run(engine.safe_capture_screenshot(page,'.cards a',carousel=True)),(b'protected','carousel-observed-v1'))
            viewport.assert_awaited_once_with(page)
            strips.assert_not_awaited()

    def run_capture(self,states,**kwargs):
        page=FakePage(states)
        with patch('carousel_content.visible_selection',side_effect=page.selection), \
                patch('carousel_content.monotonic',side_effect=lambda:page.clock):
            result=asyncio.run(carousel_html(page,'.item',max_ms=500,poll_ms=100,**kwargs))
        return result,page

    def test_01_accumulates_visible_slots_in_dom_order_regardless_of_start_slide(self):
        result,page=self.run_capture([state((2,)),state((0,)),state((1,))])
        self.assertTrue(result['selection_applied'])
        self.assertEqual(result['coverage']['expected_nodes'],4)
        self.assertEqual(result['coverage']['observed_nodes'],4)
        self.assertEqual([a.get_text() for a in BeautifulSoup(result['html'],'html.parser').select('a')],
                         ['Norm 2026-01','Norm 2026-02','Norm 2026-03'])
        self.assertEqual(len(page.waits),2)

    def test_02_frozen_rotation_fails_instead_of_archiving_subset(self):
        with self.assertRaisesRegex(RuntimeError,'nicht vollständig.*2/4'):
            self.run_capture([state()])

    def test_03_changed_node_identity_or_count_aborts(self):
        for variant in ('identity','count','order'):
            before,after=state(),state((1,))
            if variant=='identity':after['items'][1]['identity']='different'
            elif variant=='count':after['items'].pop()
            else:after['items'][1],after['items'][2]=after['items'][2],after['items'][1]
            with self.subTest(variant=variant), self.assertRaises(RuntimeError):
                self.run_capture([before,after])

    def test_04_changed_visible_text_during_scan_aborts(self):
        with self.assertRaisesRegex(RuntimeError,'hat sich während'):
            self.run_capture([state((0,)),state((0,),labels=['Norm 2026-09','Norm 2026-02','Norm 2026-03'])])

    def test_05_selector_must_cover_all_observed_slides(self):
        data=state();data['items']=data['items'][:2]
        with self.assertRaisesRegex(RuntimeError,'umfasst nicht alle'):
            self.run_capture([data])

    def test_06_no_carousel_uses_ordinary_full_visible_reader_without_wait(self):
        page=FakePage([{'items':[],'carousels':[]}])
        async def ordinary(page):return '<main>Ordinary</main>'
        with patch('carousel_content.visible_selection',side_effect=page.selection), \
                patch('carousel_content.visible_html',side_effect=ordinary):
            for selector in (None,'.normal'):
                result=asyncio.run(carousel_html(page,selector))
                self.assertFalse(result['selection_applied']);self.assertEqual(result['html'],'<main>Ordinary</main>')
        self.assertEqual(page.waits,[])

    def test_07_unobserved_html_never_enters_output_and_title_is_escaped(self):
        states=[state((0,)),state((1,)),state((2,))]
        for item in states:item['title']='<script>not markup</script>'
        result,_=self.run_capture(states)
        self.assertIn('&lt;script&gt;',result['html']);self.assertNotIn('<script>',result['html'])

    def test_08_size_limits_and_invalid_bounds_abort(self):
        with patch('carousel_content.MAX_CHARACTERS',10), self.assertRaisesRegex(RuntimeError,'Lesegrenze'):
            self.run_capture([state()])
        for kwargs in ({'max_ms':0},{'max_ms':True},{'poll_ms':0},{'poll_ms':1001}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                asyncio.run(carousel_html(None,None,**kwargs))


class CarouselBrowserChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(os.environ.get('BROWSER_CDP_URL','http://browser:9223'))
        self.context=await self.browser.new_context(viewport={'width':1000,'height':700},service_workers='block')
        self.page=await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close();await self.browser.close();await self.pw.stop()

    async def load(self, *, rotate=True, extra='', second=False):
        def carousel(prefix):
            slides=[]
            for i in range(3):
                slides.append('<div class="carousel-item'+(' active' if i==0 else '')+'">'
                    '<a class="item" href="/'+prefix+str(i)+'"><h5>'+prefix+str(i)+'</h5>'
                    '<input value="PRIVATE_INPUT"><span hidden>PRIVATE_HIDDEN</span>'
                    '<span style="visibility:hidden"><b style="visibility:visible">PRIVATE_ANCESTOR</b></span>'
                    '</a></div>')
            return '<div class="carousel"><div class="carousel-inner">'+''.join(slides)+'</div></div>'
        body='<section class="container"><h1>Neu</h1>'+carousel('A')+(carousel('B') if second else '')+extra+'</section>'
        await self.page.set_content('<html><head><title>Test</title><style>.carousel-item{display:none}.carousel-item.active{display:block}</style></head><body>'+body+'</body></html>')
        if rotate:
            await self.page.evaluate('''() => {window.fixtureTimer=setInterval(()=>{
                for(const inner of document.querySelectorAll('.carousel-inner')){
                    const slides=Array.from(inner.children);let i=slides.findIndex(e=>e.classList.contains('active'));
                    slides[i].classList.remove('active');slides[(i+1)%slides.length].classList.add('active');
                }},160)}''')

    async def test_09_actual_rotations_capture_all_cards_and_preserve_selector_context(self):
        await self.load(second=True)
        result=await carousel_html(self.page,'section.container h1, section.container a.item',max_ms=2500,poll_ms=40)
        soup=BeautifulSoup(result['html'],'html.parser')
        self.assertEqual([a.h5.get_text() for a in soup.select('section.container a.item')],['A0','A1','A2','B0','B1','B2'])
        self.assertEqual(result['coverage']['carousels'],2)
        self.assertEqual(result['coverage']['expected_nodes'],7)
        self.assertNotIn('PRIVATE_',result['html'])

    async def test_10_frozen_browser_carousel_is_not_a_complete_source(self):
        await self.load(rotate=False)
        with self.assertRaisesRegex(RuntimeError,'nicht vollständig'):
            await carousel_html(self.page,'.item',max_ms=300,poll_ms=50)

    async def test_11_hidden_private_noncarousel_and_closed_details_never_copied(self):
        extra='<div hidden><a class="item">PRIVATE_ACCOUNT</a></div><details><summary>FAQ</summary><a class="item">PRIVATE_CLOSED</a></details><form><a class="item">PRIVATE_FORM</a></form>'
        await self.load(extra=extra)
        result=await carousel_html(self.page,'.item',max_ms=2500,poll_ms=40)
        self.assertEqual(result['coverage']['expected_nodes'],3)
        self.assertNotIn('PRIVATE_',result['html'])

    async def test_12_reader_never_mutates_dom_or_constructs_custom_elements(self):
        await self.load(rotate=False)
        await self.page.evaluate('''() => {window.constructed=0;customElements.define('x-test',class extends HTMLElement{constructor(){super();window.constructed++}});
            window.mutations=0;window.observer=new MutationObserver(rs=>window.mutations+=rs.length);
            window.observer.observe(document.documentElement,{attributes:true,childList:true,subtree:true,characterData:true});}''')
        before=await self.page.content()
        with self.assertRaises(RuntimeError):await carousel_html(self.page,'.item',max_ms=150,poll_ms=30)
        self.assertEqual(await self.page.content(),before)
        self.assertEqual(await self.page.evaluate('window.mutations'),0)
        self.assertEqual(await self.page.evaluate('window.constructed'),0)

    async def test_13_active_only_selector_is_rejected_not_silently_complete(self):
        await self.load()
        with self.assertRaisesRegex(RuntimeError,'umfasst nicht alle'):
            await carousel_html(self.page,'.active .item',max_ms=300,poll_ms=50)


if __name__=='__main__':unittest.main(verbosity=2)
