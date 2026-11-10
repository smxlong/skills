# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0", "beautifulsoup4==4.14.3"]
# ///
"""Public Census directory-tree practice. JSON-line follow/export commands.

Start with --output a fresh local capture directory; the known public seed is
opened once. Commands {"follow":"OBSERVED_HREF"}, {"export":"OBSERVED_HREF"},
and {"quit":true} use only links discovered on the current page. Export captures
same-origin CSV bytes, preserves them, and validates every row offline. The export command
accepts an explicit encoding (default strict UTF-8 with optional BOM). No profile
or existing-tab content is inspected. All capture bytes stay in the output dir.
"""
import argparse
import base64
import csv
import json
from pathlib import Path
import sys
import time
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright
from browser import html_map, metadata, save_json, stamp, bounded
from bs4 import BeautifulSoup

SEED = 'https://www2.census.gov/programs-surveys/popest/datasets/'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:9223')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {'source':SEED, 'started_at':stamp(), 'pages':[], 'exports':[], 'failures':[]}
    last_request = 0
    links = []

    def emit(value):
        print(json.dumps(bounded(value), ensure_ascii=False), flush=True)

    def gate():
        nonlocal last_request
        wait = 8.1 - (time.monotonic()-last_request)
        if wait > 0:
            time.sleep(wait)
        last_request = time.monotonic()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.endpoint)
        context = browser.contexts[0]
        original = list(context.pages)
        page = context.new_page()
        page.set_default_timeout(15000)
        try:
            command = {'open':SEED}
            while True:
                try:
                    if command.get('quit'):
                        break
                    current = {link['href']:link for link in links}
                    if 'follow' in command or 'export' in command:
                        href = command.get('follow', command.get('export'))
                        if href not in current:
                            raise ValueError('Must choose an observed current-page link')
                        url = current[href]['url']
                        if urlparse(url).netloc != 'www2.census.gov':
                            raise ValueError('Probe restricted to the public Census data host')
                    else:
                        url = command['open']
                        if url != SEED:
                            raise ValueError('Only the seed may be opened directly')
                    if 'export' in command:
                        if not urlparse(url).path.lower().endswith('.csv'):
                            raise ValueError('This probe exports CSV files only')
                        gate()
                        # Modest binary transfer: browser-origin fetch avoids changing
                        # the borrowed default context download preferences.
                        with page.expect_response(lambda r:r.url==url and r.request.method=='GET') as pending:
                            data = page.evaluate('''async url=>{const r=await fetch(url,{credentials:'omit'});if(!r.ok)throw Error(`HTTP ${r.status}`);const b=new Uint8Array(await r.arrayBuffer());let binary='';for(let i=0;i<b.length;i+=32768)binary+=String.fromCharCode(...b.subarray(i,i+32768));return {status:r.status,type:r.headers.get('content-type'),response_bytes:b.byteLength,base64:btoa(binary)}}''', url)
                        raw = base64.b64decode(data.pop('base64'),validate=True)
                        assert len(raw)==data['response_bytes']
                        playwright_body=pending.value.body()
                        # For legacy text, this CDP endpoint may expose a UTF-8
                        # transcoded response.body(); preserve both to show it.
                        data['playwright_body_bytes']=len(playwright_body)
                        data['playwright_body_matches_arraybuffer']=playwright_body==raw
                        if b'<html' in raw[:1000].lower():
                            raise ValueError('Export was an HTML page')
                        name = Path(urlparse(url).path).name
                        out = args.output/name
                        with out.open('xb') as dst:
                            dst.write(raw)
                        raw_record=metadata(out,url=url,observed_at=stamp(),kind='fetch-arraybuffer-bytes',**data)
                        with Path(str(out)+'.playwright-body').open('xb') as alternate:
                            alternate.write(playwright_body)
                        save_json(str(out)+'.raw.meta.json',raw_record)
                        encoding=command.get('encoding','utf-8-sig')
                        # Strict decode is essential: fetch.text() silently replaces
                        # invalid UTF-8 bytes in legacy CSV exports.
                        decoded=raw.decode(encoding, errors='strict')
                        if encoding!='utf-8-sig':
                            assert decoded.encode(encoding)==raw, 'Encoding must round trip'
                        with out.open(encoding=encoding,errors='strict',newline='') as src:
                            reader=csv.DictReader(src)
                            fields=reader.fieldnames
                            count=0; malformed=0; missing=0; ids=set(); duplicates=0
                            population=0
                            for row in reader:
                                count+=1
                                malformed+=None in row
                                missing+=any(value is None for value in row.values())
                                key=tuple(row.get(k) for k in ('SUMLEV','STATE','COUNTY'))
                                duplicates+=key in ids; ids.add(key)
                                if row.get('SUMLEV')=='050' and 'POPESTIMATE2024' in row:
                                    population+=int(row['POPESTIMATE2024'])
                        result=metadata(out, url=url, observed_at=stamp(), rows=count, columns=len(fields), fields=fields,
                            malformed_rows=malformed, missing_cells=missing, duplicate_geography_keys=duplicates,
                            county_population_sum_2024=population, encoding=encoding, decoded_replacement_characters=decoded.count('\ufffd'), **data)
                        report['exports'].append(result)
                        save_json(str(out)+'.meta.json',result)
                        emit({'export':{k:v for k,v in result.items() if k not in ('path','fields')},'field_sample':fields[:12]})
                    else:
                        gate()
                        response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
                        if not response or not response.ok:
                            raise ValueError(f'HTTP {response.status if response else None}')
                        n=len(report['pages'])
                        out=args.output/f'page-{n:02d}.html'
                        html=page.content()
                        out.write_text(html,encoding='utf-8')
                        soup=BeautifulSoup(html,'html.parser')
                        links=[{'href':a['href'],'url':urljoin(page.url,a['href']),'text':a.get_text(' ',strip=True)} for a in soup.select('a[href]')]
                        save_json(args.output/f'page-{n:02d}.links.json',links)
                        record=metadata(out,url=page.url,status=response.status,observed_at=stamp(),links=len(links))
                        report['pages'].append(record)
                        save_json(str(out)+'.meta.json',record)
                        emit({'page':n,'url':page.url,'status':response.status,'html_bytes':record['bytes'],
                              'links':len(links),'sample':[{'href':l['href'][:180],'text':l['text'][:100]} for l in links[:24]],
                              'map':html_map(out,limit=4)})
                except Exception as exc:
                    failure={'type':type(exc).__name__,'message':str(exc)[:200],'observed_at':stamp()}
                    report['failures'].append(failure)
                    emit({'failure':failure})
                line=sys.stdin.readline()
                if not line:
                    break
                command=json.loads(line)
        finally:
            page.close()
            report['original_pages_still_open']=all(not p.is_closed() for p in original)
            report['elapsed_seconds']=round(time.monotonic()-started,3)
            report['finished_at']=stamp()
            save_json(args.output/'report.json',report)
            emit({'finished':True,'elapsed_seconds':report['elapsed_seconds'],'pages':len(report['pages']),
                  'exports':len(report['exports']),'original_pages_still_open':report['original_pages_still_open']})



def analyze_capture(root):
    """Offline row-level integrity and table-only link extraction."""
    root=Path(root)
    report=json.loads((root/'report.json').read_text())
    entries=[]
    per_page=[]
    for index, source in enumerate(report['pages']):
        soup=BeautifulSoup((root/f'page-{index:02d}.html').read_text(),'html.parser')
        rows=[]
        for tr in soup.select('table tr'):
            anchor=tr.select_one('td a[href]')
            if not anchor:
                continue
            href=anchor['href']
            if href.startswith(('/', '?', '#')) or '://' in href:
                continue
            record={'source':source['url'],'name':anchor.get_text(' ',strip=True),'href':href,
                    'url':urljoin(source['url'],href),'cells':[td.get_text(' ',strip=True) for td in tr.select('td')]}
            rows.append(record)
        entries.extend(rows)
        per_page.append({'url':source['url'],'dataset_entries':len(rows),'html_bytes':source['bytes'],'sha256':source['sha256']})
    with (root/'directory-entries.jsonl').open('x',encoding='utf8') as out:
        for row in entries:
            out.write(json.dumps(row,ensure_ascii=False)+'\n')
    export=report['exports'][0]
    path=root/Path(urlparse(export['url']).path).name
    states={};counties={};count=0
    with path.open(encoding=export['encoding'],errors='strict',newline='') as src, (root/'dataset.jsonl').open('x',encoding='utf8') as out:
        reader=csv.DictReader(src)
        for row in reader:
            out.write(json.dumps(row,ensure_ascii=False)+'\n')
            count+=1
            if row['SUMLEV']=='040':
                states[row['STATE']]=int(row['POPESTIMATE2024'])
            elif row['SUMLEV']=='050':
                counties[row['STATE']]=counties.get(row['STATE'],0)+int(row['POPESTIMATE2024'])
    reconciled=sum(states[key]==value for key,value in counties.items())
    assert reconciled==len(states)==51
    raw=path.read_bytes()
    alternate=Path(str(path)+'.playwright-body').read_bytes()
    result={'source':report['source'],'started_at':report['started_at'],'finished_at':report['finished_at'],
            'elapsed_seconds':report['elapsed_seconds'],'pages':per_page,'directory_entries':len(entries),
            'export':{k:v for k,v in export.items() if k!='path'},
            'csv_rows_reparsed':count,'state_rows':len(states),'county_rows':count-len(states),
            'states_reconciled_to_counties':reconciled,'state_population_sum_2024':sum(states.values()),
            'raw_high_byte_counts':{str(b):raw.count(b) for b in set(raw) if b>127},
            'playwright_body_high_byte_counts':{str(b):alternate.count(b) for b in set(alternate) if b>127},
            'scope':'All rows in the one selected CSV and entries in four selected directory pages; not a census of the full site.',
            'minimum_request_spacing_seconds':8.1,
            'raw_artifact_bytes':sum(p.stat().st_size for p in root.iterdir() if p.is_file())}
    save_json(root/'analysis.json',result)
    return result


def verify_fetch_helper(endpoint, output):
    """One known public export; local edge cases live in test_fetch_bytes.py."""
    from fetch_bytes import fetch_bytes
    root=Path(output)
    root.mkdir(parents=True,exist_ok=False)
    start=time.monotonic()
    url=SEED+'2020-2024/counties/totals/'
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(endpoint)
        page=browser.contexts[0].new_page()
        try:
            response=page.goto(url,wait_until='domcontentloaded')
            assert response.status==200
            export=page.get_by_role('link',name='co-est2024-alldata.csv',exact=True)
            assert export.count()==1
            target=urljoin(page.url,export.get_attribute('href'))
            time.sleep(8.1)
            path=root/'exact.csv'
            result=fetch_bytes(page,target,path)
            assert result['bytes']==1769182
            assert result['sha256']=='abcc8720d669e793bbfdcd440eeec37a78db3b452adbe4ccd1eadf7c72b522b9'
            raw=path.read_bytes()
            assert raw.decode('cp1252').encode('cp1252')==raw
            assert raw.count(b'\xf1')==1
            report={'public_export':{k:v for k,v in result.items() if k!='path'},
                    'elapsed_seconds':round(time.monotonic()-start,3),'public_requests':2,
                    'public_request_spacing_seconds':8.1}
            save_json(root/'verification.json',report)
            return report
        finally:
            page.close()

if __name__=='__main__':
    main()
