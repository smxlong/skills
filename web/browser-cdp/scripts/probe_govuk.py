# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0", "beautifulsoup4==4.14.3"]
# ///
"""Paced, disk-first GOV.UK navigation tree probe. Uses only public pages."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit
from playwright.sync_api import sync_playwright
from browser import bounded, html_map, metadata, soup_file, stamp


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--endpoint',default='http://127.0.0.1:9223')
    p.add_argument('--inspect-only',action='store_true')
    p.add_argument('--print-from',type=Path,help='Existing captured guide links JSONL; inspect observed print link only')
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    started=time.monotonic()
    pages=[]
    last_navigation=0.0
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(args.endpoint)
        context=browser.contexts[0]
        originals=list(context.pages)
        page=context.new_page()
        try:
            def capture(url,index):
                nonlocal last_navigation
                wait=max(0,8-(time.monotonic()-last_navigation))
                if wait:time.sleep(wait)
                before=time.monotonic()
                response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
                last_navigation=time.monotonic()
                page.locator('main').wait_for(state='attached')
                path=args.output/f'{index:02d}.html'
                path.write_text(page.content(),encoding='utf-8')
                info=metadata(path,url=page.url,observed_at=stamp(),status=response.status if response else None)
                Path(str(path)+'.meta.json').write_text(json.dumps(info,indent=2)+'\n')
                soup=soup_file(path)
                main=soup.select_one('main')
                links=[{'text':a.get_text(' ',strip=True),'url':urljoin(page.url,a['href'])} for a in main.select('a[href]')]
                with (args.output/f'{index:02d}-links.jsonl').open('x') as out:
                    for link in links:out.write(json.dumps(link)+'\n')
                page_info={'source_url':page.url,'status':info['status'],'html_bytes':info['bytes'],'html_sha256':info['sha256'],
                    'main_links':len(links),'all_links':len(soup.select('a[href]')),'headings':[h.get_text(' ',strip=True)[:140] for h in main.select('h1,h2,h3')[:12]],
                    'elapsed_s':round(time.monotonic()-before,3),'capture':path.name,'main_selector':'main'}
                pages.append(page_info)
                print(json.dumps(bounded({'page':index,**page_info,'link_sample':links[:24]})),flush=True)
                if response and response.status>=400:raise RuntimeError(f'HTTP {response.status}')
                return soup,main,links
            if args.print_from:
                links=[json.loads(line) for line in args.print_from.open()]
                observed=[link for link in links if link['text']=='View a printable version of the whole guide']
                if len(observed)!=1:raise RuntimeError('Expected unique observed whole-guide print link')
                soup,main,links=capture(observed[0]['url'],0)
                candidates=['main .gem-c-govspeak','main .govuk-govspeak','main article','main section','main']
                structure=[{'selector':sel,'matches':len(soup.select(sel)),'paragraphs':sum(len(n.select('p')) for n in soup.select(sel)),'tables':sum(len(n.select('table')) for n in soup.select(sel))} for sel in candidates]
                records=[]
                for i,body in enumerate(soup.select('main .gem-c-govspeak')):
                    for node in body.select('h1,h2,h3,p,ul,ol,table'):
                        if node.find_parent(['ul','ol','table']) is not None:continue
                        record={'body_index':i,'tag':node.name,'text':node.get_text(' ',strip=True),'source_url':pages[0]['source_url']}
                        if node.name=='table':record['rows']=[[c.get_text(' ',strip=True) for c in row.select('th,td')] for row in node.select('tr')]
                        records.append(record)
                with (args.output/'print-content.jsonl').open('x') as out:
                    for record in records:out.write(json.dumps(record,ensure_ascii=False)+'\n')
                result={'passed':bool(records),'site':'GOV.UK','mode':'observed whole-guide print link','observed_at':stamp(),'pages':pages,'selector_candidates':structure,'content_records':len(records),'bodies':len(soup.select('main .gem-c-govspeak')),'table_count':sum(r['tag']=='table' for r in records),'table_rows':sum(len(r.get('rows',[])) for r in records),'elapsed_seconds':round(time.monotonic()-started,3),'scope':'All observed print-guide govspeak bodies, without linked secondary guidance','owned_tab_closed':True}
                reference_html=args.print_from.with_name(args.print_from.name.replace('-links.jsonl','.html'))
                reference=soup_file(reference_html)
                contents=reference.select_one('main .gem-c-contents-list')
                expected=[li.get_text(' ',strip=True) for li in contents.select('li')] if contents else []
                sections=[section.find(['h1','h2']).get_text(' ',strip=True) for section in main.select('section')]
                def table_rows(document):
                    return [[cell.get_text(' ',strip=True) for cell in row.select('th,td')] for row in document.select('main .gem-c-govspeak table tr')]
                result.update({'observed_contents_sections':len(expected),'print_sections':sections,
                    'leaf_table_matches_print':table_rows(reference)==table_rows(soup),
                    'verified_print_section_coverage':bool(expected) and len(expected)==len(sections) and all(title in heading for title,heading in zip(expected,sections))})
                (args.output/'print-results.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(bounded(result)),flush=True)
                return
            soup,main,links=capture('https://www.gov.uk/browse/benefits',0)
            if args.inspect_only:return
            targets=['/browse/benefits/families','/child-benefit','/child-benefit/what-youll-get']
            for i,target in enumerate(targets,1):
                matched=[link for link in links if urlsplit(link['url']).path.rstrip('/')==target]
                if not matched:raise RuntimeError(f'Observed main links do not contain {target}')
                soup,main,links=capture(matched[0]['url'],i)
            candidates=['main .gem-c-govspeak','main article','main .govuk-grid-column-two-thirds','main']
            selection=[{'selector':s,'matches':len(soup.select(s)),'paragraphs':sum(len(n.select('p')) for n in soup.select(s)),'tables':sum(len(n.select('table')) for n in soup.select(s))} for s in candidates]
            content=soup.select_one('main .gem-c-govspeak')
            selector='main .gem-c-govspeak'
            if content is None:raise RuntimeError('No observed canonical govspeak body; inspect capture')
            records=[]
            # Preserve ordered semantic blocks, including all table cells and list items.
            for node in content.select('h2,h3,p,ul,ol,table'):
                if node.find_parent(['ul','ol','table']) is not None:continue
                record={'tag':node.name,'text':node.get_text(' ',strip=True),'source_url':pages[-1]['source_url']}
                if node.name=='table':record['rows']=[[cell.get_text(' ',strip=True) for cell in row.select('th,td')] for row in node.select('tr')]
                records.append(record)
            output=args.output/'content-blocks.jsonl'
            with output.open('x') as out:
                for record in records:out.write(json.dumps(record,ensure_ascii=False)+'\n')
            main_h=Counter(h.get_text(' ',strip=True) for h in main.select('h1,h2,h3'))
            outside=[h.get_text(' ',strip=True) for h in soup.select('h1,h2,h3') if h.find_parent('main') is None]
            record_meta=metadata(output)
            result={'passed':True,'observed_at':stamp(),'site':'GOV.UK','navigation_levels':len(pages)-1,'pages':pages,
                'selector_candidates':selection,'selected_content_selector':selector,'content_records':len(records),
                'table_count':sum(r['tag']=='table' for r in records),'table_rows':sum(len(r.get('rows',[])) for r in records),
                'content_sha256':record_meta['sha256'],'content_bytes':record_meta['bytes'],
                'outside_main_headings':outside[:12],'duplicate_heading_text_inside_and_outside':list(set(main_h)&set(outside))[:12],
                'complete_scope':'All selected semantic blocks in one observed guidance body; linked pages not comprehensively crawled',
                'elapsed_seconds':round(time.monotonic()-started,3),'pacing_seconds':8,'owned_tab_closed':True,'preexisting_tabs_closed_by_probe':False}
            (args.output/'govuk-results.json').write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(bounded(result)),flush=True)
        finally:
            page.close()
            assert all(not original.is_closed() for original in originals),'An original tab was closed during probe'

if __name__=='__main__':main()
