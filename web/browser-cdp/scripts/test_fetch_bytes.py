# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0"]
# ///
"""Offline fetch-bytes probes in an owned CDP tab; no public requests."""
import argparse
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright
from fetch_bytes import fetch_bytes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint',default='http://127.0.0.1:9223')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    start=time.monotonic()
    checks={}
    # Invalid configuration must fail before writing or touching the browser.
    class UntouchedPage:
        def evaluate_handle(self,*a,**kw):
            raise AssertionError('Invalid config reached browser')
    invalid=[{'chunk_size':0},{'chunk_size':-1},{'chunk_size':1.5},
             {'max_bytes':-1},{'max_bytes':1.5}]
    for i,config in enumerate(invalid):
        path=args.output/f'invalid-{i}.bin'
        rejected=False
        try:
            fetch_bytes(UntouchedPage(),'https://byte-limit.invalid/data',path,**config)
        except (ValueError,TypeError):
            rejected=True
        assert rejected and not path.exists()
    checks['invalid_config_before_side_effects']=len(invalid)
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(args.endpoint)
        page=browser.contexts[0].new_page()
        def route_fixture(route):
            if route.request.url.endswith('/'):
                route.fulfill(content_type='text/html',body='<title>Byte fixture</title>')
            else:
                route.fulfill(content_type='application/octet-stream',body=bytes(range(256)))
        page.route('https://byte-limit.invalid/**',route_fixture)
        try:
            page.goto('https://byte-limit.invalid/')
            rejected=False
            try:
                fetch_bytes(page,'https://byte-limit.invalid/data',args.output/'declared-limit.bin',max_bytes=32)
            except Exception as exc:
                rejected='exceeds byte limit' in str(exc)
            assert rejected
            checks['declared_limit_rejected']=True
            good=fetch_bytes(page,'https://byte-limit.invalid/data',args.output/'binary.bin',max_bytes=256,chunk_size=7)
            assert (args.output/'binary.bin').read_bytes()==bytes(range(256))
            checks['all_byte_values_preserved']=good['bytes']==256
            # A synthetic WHATWG Response without a Content-Length exercises
            # incremental stream enforcement independently of declared length.
            page.evaluate('()=>{window.calls=0;window.fetch=async()=>{window.calls++;return new Response(new Uint8Array(64),{status:200})}}')
            rejected=False
            try:
                fetch_bytes(page,'https://byte-limit.invalid/data',args.output/'stream-limit.bin',max_bytes=16)
            except Exception as exc:
                rejected='exceeds byte limit' in str(exc)
            assert rejected
            checks['undeclared_stream_limit_rejected']=True
            existing=args.output/'existing.bin'
            existing.write_bytes(b'keep')
            calls_before=page.evaluate('window.calls')
            try:
                fetch_bytes(page,'https://byte-limit.invalid/data',existing)
                raise AssertionError('Existing output accepted')
            except FileExistsError:
                pass
            assert existing.read_bytes()==b'keep' and page.evaluate('window.calls')==calls_before
            checks['existing_output_rejected_before_fetch']=True
            page.evaluate('()=>{window.fetch=async()=>new Response(new Uint8Array(0),{status:200})}')
            empty=fetch_bytes(page,'https://byte-limit.invalid/empty',args.output/'empty.bin',max_bytes=1)
            assert empty['bytes']==0
            checks['empty_response']=True
        finally:
            page.close()
    result={'passed':True,'checks':checks,'elapsed_seconds':round(time.monotonic()-start,3),
            'external_requests':0,'owned_tab_closed':page.is_closed()}
    (args.output/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    main()
