# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0", "beautifulsoup4==4.14.3"]
# ///
"""Paced read-only GovInfo tree probe and independent HTML/XML extraction QA.

Use a fresh --output directory. Captures public source material only. Saves page
and selected JSON/XML bytes to disk; stdout is counts and selected source URLs.
"""
import argparse
import base64
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from playwright.sync_api import sync_playwright, expect
from browser import bounded, metadata, save_json, soup_file, stamp


def write_jsonl(path,records):
    with path.open('w',encoding='utf-8') as out:
        for record in records:out.write(json.dumps(record,ensure_ascii=False)+'\n')
    return metadata(path)


def analyze(directory):
    html_path=directory/'rendition.html'
    soup=soup_file(html_path)
    text=soup.body.get_text('\n',strip=False)
    blocks=soup.select('r05')
    pattern=re.compile(r'^Sec\.\s+(\d+[\w.\-]*)[ \t]+([^\n]+)',re.M)
    matches=list(pattern.finditer(text))
    flat=[{'id':m[1],'heading':m[2].strip(),'text':text[m.start():matches[i+1].start() if i+1<len(matches) else len(text)].strip()} for i,m in enumerate(matches)]
    direct=[]
    for node in blocks:
        own=''.join(str(child) for child in node.contents if isinstance(child,str)).strip()
        match=pattern.match(own)
        if match:direct.append({'id':match[1],'heading':match[2].strip(),'text':own})
    xml=ET.parse(directory/'rendition.xml')
    xml_records=[]
    for section in xml.findall('.//SECTION'):
        sectno=section.findtext('SECTNO','').strip()
        xml_records.append({'id':sectno.replace('§','').strip(),'heading':section.findtext('SUBJECT'),'text':' '.join(section.itertext())})
    flat_ids={r['id'] for r in flat};direct_ids={r['id'] for r in direct};xml_ids={r['id'] for r in xml_records}
    report={'html':metadata(html_path),'xml':metadata(directory/'rendition.xml'),
        'nested_r05_tags':len(blocks),'r05_with_r05_descendant':sum(bool(n.find('r05')) for n in blocks),
        'naive_descendant_extraction_chars':sum(len(n.get_text()) for n in blocks),'source_text_chars':len(text),
        'flat_regex':{'records':len(flat),'unique_ids':len(flat_ids),'duplicates':len(flat)-len(flat_ids),'artifact':write_jsonl(directory/'flat-sections.jsonl',flat)},
        'node_local':{'records':len(direct),'unique_ids':len(direct_ids),'artifact':write_jsonl(directory/'node-local-sections.jsonl',direct)},
        'xml_sections':{'root_tag':xml.getroot().tag,'records':len(xml_records),'unique_ids':len(xml_ids),'artifact':write_jsonl(directory/'xml-sections.jsonl',xml_records)},
        'node_local_ids_absent_from_xml':sorted(direct_ids-xml_ids),
        'xml_ids_absent_from_node_local':sorted(xml_ids-direct_ids),
        'flat_ids_absent_from_xml':sorted(flat_ids-xml_ids),
        'coverage':'one public 2025 Title 1 volume; not all CFR years/titles'}
    (directory/'analysis.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--analyze-only',action='store_true')
    args=parser.parse_args();out=args.output
    if args.analyze_only:
        print(json.dumps(bounded(analyze(out))));return
    out.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();last_action=0;network=[];events=[];owned=[]
    def gate():
        nonlocal last_action
        gap=time.monotonic()-last_action
        if gap<8:time.sleep(8-gap)
        last_action=time.monotonic()
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp('http://127.0.0.1:9223')
        page=browser.contexts[0].new_page()
        try:
            owned.append(page)
            cdp=page.context.new_cdp_session(page)
            try:target_id=cdp.send('Target.getTargetInfo')['targetInfo']['targetId']
            finally:cdp.detach()
            save_json(out/'ownership.json',{'target_id':target_id,'created_at':stamp()})
            page.on('popup',lambda popup:owned.append(popup))
            page.set_default_timeout(15000)
        except BaseException:
            page.close()
            raise
        def response_seen(r):
            if urlsplit(r.url).hostname!='www.govinfo.gov':return
            seq=len(network)+1 # Freeze identity BEFORE r.body(), which can dispatch more events.
            item={'id':seq,'url':r.url,'status':r.status,'method':r.request.method,'type':r.request.resource_type,'content_type':r.headers.get('content-type',''),'at':stamp()}
            network.append(item)
            if 'json' in item['content_type'] and item['type'] in ('xhr','fetch'):
                try:
                    body=r.body()
                    if len(body)<=8_000_000:
                        path=out/f'response-{seq:04}.json'
                        with path.open('xb') as dst:dst.write(body)
                        item['artifact']=metadata(path)
                except Exception as exc:item['error']=type(exc).__name__
        page.on('response',response_seen)
        def capture(name):
            path=out/(name+'.html');path.write_text(page.content(),encoding='utf-8')
            event={'name':name,'url':page.url,'at':stamp(),'elapsed_s':round(time.monotonic()-started,3),'artifact':metadata(path)}
            events.append(event);print(json.dumps(bounded(event)),flush=True)
        def goto(url):
            assert urlsplit(url).hostname=='www.govinfo.gov'
            gate();response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
            if response and response.status in (401,403,429):raise RuntimeError('Access denied or rate limited; stop')
            return response
        try:
            goto('https://www.govinfo.gov/browse-a-z')
            expect(page.locator('.views-field a[href="/app/collection/cfr"]')).to_have_count(1)
            capture('01-directory')
            gate();page.locator('.views-field a[href="/app/collection/cfr"]').click()
            expect(page.locator('button.accordion-button').filter(has_text=re.compile(r'^2025$'))).to_be_visible()
            capture('02-collection')
            year=page.locator('button.accordion-button').filter(has_text=re.compile(r'^2025$'))
            gate();year.click();expect(year).to_have_attribute('aria-expanded','true')
            title=page.locator('button.browse-titles').filter(has_text='Title 1 - General Provisions')
            expect(title).to_have_count(1);capture('03-year')
            gate();title.click();expect(title).to_have_attribute('aria-expanded','true')
            expect(page.locator('a[href*="CFR-2025-title1-vol1-toc-id2"]')).not_to_have_count(0)
            capture('04-title')
            html_link=page.locator('a[href="/content/pkg/CFR-2025-title1-vol1/html/CFR-2025-title1-vol1.htm"]')
            xml_link=page.locator('a[href="/content/pkg/CFR-2025-title1-vol1/xml/CFR-2025-title1-vol1.xml"]')
            expect(html_link).to_have_count(1);expect(xml_link).to_have_count(1)
            html_url=urljoin(page.url,html_link.get_attribute('href'));xml_url=urljoin(page.url,xml_link.get_attribute('href'))
            # The observed links open popups; navigate our owned tab to the observed URLs.
            html_response=goto(html_url)
            if not html_response or not html_response.ok:raise RuntimeError('HTML rendition unavailable')
            (out/'rendition-original.html').write_bytes(html_response.body())
            capture('rendition')
            # Navigating an XML+XSLT document yielded transformed XHTML through
            # response.body() in the real probe. Fetch the observed URL as bytes.
            gate()
            xml_result=page.evaluate("""async url=>{const r=await fetch(url);if(!r.ok)throw Error(r.status);const bytes=new Uint8Array(await r.arrayBuffer());let s='';for(let i=0;i<bytes.length;i+=32768)s+=String.fromCharCode(...bytes.slice(i,i+32768));return {status:r.status,type:r.headers.get('content-type'),base64:btoa(s)}}""",xml_url)
            (out/'rendition.xml').write_bytes(base64.b64decode(xml_result.pop('base64')))
            save_json(out/'xml-fetch.json',dict(xml_result,url=xml_url,at=stamp()))
        finally:
            save_json(out/'network.json',network);save_json(out/'events.json',events)
            for tab in reversed(owned):
                if not tab.is_closed():tab.close()
        analysis=analyze(out)
        report={'started_at':events[0]['at'],'finished_at':stamp(),'elapsed_s':round(time.monotonic()-started,3),'minimum_public_action_interval_s':8,'tree_transitions':3,'owned_tabs_closed':len(owned),'selected_json_responses':len([r for r in network if r.get('artifact')]),'analysis':analysis}
        save_json(out/'results.json',report)
        print(json.dumps(bounded(report)),flush=True)


if __name__=='__main__':main()
