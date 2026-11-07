"""Disclosure checks in a fresh real-Brave context; synthetic HTTPS only."""
import os
import unittest
from playwright.async_api import async_playwright

from disclosure_actions import DisclosureError,DisclosureNotFound,open_disclosure,inspect_disclosure,list_disclosures,legacy_base_exclusions


ORIGIN='https://disclosures.test'


def aria_panel(outer,label,token):
    return ('<section id="'+outer+'"><h2>'+outer+'</h2><div class="accordion">'
            '<h3><button type="button" id="heading-'+token+'" aria-controls="panel-'+token+'" '
            'aria-expanded="false" onclick="togglePanel(this)">'+label+'</button></h3>'
            '<div id="panel-'+token+'" aria-labelledby="heading-'+token+'" aria-hidden="true" hidden>'
            '<p>Visible document '+outer+'</p><a href="/'+outer+'.pdf">Document '+outer+'</a></div></div></section>')


ARIA_SCRIPT='''<script>window.headerClicks=0;function togglePanel(button){
    window.headerClicks++;const open=button.getAttribute('aria-expanded')!=='true';
    for(const other of document.querySelectorAll('button[aria-controls]')){
        const panel=document.getElementById(other.getAttribute('aria-controls'));
        if(panel){other.setAttribute('aria-expanded','false');panel.setAttribute('aria-hidden','true');panel.hidden=true;}
    }
    const panel=document.getElementById(button.getAttribute('aria-controls'));
    button.setAttribute('aria-expanded',String(open));panel.setAttribute('aria-hidden',String(!open));panel.hidden=!open;
}</script>'''


def legacy_panel(label,token='broken',opened=False):
    return ('<section><div><h3><button id="repeated-control" aria-controls="missing-'+token+'" '
            'aria-expanded="'+str(opened).lower()+'" onclick="toggleLocal(this)">'+label+'<span></span></button></h3></div>'
            '<div'+('' if opened else ' style="display:none"')+'><section><ul><li>'
            '<a href="https://documents.test/'+token+'.pdf">Download '+label+'</a>'
            '</li></ul></section></div></section>')


LEGACY_SCRIPT='''<script>window.headerClicks=0;function toggleLocal(button){
    window.headerClicks++;const open=button.getAttribute('aria-expanded')!=='true';
    const panel=button.parentElement.parentElement.nextElementSibling;
    button.setAttribute('aria-expanded',String(open));panel.style.display=open?'block':'none';
}</script>'''


class DisclosureActionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.connect_over_cdp(
            os.environ.get('BROWSER_CDP_URL','http://browser:9223'))
        self.context=await self.browser.new_context(viewport={'width':1100,'height':800},service_workers='block')
        self.page=await self.context.new_page();self.html='';self.unexpected=[]
        async def serve(route):
            if route.request.method=='GET' and route.request.url==ORIGIN+'/':
                return await route.fulfill(content_type='text/html',body=self.html)
            if route.request.url==ORIGIN+'/favicon.ico':return await route.fulfill(status=204,body='')
            self.unexpected.append(route.request.url);return await route.abort()
        await self.context.route('**/*',serve)

    async def asyncTearDown(self):
        await self.context.close();await self.browser.close();await self.pw.stop()

    async def load(self,body):
        self.html='<!doctype html><html><head><meta charset="utf-8"><title>Native disclosure fixture</title></head><body>'+body+'</body></html>'
        await self.page.goto(ORIGIN+'/',wait_until='domcontentloaded')

    async def test_01_exclusive_native_sections_open_actual_target_only(self):
        await self.load('''<main id="documents"><details name="exclusive"><summary>FAQ (15)</summary><p>A</p></details>
            <details name="exclusive"><summary>Merkblätter (37)</summary><p>B</p></details></main>''')
        a=await open_disclosure(self.page,'FAQ')
        self.assertEqual(a['label'],'FAQ (15)');self.assertEqual(a['kind'],'disclosure')
        self.assertTrue(await self.page.locator(a['selector']).evaluate('e=>e.open'))
        b=await open_disclosure(self.page,'Merkblätter (37)')
        self.assertNotEqual(a['identity'],b['identity'])
        self.assertTrue(await self.page.locator(b['selector']).evaluate('e=>e.open'))
        self.assertFalse(await self.page.locator(a['selector']).evaluate('e=>e.open'))
        self.assertLessEqual(len(b['selector']),240)

    async def test_02_open_section_is_idempotent_and_not_clicked(self):
        await self.load('<main id="documents"><details open><summary>FAQ (15)</summary><p>A</p></details></main>')
        await self.page.evaluate("() => {window.clicks=0;document.querySelector('summary').addEventListener('click',()=>window.clicks++)}")
        result=await open_disclosure(self.page,'FAQ')
        again=await open_disclosure(self.page,'FAQ (15)')
        self.assertEqual(result,again)
        self.assertEqual(await self.page.evaluate('window.clicks'),0)
        self.assertTrue(await self.page.locator('details').evaluate('e=>e.open'))

    async def test_03_counter_change_keeps_semantic_identity_without_details_id(self):
        await self.load('<main id="documents"><details><summary><span>FAQ (15)</span></summary><p>A</p></details></main>')
        before=await open_disclosure(self.page,'FAQ')
        await self.page.locator('summary span').evaluate("e=>e.textContent='FAQ (16)'")
        after=await open_disclosure(self.page,'FAQ')
        self.assertEqual(after['label'],'FAQ (16)')
        self.assertEqual(before['identity'],after['identity'])
        self.assertNotIn('15',before['identity']);self.assertNotIn('16',after['identity'])
        self.assertEqual(before['selector'],after['selector'])

    async def test_04_form_or_nested_active_controls_are_blocked(self):
        bodies=[
            '<form><details><summary>FAQ (15)</summary><p>A</p></details></form>',
            '<details><summary><a href="/leave">FAQ (15)</a></summary><p>A</p></details>',
            '<details><summary>FAQ (15)<button type="button" hidden>Action</button></summary><p>A</p></details>',
        ]
        for body in bodies:
            with self.subTest(markup=body[:30]):
                await self.load(body)
                with self.assertRaises(ValueError) as raised:await open_disclosure(self.page,'FAQ')
                self.assertNotIsInstance(raised.exception,DisclosureNotFound)
                self.assertFalse(await self.page.locator('details').evaluate('e=>e.open'))
                self.assertEqual(self.page.url,ORIGIN+'/')
        self.assertEqual(self.unexpected,[])

    async def test_05_visible_ambiguous_labels_are_rejected(self):
        await self.load('<details><summary>FAQ (15)</summary>A</details><details><summary>FAQ (15)</summary>B</details>')
        for label in ('FAQ','FAQ (15)'):
            with self.subTest(label=label),self.assertRaises(ValueError) as raised:
                await open_disclosure(self.page,label)
            self.assertNotIsInstance(raised.exception,DisclosureNotFound)
        self.assertFalse(await self.page.locator('details').evaluate_all('es=>es.some(e=>e.open)'))

    async def test_06_real_unique_details_id_is_escaped_and_preferred(self):
        await self.load('<main id="documents"><details id="docs:FAQ 1"><summary>FAQ (15)</summary>A</details></main>')
        result=await open_disclosure(self.page,'FAQ')
        self.assertTrue(result['selector'].startswith('#'))
        self.assertEqual(await self.page.locator(result['selector']).get_attribute('id'),'docs:FAQ 1')
        self.assertIn('details-id',result['identity'])
        await self.page.locator('summary').evaluate("e=>e.textContent='FAQ (99)'")
        self.assertEqual(result['identity'],(await open_disclosure(self.page,'FAQ'))['identity'])

    async def test_07_duplicate_semantic_labels_use_distinct_exact_fallbacks(self):
        await self.load('<main id="documents"><details><summary>FAQ (15)</summary>A</details><details><summary>FAQ (37)</summary>B</details></main>')
        a=await open_disclosure(self.page,'FAQ (15)')
        b=await open_disclosure(self.page,'FAQ (37)')
        self.assertNotEqual(a['identity'],b['identity'])
        self.assertIn('exact-summary',a['identity']);self.assertIn('exact-summary',b['identity'])
        with self.assertRaises(ValueError):await open_disclosure(self.page,'FAQ')

    async def test_08_hidden_extra_is_excluded_and_non_native_can_fall_back(self):
        await self.load('''<main id="documents"><details style="display:none"><summary>FAQ (99)</summary>Hidden</details>
            <details><summary>FAQ (15)</summary>Visible</details></main>''')
        result=await open_disclosure(self.page,'FAQ')
        self.assertEqual(result['label'],'FAQ (15)')
        self.assertEqual(await self.page.locator(result['selector']).count(),1)
        self.assertNotIn('15',result['identity'])
        await self.load('<button type="button" aria-expanded="false">Veröffentlichungen</button>')
        with self.assertRaises(DisclosureNotFound):await open_disclosure(self.page,'Veröffentlichungen')


    async def test_09_exclusive_aria_siblings_are_separate_and_open_idempotently(self):
        await self.load('<main>'+aria_panel('block-a','FAQ','one')+aria_panel('block-b','Documents','two')+'</main>'+ARIA_SCRIPT)
        first=await open_disclosure(self.page,'FAQ')
        self.assertEqual(first['disclosure_type'],'aria');self.assertEqual(first['container_selector'],'#block-a')
        self.assertTrue((await inspect_disclosure(self.page,'FAQ'))['open'])
        self.assertEqual(first,await open_disclosure(self.page,'FAQ'))
        self.assertEqual(await self.page.evaluate('window.headerClicks'),1)
        second=await open_disclosure(self.page,'Documents')
        self.assertNotEqual(first['identity'],second['identity'])
        self.assertFalse((await inspect_disclosure(self.page,'FAQ'))['open'])
        self.assertTrue((await inspect_disclosure(self.page,'Documents'))['open'])
        self.assertEqual(await self.page.evaluate('window.headerClicks'),2)
        self.assertEqual(await self.page.locator(first['selector']).get_attribute('aria-hidden'),'true')

    async def test_10_dynamic_control_panel_ids_do_not_change_identity_or_reopen_on_inspection(self):
        await self.load('<main>'+aria_panel('stable-block','Documents','random-first')+'</main>'+ARIA_SCRIPT)
        first=await open_disclosure(self.page,'Documents')
        await self.load('<main>'+aria_panel('stable-block','Documents','random-second')+'</main>'+ARIA_SCRIPT)
        current=await inspect_disclosure(self.page,'Documents',first['container_selector'])
        self.assertFalse(current['open']);self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        self.assertEqual(first['identity'],current['identity'])
        self.assertNotEqual(first['selector'],current['selector'])
        self.assertNotIn('random-first',first['identity']);self.assertNotIn('random-second',current['identity'])
        reopened=await open_disclosure(self.page,'Documents',first['container_selector'])
        self.assertEqual(reopened['identity'],first['identity'])
        self.assertTrue((await inspect_disclosure(self.page,'Documents'))['open'])

    async def test_11_duplicate_aria_labels_require_observed_outer_context(self):
        await self.load('<main>'+aria_panel('block-a','Technical documents','one')+aria_panel('block-b','Technical documents','two')+'</main>'+ARIA_SCRIPT)
        with self.assertRaises(ValueError) as caught:await open_disclosure(self.page,'Technical documents')
        self.assertNotIsInstance(caught.exception,DisclosureNotFound)
        self.assertEqual({item['container_selector'] for item in caught.exception.details['contexts']},{'#block-a','#block-b'})
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        first=await open_disclosure(self.page,'Technical documents','#block-a')
        second=await open_disclosure(self.page,'Technical documents','#block-b')
        self.assertNotEqual(first['identity'],second['identity'])
        self.assertFalse((await inspect_disclosure(self.page,'Technical documents','#block-a'))['open'])
        self.assertTrue((await inspect_disclosure(self.page,'Technical documents','#block-b'))['open'])
        for selector in ('#missing','section','#heading-one','#panel-one'):
            with self.subTest(selector=selector),self.assertRaises(ValueError):
                await open_disclosure(self.page,'Technical documents',selector)

    async def test_12_unsafe_or_unconfirmed_aria_controls_never_click_or_fall_back(self):
        safe=aria_panel('block','Documents','one')
        bodies=[
            '<form>'+safe+'</form>',
            '<form id="external"></form>'+safe.replace('type="button"','type="submit" form="external"'),
            safe.replace('>Documents</button>','>Documents<a href="/leave" hidden>Action</a></button>'),
            safe.replace('type="button"','type="button" href="/leave"'),
            safe.replace('aria-labelledby="heading-one"','aria-labelledby="nonexistent"'),
            safe.replace('aria-controls="panel-one"','aria-controls="panel-one panel-other"'),
            safe.replace('<h3>','<div>').replace('</h3>','</div>'),
            safe.replace('id="panel-one"','role="dialog" id="panel-one"'),
            safe.replace('type="button"','type="button" aria-haspopup="dialog"'),
        ]
        for body in bodies:
            with self.subTest(markup=body[:65]):
                await self.load('<main>'+body+'</main>'+ARIA_SCRIPT)
                with self.assertRaises(ValueError) as caught:await open_disclosure(self.page,'Documents')
                self.assertNotIsInstance(caught.exception,DisclosureNotFound)
                self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
                self.assertEqual(self.page.url,ORIGIN+'/')
        self.assertEqual(self.unexpected,[])

    async def test_13_hidden_aria_duplicate_is_not_a_visible_choice(self):
        await self.load('<main>'+aria_panel('block-a','Documents','one')+'<div hidden>'+aria_panel('block-b','Documents','two')+'</div></main>'+ARIA_SCRIPT)
        result=await open_disclosure(self.page,'Documents')
        self.assertEqual(result['container_selector'],'#block-a')
        self.assertEqual(await self.page.locator('#heading-two').get_attribute('aria-expanded'),'false')

    async def test_14_soft_hyphens_and_zero_width_formatting_match_visible_native_and_aria_labels(self):
        await self.load('<main><details id="native"><summary>Installateur\u00ad-Rund\u200bschreiben</summary>Native text</details>'+
                        aria_panel('block','Unterlagen Niederspannung\u00ads\u200bnetz','one')+'</main>'+ARIA_SCRIPT)
        native=await open_disclosure(self.page,'Installateur-Rundschreiben')
        self.assertEqual(native['label'],'Installateur-Rundschreiben')
        aria=await open_disclosure(self.page,'Unterlagen Niederspannungsnetz')
        self.assertEqual(aria['label'],'Unterlagen Niederspannungsnetz')
        self.assertTrue((await inspect_disclosure(self.page,'Unterlagen Niederspannungsnetz'))['open'])

    async def test_15_reciprocal_heading_reference_and_formless_submit_button_are_safe(self):
        body=aria_panel('block','Documents','one').replace('<h3>','<h3 id="label-one">').replace('type="button"','type="submit"').replace('aria-labelledby="heading-one"','aria-labelledby="label-one"')
        await self.load('<main>'+body+'</main>'+ARIA_SCRIPT)
        result=await open_disclosure(self.page,'Documents')
        self.assertEqual(result['container_selector'],'#block')
        self.assertTrue((await inspect_disclosure(self.page,'Documents'))['open'])
        self.assertEqual(await self.page.evaluate('window.headerClicks'),1)

    async def test_16_duplicated_controlled_panel_ids_are_rejected_even_when_hidden(self):
        await self.load('<main>'+aria_panel('block','Documents','one')+'<div id="panel-one" hidden>Conflicting panel</div></main>'+ARIA_SCRIPT)
        with self.assertRaises(ValueError) as caught:await open_disclosure(self.page,'Documents')
        self.assertNotIsInstance(caught.exception,DisclosureNotFound)
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)


    async def test_17_only_empty_id_or_name_fragment_anchors_inside_headers_are_inert(self):
        safe=aria_panel('block','Documents','one')
        for anchor in ('<a id="fragment"></a>','<a name="fragment"></a>','<a id="fragment" name="old-fragment"></a>'):
            with self.subTest(anchor=anchor):
                await self.load('<main>'+safe.replace('Documents</button>','Documents'+anchor+'</button>')+'</main>'+ARIA_SCRIPT)
                result=await open_disclosure(self.page,'Documents')
                self.assertEqual(result['disclosure_type'],'aria')
                self.assertTrue((await inspect_disclosure(self.page,'Documents'))['open'])
                self.assertEqual(await self.page.evaluate('window.headerClicks'),1)
        for anchor,label in (
                ('<a id="fragment" href="#fragment"></a>','Documents'),
                ('<a id="fragment" onclick="window.unsafe=true"></a>','Documents'),
                ('<a name="fragment" tabindex="0"></a>','Documents'),
                ('<a name="fragment" role="link"></a>','Documents'),
                ('<a name="fragment">Text</a>','DocumentsText'),
                ('<a name="fragment"><span></span></a>','Documents'),
                ('<a></a>','Documents')):
            with self.subTest(anchor=anchor):
                await self.load('<main>'+safe.replace('Documents</button>','Documents'+anchor+'</button>')+'</main>'+ARIA_SCRIPT)
                with self.assertRaises(ValueError) as caught:await open_disclosure(self.page,label)
                self.assertNotIsInstance(caught.exception,DisclosureNotFound)
                self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        self.assertEqual(self.unexpected,[])

    async def test_18_legacy_document_panels_with_duplicate_broken_ids_are_local_and_idempotent(self):
        await self.load('<main id="documents">'+legacy_panel('Earthquake zones','same')+
                        legacy_panel('Withdrawn rules','same')+'</main>'+LEGACY_SCRIPT)
        exclusions=await legacy_base_exclusions(self.page)
        self.assertEqual(len(exclusions),4)
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        first=await open_disclosure(self.page,'Earthquake zones')
        self.assertEqual(first['disclosure_type'],'local-document')
        self.assertEqual(first,await open_disclosure(self.page,'Earthquake zones'))
        self.assertEqual(await self.page.evaluate('window.headerClicks'),1)
        self.assertFalse((await inspect_disclosure(self.page,'Withdrawn rules'))['open'])
        second=await open_disclosure(self.page,'Withdrawn rules')
        self.assertNotEqual(first['identity'],second['identity'])
        self.assertEqual(await self.page.evaluate('window.headerClicks'),2)
        self.assertNotIn('repeated-control',first['identity'])
        self.assertNotIn('missing-same',first['identity'])
        self.assertEqual(len(await legacy_base_exclusions(self.page)),4)
        self.assertEqual(self.unexpected,[])

    async def test_19_legacy_identity_ignores_generated_ids_and_open_state(self):
        await self.load('<main id="documents">'+legacy_panel('Documents (12)','one')+'</main>'+LEGACY_SCRIPT)
        first=await open_disclosure(self.page,'Documents')
        await self.load('<main id="documents">'+legacy_panel('Documents (13)','two',True)+'</main>'+LEGACY_SCRIPT)
        second=await open_disclosure(self.page,'Documents')
        self.assertEqual(first['identity'],second['identity'])
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        self.assertTrue((await inspect_disclosure(self.page,'Documents'))['open'])

    async def test_20_legacy_unsafe_or_ambiguous_structure_never_clicks(self):
        safe=legacy_panel('Documents')
        bodies=[
            '<form>'+safe+'</form>',
            safe.replace('<button ','<button type="submit" '),
            safe.replace('<button ','<button formaction="/delete" '),
            safe.replace('<button ','<button aria-haspopup="dialog" '),
            safe.replace('<span></span>','<span role="button"></span>'),
            safe.replace('<section><ul>','<section><form></form><ul>'),
            safe.replace('<section><ul>','<section><button>Delete</button><ul>'),
            safe.replace('https://documents.test/broken.pdf','https://documents.test/account'),
            safe.replace('<h3>','<div>').replace('</h3>','</div>'),
            safe.replace('<div style="display:none">','<div role="dialog" style="display:none">'),
            safe.replace('</h3></div><div','</h3></div><p>Intervening content</p><div'),
            safe.replace('<section><div><h3>','<main><div><h3>').replace('</div></section>','</div></main>'),
            safe.replace('>Documents<span>','>Delete account<span>'),
            safe.replace('aria-controls="missing-broken"','aria-controls="missing-broken missing-other"'),
            safe+'<div id="missing-broken" hidden>Existing nonreciprocal target</div>',
        ]
        for body in bodies:
            label='Delete account' if '>Delete account<span>' in body else 'Documents'
            with self.subTest(markup=body[:100]):
                await self.load('<article>'+body+'</article>'+LEGACY_SCRIPT)
                with self.assertRaises(ValueError) as caught:await open_disclosure(self.page,label)
                self.assertNotIsInstance(caught.exception,DisclosureNotFound)
                self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
                self.assertEqual(await legacy_base_exclusions(self.page),[])
        await self.load('<main>'+safe+safe+'</main>'+LEGACY_SCRIPT)
        with self.assertRaises(ValueError):await open_disclosure(self.page,'Documents')
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        self.assertEqual(self.unexpected,[])

    async def test_21_legacy_state_mismatch_and_nonopening_panel_are_never_successful(self):
        safe=legacy_panel('Documents')
        for body in (safe.replace('aria-expanded="false"','aria-expanded="true"'),
                     safe.replace(' style="display:none"','')):
            with self.subTest(markup=body[:110]):
                await self.load('<main>'+body+'</main>'+LEGACY_SCRIPT)
                with self.assertRaises(ValueError):await open_disclosure(self.page,'Documents')
                self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
                self.assertEqual(await legacy_base_exclusions(self.page),[])
        await self.load('<main>'+safe+'</main>'+LEGACY_SCRIPT.replace("panel.style.display=open?'block':'none';",''))
        with self.assertRaisesRegex(ValueError,'nicht sichtbar'):
            await open_disclosure(self.page,'Documents')
        self.assertEqual(await self.page.evaluate('window.headerClicks'),1)
        self.assertEqual(await legacy_base_exclusions(self.page),[])
        self.assertEqual(self.unexpected,[])

    async def test_22_listing_is_read_only_and_only_advertises_safe_visible_controls(self):
        await self.load('<main id="documents"><details id="native"><summary>FAQ</summary><p>Secret hidden panel</p></details>'+
                        aria_panel('block','Documents','one')+legacy_panel('Downloads')+
                        '<div hidden>'+aria_panel('hidden','Hidden documents','two')+'</div>'+
                        '<form><details><summary>Form details</summary>Not a reading control</details></form>'+
                        '<details><summary>Delete account</summary>Not a reading control</details>'+
                        aria_panel('admin','Administration','admin')+'</main>'+ARIA_SCRIPT+LEGACY_SCRIPT)
        before=await self.page.content()
        result=await list_disclosures(self.page)
        self.assertEqual((result['count'],result['returned'],result['truncated']),(3,3,False))
        self.assertEqual({item['label'] for item in result['items']},{'FAQ','Documents','Downloads'})
        self.assertEqual({item['disclosure_type'] for item in result['items']},{'native','aria','local-document'})
        self.assertTrue(all(item['kind']=='disclosure' and item['expanded'] is False for item in result['items']))
        self.assertNotIn('Secret hidden panel',str(result))
        self.assertEqual(await self.page.content(),before)
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)
        self.assertEqual(self.unexpected,[])
        for item in result['items']:
            confirmed=await inspect_disclosure(self.page,item['label'],item['container_selector'])
            self.assertEqual(confirmed['label'],item['label'])

    async def test_23_same_label_aria_siblings_share_parent_id_but_get_distinct_usable_contexts(self):
        body=aria_panel('block-a','Technical documents','one').replace(' id="block-a"','')
        body+=aria_panel('block-b','Technical documents','two').replace(' id="block-b"','')
        await self.load('<main id="documents">'+body+'</main>'+ARIA_SCRIPT)
        choices=await list_disclosures(self.page)
        self.assertEqual(choices['count'],2)
        contexts=[item['container_selector'] for item in choices['items']]
        self.assertEqual(len(set(contexts)),2)
        self.assertNotIn('#documents',contexts)
        self.assertEqual({item['heading'] for item in choices['items']},{'block-a','block-b'})
        with self.assertRaises(DisclosureError) as caught:await inspect_disclosure(self.page,'Technical documents')
        self.assertEqual({item['container_selector'] for item in caught.exception.details['contexts']},set(contexts))
        first=await open_disclosure(self.page,'Technical documents',contexts[0])
        second=await open_disclosure(self.page,'Technical documents',contexts[1])
        self.assertNotEqual(first['identity'],second['identity'])
        self.assertEqual(await self.page.evaluate('window.headerClicks'),2)

    async def test_24_identical_native_labels_have_separate_contexts_and_stable_identities(self):
        await self.load('<main id="documents"><details><summary>FAQ</summary>One</details>'+
                        '<details><summary>FAQ</summary>Two</details></main>')
        choices=await list_disclosures(self.page)
        self.assertEqual(choices['count'],2)
        first,second=choices['items']
        self.assertNotEqual(first['container_selector'],second['container_selector'])
        opened=await open_disclosure(self.page,first['label'],first['container_selector'])
        another=await open_disclosure(self.page,second['label'],second['container_selector'])
        self.assertNotEqual(opened['identity'],another['identity'])
        self.assertEqual(opened['identity'],(await inspect_disclosure(self.page,first['label'],first['container_selector']))['identity'])

    async def test_25_missing_heading_or_wrong_context_returns_safe_choices_without_clicking(self):
        await self.load('<main id="documents"><h2>10-kV-Mittelspannungsnetz</h2>'+
                        aria_panel('technical','Technische Unterlagen Mittelspannung','one')+'</main>'+ARIA_SCRIPT)
        for label,context in [('10-kV-Mittelspannungsnetz',None),('Technische Unterlagen Mittelspannung','#missing')]:
            with self.subTest(label=label),self.assertRaises(DisclosureError) as caught:
                await inspect_disclosure(self.page,label,context)
            self.assertEqual(caught.exception.details['context_count'],1)
            item=caught.exception.details['contexts'][0]
            self.assertEqual(item['label'],'Technische Unterlagen Mittelspannung')
            self.assertEqual(item['container_selector'],'#technical')
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)

    async def test_26_inventory_and_error_choices_have_explicit_limits(self):
        await self.load('<main id="documents">'+''.join(
            '<details id="section-'+str(i)+'"><summary>Documents '+str(i)+'</summary>Text</details>' for i in range(45))+'</main>')
        inventory=await list_disclosures(self.page)
        self.assertEqual((inventory['count'],inventory['returned'],inventory['truncated']),(45,40,True))
        subset=await list_disclosures(self.page,limit=3)
        self.assertEqual((subset['count'],subset['returned'],subset['truncated']),(45,3,True))
        with self.assertRaises(DisclosureNotFound) as caught:await inspect_disclosure(self.page,'Not an observed control')
        self.assertEqual(len(caught.exception.details['contexts']),8)
        self.assertEqual(caught.exception.details['context_count'],45)
        self.assertTrue(caught.exception.details['contexts_truncated'])
        for limit in (0,41,True,'8'):
            with self.subTest(limit=limit),self.assertRaises(ValueError):await list_disclosures(self.page,limit)

    async def test_27_inventory_contexts_do_not_depend_on_generated_aria_ids(self):
        await self.load('<main>'+aria_panel('stable','Documents','random-first')+'</main>'+ARIA_SCRIPT)
        first=await list_disclosures(self.page)
        await self.load('<main>'+aria_panel('stable','Documents','random-second')+'</main>'+ARIA_SCRIPT)
        second=await list_disclosures(self.page)
        self.assertEqual(first,second)
        self.assertNotIn('random-',str(first))
        self.assertEqual(await self.page.evaluate('window.headerClicks'),0)


if __name__=='__main__':unittest.main(verbosity=2)
