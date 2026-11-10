"""Isolated operations probes against an existing Chrome; no external requests."""
import argparse
import json
import tempfile
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

HTML = '''<!doctype html><title>Operations fixture</title>
<nav><button>Save</button></nav><main>
<form onsubmit="event.preventDefault();document.querySelector('#status').textContent='Saved '+this.elements.title.value">
<label>Title <input name=title></label><label><input type=checkbox>Enabled</label>
<label for=mode>Mode</label><select id=mode><option>Draft</option><option>Ready</option></select><button>Save</button></form>
<p id=status role=status></p>
<button onclick="document.querySelector('#status').textContent=confirm('Continue?')?'Confirmed':'Dismissed'">Confirm</button>
<a href=/popup target=_blank>Open report</a><a href=/data.csv download>Export CSV</a>
<label>Attachment <input type=file onchange="document.querySelector('#status').textContent=this.files[0].name"></label>
<iframe title=Editor src=/frame></iframe><div id=shadow></div>
<button onclick="fetch('/api/data').then(r=>r.json()).then(r=>document.querySelector('#status').textContent=r.result)">Load data</button>
<button onclick="setTimeout(()=>document.querySelector('#status').textContent='Delayed ready',120)">Delay</button>
<a href=/next>Continue</a></main>
<script>document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<button>Shadow action</button>';</script>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:9223')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.endpoint)
        context = browser.contexts[0]
        originals = set(context.pages)
        owned = []
        page = context.new_page()
        page.set_default_timeout(5000)
        owned.append(page)
        page.on('popup', lambda popup: owned.append(popup))
        def serve(route):
            path = route.request.url.split('operations.test')[-1]
            if path == '/frame':
                route.fulfill(content_type='text/html', body='<label>Frame value <input></label>')
            elif path == '/popup':
                route.fulfill(content_type='text/html', body='<title>Report</title><main>Ready</main>')
            elif path == '/data.csv':
                route.fulfill(content_type='text/csv', headers={'Content-Disposition':'attachment; filename="report.csv"'}, body='id,value\n1,ok\n')
            elif path == '/api/data':
                route.fulfill(content_type='application/json', body='{"result":"Network ready"}')
            elif path == '/next':
                route.fulfill(content_type='text/html', body='<main><h1>Next step</h1></main>')
            else:
                route.fulfill(content_type='text/html', body=HTML)
        page.route('https://operations.test/**', serve)
        # Newly opened popup needs routing before its first request.
        def popup_serve(route):
            route.fulfill(content_type='text/html', body='<title>Report</title><main>Ready</main>')
        context.route('https://operations.test/popup', popup_serve)
        try:
            page.goto('https://operations.test/', wait_until='domcontentloaded')
            main_scope = page.get_by_role('main')
            expect(page.get_by_role('button', name='Save', exact=True)).to_have_count(2)
            main_scope.get_by_label('Title', exact=True).fill('Fixture title')
            main_scope.get_by_label('Enabled', exact=True).set_checked(True)
            main_scope.get_by_label('Mode', exact=True).select_option(label='Ready')
            main_scope.get_by_role('button', name='Save', exact=True).click()
            expect(page.get_by_role('status')).to_have_text('Saved Fixture title')
            results['scoped_form'] = True
            def handle_dialog(dialog):
                assert dialog.type == 'confirm'
                dialog.accept()
            page.once('dialog', handle_dialog)
            main_scope.get_by_role('button', name='Confirm', exact=True).click()
            expect(page.get_by_role('status')).to_have_text('Confirmed')
            results['dialog'] = True
            with page.expect_popup() as popup_event:
                main_scope.get_by_role('link', name='Open report', exact=True).click()
            popup = popup_event.value
            popup.wait_for_load_state('domcontentloaded')
            expect(popup.get_by_role('main')).to_have_text('Ready')
            results['popup'] = True
            with page.expect_download() as download_event:
                main_scope.get_by_role('link', name='Export CSV', exact=True).click()
            download = download_event.value
            download_path = args.output / 'download.csv'
            try:
                download.save_as(download_path)
                results['download_save'] = 'saved'
            except Exception as exc:
                results['download_save'] = type(exc).__name__ + ': ' + str(exc).splitlines()[0]
                download_path.write_text(page.evaluate("async()=>{const r=await fetch('/data.csv');if(!r.ok)throw Error(r.status);return r.text()}"))
            assert download_path.read_text() == 'id,value\n1,ok\n'
            results['download_bytes_verified'] = True
            upload = args.output / 'upload.txt'
            upload.write_text('synthetic fixture')
            main_scope.get_by_label('Attachment', exact=True).set_input_files({'name':'upload.txt','mimeType':'text/plain','buffer':upload.read_bytes()})
            expect(page.get_by_role('status')).to_have_text('upload.txt')
            results['upload'] = True
            frame_value = page.frame_locator('iframe[title="Editor"]').get_by_label('Frame value')
            frame_value.fill('Frame edited')
            expect(frame_value).to_have_value('Frame edited')
            results['iframe'] = True
            expect(page.get_by_role('button', name='Shadow action', exact=True)).to_be_visible()
            results['shadow'] = True
            with page.expect_response(lambda r: r.url.endswith('/api/data') and r.request.method == 'GET') as response_event:
                main_scope.get_by_role('button', name='Load data', exact=True).click()
            response = response_event.value
            (args.output / 'response.json').write_bytes(response.body())
            expect(page.get_by_role('status')).to_have_text('Network ready')
            results['network'] = True
            main_scope.get_by_role('button', name='Delay', exact=True).click()
            expect(page.get_by_role('status')).to_have_text('Delayed ready')
            results['delayed_condition'] = True
            page.screenshot(path=str(args.output / 'viewport.png'), full_page=False)
            cdp = context.new_cdp_session(page)
            metrics = cdp.send('Page.getLayoutMetrics')
            assert metrics['cssLayoutViewport']['clientWidth'] > 0
            cdp.detach()
            results['cdp_screenshot'] = True
            main_scope.get_by_role('link', name='Continue', exact=True).click()
            page.wait_for_url('https://operations.test/next')
            expect(page.get_by_role('heading', name='Next step')).to_be_visible()
            results['navigation'] = True
        finally:
            context.unroute('https://operations.test/popup', popup_serve)
            for owned_page in owned:
                if not owned_page.is_closed():
                    owned_page.close()
            assert originals.issubset(set(context.pages)), 'An original page was closed'
            results['original_tabs_preserved'] = True
    results['elapsed_seconds'] = round(time.monotonic() - start, 3)
    results['observed_at_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    (args.output / 'operations-results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results))

if __name__ == '__main__':
    main()
