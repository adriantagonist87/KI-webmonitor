"""Generic, bounded reading of a dated, descending browser collection."""
from datetime import datetime
import re
import time
import site_safety
from browser_actions import scroll_region
from collection_index import normalize_records

_READ = r'''args => {
 const visible=e=>{const s=getComputedStyle(e);return e.getClientRects().length&&s.display!=='none'&&s.visibility!=='hidden'&&!e.closest('[hidden],[aria-hidden="true"]')};
 return [...document.querySelectorAll(args.items)].filter(visible).map(e=>{
  const title=e.querySelector(args.title), link=title?.closest('a[href]')||title?.querySelector('a[href]');
  const dates=[...e.querySelectorAll('dt')].filter(visible).filter(d=>d.innerText.replace(/[:\s]+$/,'').trim().toLocaleLowerCase('de')===args.date.toLocaleLowerCase('de'));
  return {url:link?.href||null,title:title?.innerText.trim()||'',date:dates.length===1?dates[0].nextElementSibling?.innerText.trim():null,text:e.innerText.trim()};
 });
}'''


async def scan_collection(page, item_selector, title_selector, date_label, container_selector, *, year=None, max_scrolls=80, timeout=120, clean_url=lambda u:u, scroll_fn=None):
    scroll = scroll_fn or scroll_region
    if site_safety.current():
        timeout += max_scrolls * site_safety.current().seconds
    for value in (item_selector,title_selector,date_label,container_selector):
        if not isinstance(value,str) or not value.strip() or len(value)>400:raise ValueError('Kurze bestätigte Selektoren und eine genaue Datumsbeschriftung sind erforderlich.')
    if year is None:year=datetime.now().year
    if type(year) is not int or not 1900<=year<=2200:raise ValueError('Ungültige Jahresgrenze.')
    start=time.monotonic();records={};stalled=0;boundary=None;reason='limit';complete=False
    # A new scan always starts at the current top; nested content is never read from an old offset.
    for _ in range(max_scrolls):
        if site_safety.current():
            await site_safety.check_page(page)
        await site_safety.wait()
        top=await scroll(page,'up',container_selector)
        if top['at_start']:break
    else:raise ValueError('Anfang der Sammlung konnte nicht erreicht werden.')
    for step in range(max_scrolls+1):
        if site_safety.current():
            await site_safety.check_page(page)
        if time.monotonic()-start>timeout:reason='time_limit';break
        batch=await page.evaluate(_READ,{'items':item_selector,'title':title_selector,'date':date_label})
        if not batch:raise ValueError('Keine sichtbaren Sammlungseinträge im bestätigten Inhaltsbereich.')
        if len(batch)>500:reason='item_limit';break
        dates=[];before_count=len(records)
        for item in batch:
            if not isinstance(item.get('date'),str) or not re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?',item['date']):raise ValueError('Ein Eintrag hat kein eindeutiges ISO-Datum im ausgewählten Datumsfeld.')
            dates.append(item['date'][:7])
            if int(item['date'][:4])<year:
                if boundary is None:boundary=item['date']
                continue
            if boundary is not None:raise ValueError('Datumsfolge ist nicht absteigend; Jahresgrenze nicht zuverlässig bestätigt.')
            item['url']=clean_url(item['url']) if item.get('url') else None
            normalized=normalize_records([item])[0]
            old=records.get(normalized['url'])
            if old is not None and old!=normalized:raise ValueError('Ein Sammlungseintrag änderte sich während des Lesens.')
            records[normalized['url']]=normalized
            if len(records)>500:raise ValueError('Sammlungsgrenze von 500 Einträgen erreicht.')
        if any(a<b for a,b in zip(dates,dates[1:])):raise ValueError('Die Sammlung ist im gewählten Datumsfeld nicht absteigend sortiert.')
        if boundary is not None:reason='year_boundary';complete=True;break
        if step>=max_scrolls:reason='scroll_limit';break
        await site_safety.wait()
        progress=await scroll(page,'down',container_selector)
        if not progress['progress'] and len(records)==before_count and progress['at_end']:
            stalled+=1
            if stalled>=2:reason='list_end';complete=True;break
            await page.wait_for_timeout(800)
        else:stalled=0
    return {'complete':complete,'reason':reason,'year_from':year,'date_label':date_label,
            'boundary_date':boundary,'scrolls':step,'records':normalize_records(list(records.values())),'observed_count':len(records)}
