# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0", "beautifulsoup4==4.14.3"]
# ///
"""Observed SharePoint tree -> large Word HTML -> ordered versioned records."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from browser import metadata, soup_file, stamp


def extract_chapter(source, output):
    soup = soup_file(source)
    paragraphs = soup.select('#main p')
    rows = []
    for index, node in enumerate(paragraphs):
        text = node.get_text('', strip=False)
        bold = ' '.join(node.b.get_text(' ',strip=True).split()) if node.b else ''
        rows.append(dict(index=index, text=text, bold_prefix=bold))
    with (output/'paragraphs.jsonl').open('x',encoding='utf-8') as dst:
        for row in rows: dst.write(json.dumps(row,ensure_ascii=False)+'\n')
    boundaries=[]
    for row in rows:
        match = re.match(r'^(90\.\d{3})\b',row['bold_prefix'])
        if match and re.match(r'^\s*90\.\d{3}\b',row['text']):
            boundaries.append((row['index'],match.group(1)))
    assert boundaries, 'No observed section boundaries; inspect source layout'
    occurrences=Counter(); covered=[]
    with (output/'sections.jsonl').open('x',encoding='utf-8') as dst:
        # Preserve preamble/contents too, avoiding unaccounted source paragraphs.
        records=[dict(kind='preamble',start=0,end=boundaries[0][0],section=None)]
        records += [dict(kind='section',start=start,
            end=boundaries[i+1][0] if i+1<len(boundaries) else len(rows), section=number)
            for i,(start,number) in enumerate(boundaries)]
        for record in records:
            occurrences[record['section']] += 1
            selected = rows[record['start']:record['end']]
            covered.extend(r['index'] for r in selected)
            record.update(occurrence=occurrences[record['section']], paragraphs=selected)
            dst.write(json.dumps(record,ensure_ascii=False)+'\n')
    assert covered == list(range(len(rows))), 'Partition loses/repeats paragraphs'
    repeated = {k:v for k,v in occurrences.items() if k is not None and v>1}
    return dict(paragraphs=len(rows),section_occurrences=len(boundaries),
        distinct_section_numbers=len(occurrences)-1,repeated_section_numbers=repeated,
        preamble_paragraphs=boundaries[0][0],partition_verified=True,
        paragraph_artifact=metadata(output/'paragraphs.jsonl'),
        section_artifact=metadata(output/'sections.jsonl'))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    started=time.monotonic(); pages=[]
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp('http://127.0.0.1:9223')
        page=browser.contexts[0].new_page(); page.set_default_timeout(10000)
        def capture(name):
            path=args.output/(name+'.html'); path.write_text(page.content(),encoding='utf-8')
            info=metadata(path,url=page.url,observed_at=stamp(),kind='rendered-dom')
            (args.output/(name+'.meta.json')).write_text(json.dumps(info,indent=2),encoding='utf-8')
            soup=soup_file(path)
            pages.append(dict(name=name,url=page.url,html_bytes=info['bytes'],sha256=info['sha256'],
                headings=len(soup.select('h1,h2,h3')),links=len(soup.select('a[href]'))))
            print(json.dumps(dict(stage=name,bytes=info['bytes'],elapsed_s=round(time.monotonic()-started,3))),flush=True)
            return path
        try:
            response=page.goto('https://www.oregonlegislature.gov/bills_laws/Pages/ORS.aspx',wait_until='domcontentloaded')
            assert response and response.status==200
            page.locator('#mainbody table.ms-listviewtable').first.wait_for()
            capture('root')
            time.sleep(8)
            # Expansion can persist between visits. Check descendant visibility
            # rather than blindly toggling a branch that may already be open.
            volume=page.locator('tr').filter(has_text=re.compile(r'^\s*Volume\s*:\s*03\s*-')).locator('a')
            title=page.locator('tr').filter(has_text=re.compile(r'^\s*Title Number\s*:\s*10\.')).locator('a')
            if not title.is_visible(): volume.click()
            title.wait_for(state='visible')
            capture('volume')
            time.sleep(8)
            chapter=page.locator('#mainbody a').filter(has_text=re.compile(r'^Chapter 090$'))
            if not chapter.is_visible(): title.click()
            chapter.wait_for(state='visible')
            capture('title')
            assert chapter.count()==1
            observed_url=urljoin(page.url,chapter.get_attribute('href'))
            assert observed_url.startswith('https://www.oregonlegislature.gov/')
            time.sleep(8)
            response=page.goto(observed_url,wait_until='domcontentloaded')
            assert response and response.status==200
            source=capture('chapter')
            extraction=extract_chapter(source,args.output)
        finally: page.close()
    # Public-safe report uses source URLs and aggregate metrics, no local paths.
    for key in ('paragraph_artifact','section_artifact'):
        extraction[key].pop('path')
    result=dict(passed=True,site='Oregon Legislature',pages=pages,tree_edges=3,
        controls='Observed row label -> unique anchor; generated IDs avoided',
        extraction=extraction,elapsed_seconds=round(time.monotonic()-started,3),
        complete_scope='Every paragraph under #main in one observed chapter; no claim of legal completeness or currency',
        owned_tab_closed=True)
    (args.output/'statutes-results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
