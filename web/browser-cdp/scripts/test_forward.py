"""Independent forward challenge: noisy extraction + failure recovery semantics."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from playwright.sync_api import sync_playwright
from browser import html_extract, html_map, public_url
from fixture import render, ORIGIN, TOTAL


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    start=time.monotonic()
    assert public_url('https://user:password@example.test/path?token=QUERY_SECRET#FRAGMENT_SECRET')=='https://example.test/path'
    artifact=lambda name:str((args.output/name).resolve())
    # Script receives fixture bytes via stdin; neither page stream nor rows reach stdout.
    html=render(ORIGIN+'/large')[1]
    duplicate=Path(artifact('existing.json'))
    duplicate.write_text('{"already":true}\n')
    commands=[
        {'action':'evaluate','expression':'html=>{document.open();document.write(html);document.close();window.attempts=0;return true}', 'arg':html,'path':artifact('mounted.json')},
        {'action':'map'},
        {'action':'inspect','selector':'main article.record'},
        {'action':'capture','path':artifact('page.html')},
        # Failure must not kill the session, and duplicate-name actions must be strict.
        {'action':'evaluate','expression':"()=>{document.body.insertAdjacentHTML('beforeend','<button>Same</button><button>Same</button>');return true}",'path':artifact('controls.json')},
        {'action':'click','role':'button','name':'Same'},
        {'action':'evaluate','expression':'()=>++window.attempts','path':str(duplicate)},
        {'action':'evaluate','expression':'()=>({attempts:window.attempts})','path':artifact('attempts.json')},
        {'action':'map'},
        {'action':'evaluate','expression':"()=>{location.hash='FRAGMENT_SECRET';window.open('about:blank');return {opened:true}}",'path':artifact('popup.json')},
        {'action':'map'},
        {'action':'capture','path':artifact('redacted.html')},
        {'action':'map'},
        {'action':'quit'},
    ]
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp('http://127.0.0.1:9223')
        originals=set(browser.contexts[0].pages)
        proc=subprocess.run([sys.executable,str(Path(__file__).with_name('browser.py')),'session'],input=''.join(json.dumps(x)+'\n' for x in commands),text=True,capture_output=True,timeout=40,check=True)
        assert set(browser.contexts[0].pages)==originals,'Tab ownership changed'
    replies=[json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies)==len(commands)
    assert replies[6]['error']=='Error' and 'strict mode violation' in replies[6]['message']
    assert replies[7]['error']=='FileExistsError'
    attempts=json.loads(Path(artifact('attempts.json')).read_text())['result']['attempts']
    assert attempts==0,'Output conflict executed JavaScript'
    assert 'FRAGMENT_SECRET' not in proc.stdout,'Console leaked fragment'
    sidecar=json.loads(Path(artifact('redacted.html.meta.json')).read_text())
    assert '#FRAGMENT_SECRET' in sidecar['url'],'Full provenance URL lost'
    assert replies[-1].get('title')=='Offline browser challenge','Session did not recover'
    report=html_extract(artifact('page.html'),artifact('records.jsonl'),'main article.record',{'id':{'attr':'data-id'},'title':{'selector':'h2'},'value':{'selector':'.value'}})
    assert report['rows']==TOTAL
    ids=set()
    with Path(report['path']).open() as src:
        for line in src:
            ids.add(json.loads(line)['id'])
    assert len(ids)==TOTAL and 'DECOY' not in ids
    result={
        'passed':True,'records':report['rows'],'unique_ids':len(ids),'decoy_excluded':True,
        'mounted_fixture_complete':len(ids)==TOTAL,'production_completeness_claim':False,
        'session_stdout_bytes':len(proc.stdout.encode()),'max_stdout_line_bytes':max(len(x.encode()) for x in proc.stdout.splitlines()),
        'ambiguous_control_rejected':True,'session_recovered_after_errors':True,'original_tabs_preserved':True,
        'existing_output_preserved':duplicate.read_text()=='{"already":true}\n',
        'evaluation_skipped_on_output_conflict':attempts==0,
        'owned_popup_cleaned':True,'console_url_redacted':True,'full_provenance_preserved':True,
        'elapsed_seconds':round(time.monotonic()-start,3),
        'html_sha256':hashlib.sha256(Path(artifact('page.html')).read_bytes()).hexdigest(),
    }
    Path(artifact('forward-results.json')).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
