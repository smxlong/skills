"""Run isolated behavioral challenges in existing Chrome; print only metrics.

Use a Python environment containing playwright. All fixture network traffic is
intercepted; browser profile and pre-existing pages are left untouched.
"""
import argparse
import subprocess
import sys
import hashlib
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright
from fixture import install, ORIGIN, TOTAL, render
from browser import html_map, html_extract, bounded


def write_jsonl(path, records):
    with path.open('w') as stream:
        for row in records:
            stream.write(json.dumps(row) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:9223')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    adversarial = args.output / 'huge-attribute.html'
    adversarial.write_text('<title>Sample</title><main id="' + 'x'*100000 + '"><h1 class="' + 'y'*100000 + '">Title</h1></main>')
    assert len(json.dumps(html_map(adversarial))) < 12000, 'Attribute strings escaped structural output bounds'
    unicode_probe = {'headings': [{'id': '😀'*100000, 'classes': ['😀'*100000]*5} for _ in range(12)]}
    unicode_bounded_bytes = len(json.dumps(bounded(unicode_probe), ensure_ascii=False).encode('utf-8'))
    assert unicode_bounded_bytes <= 10000, 'Character count did not bound UTF-8 bytes'
    timings = {}
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.endpoint)
        context = browser.contexts[0]
        originals = list(context.pages)
        page = context.new_page()
        install(page)
        try:
            before = time.monotonic()
            page.goto(ORIGIN + '/large', wait_until='domcontentloaded')
            html_path = args.output / 'large.html'
            html_path.write_text(page.content())
            rows = page.locator('main article.record').evaluate_all("nodes=>nodes.map(n=>({id:n.dataset.id,title:n.querySelector('h2').textContent,value:Number(n.querySelector('.value').textContent),url:n.querySelector('a').href}))")
            write_jsonl(args.output / 'large.jsonl', rows)
            assert len(rows) == TOTAL
            assert len({r['id'] for r in rows}) == TOTAL
            assert sum(r['value'] for r in rows) == 7 * TOTAL * (TOTAL-1) // 2
            assert rows[-1]['id'] == 'R11999'
            timings['large'] = round(time.monotonic() - before, 3)
            # Forward-test the supplied skill helpers, not only equivalent code.
            before = time.monotonic()
            overview = html_map(html_path, limit=12)
            assert len(json.dumps(overview)) < 12000
            assert len(overview['headings']) <= 12
            assert dict(overview['repeated_classes'])['record'] == TOTAL + 1
            fields = {'id': {'attr': 'data-id'}, 'title': {'selector': 'h2'},
                      'value': {'selector': '.value'}, 'missing': {'selector': '.absent'},
                      'nested': {'selector': 'p'}}
            extracted = html_extract(html_path, args.output/'helper-extract.jsonl', 'main article.record', fields)
            assert extracted['rows'] == TOTAL
            assert extracted['missing'] == {'missing': TOTAL}
            with (args.output/'helper-extract.jsonl').open() as extracted_stream:
                first = json.loads(extracted_stream.readline())
            assert first['id'] == 'R00000' and first['value'] == '0'
            assert first['missing'] is None and 'category 0' in first['nested']
            assert extracted['sha256'] == hashlib.sha256((args.output/'helper-extract.jsonl').read_bytes()).hexdigest()
            timings['offline_helpers'] = round(time.monotonic() - before, 3)
            before = time.monotonic()
            seen = {}
            page_count, observations = 0, 0
            page.goto(ORIGIN + '/pages', wait_until='domcontentloaded')
            while True:
                data = page.locator('main tbody tr').evaluate_all("ns=>ns.map(n=>({id:n.dataset.id,value:Number(n.cells[1].textContent)}))")
                observations += len(data)
                for row in data:
                    seen[row['id']] = row
                page_count += 1
                next_link = page.locator('a[rel=next]')
                if not next_link.count():
                    break
                next_link.click()
                page.wait_for_url('**/pages?page=' + str(page_count))
                assert page_count < 15, 'Pagination cycle'
            write_jsonl(args.output / 'pages.jsonl', seen.values())
            assert len(seen) == 1000
            assert observations > len(seen)
            timings['pagination'] = round(time.monotonic() - before, 3)
            before = time.monotonic()
            page.goto(ORIGIN + '/virtual', wait_until='domcontentloaded')
            seen_virtual = {}
            windows = 0
            while True:
                batch = page.locator('#rows article').evaluate_all("ns=>ns.map(n=>({id:n.dataset.id,text:n.textContent}))")
                for row in batch:
                    seen_virtual[row['id']] = row
                windows += 1
                if page.get_by_role('button', name='Load next window').is_disabled():
                    break
                old_first = batch[0]['id']
                page.get_by_role('button', name='Load next window').click()
                page.wait_for_function("old=>document.querySelector('#rows article').dataset.id!==old", arg=old_first)
                assert windows < 20, 'Virtualization cycle'
            write_jsonl(args.output / 'virtual.jsonl', seen_virtual.values())
            assert len(seen_virtual) == 240
            assert page.locator('#rows article').count() == 20
            timings['virtual'] = round(time.monotonic() - before, 3)
            page.goto(ORIGIN + '/controls', wait_until='domcontentloaded')
            page.get_by_role('textbox', name='Query', exact=True).fill('fixture value')
            page.get_by_role('button', name='Save locally', exact=True).click()
            assert page.locator('#saved').text_content() == 'fixture value'
            page.frame_locator('iframe').get_by_role('textbox', name='Frame input').fill('frame value')
            assert page.frame_locator('iframe').get_by_role('textbox', name='Frame input').input_value() == 'frame value'
            page.get_by_role('button', name='Shadow action').click()
            assert page.locator('#shadow output').text_content() == 'shadow done'
            page.get_by_role('button', name='Delayed action').wait_for(state='visible')
            metrics = dict(passed=True, large_records=TOTAL, html_bytes=html_path.stat().st_size,
                jsonl_bytes=(args.output/'large.jsonl').stat().st_size,
                unique_paginated=len(seen), pagination_observations=observations, pages=page_count,
                virtual_records=len(seen_virtual), virtual_windows=windows,
                controls=['fill/save readback', 'iframe', 'open shadow DOM', 'delayed render'],
                timings_seconds=timings, elapsed_seconds=round(time.monotonic()-started,3),
                html_sha256=hashlib.sha256(html_path.read_bytes()).hexdigest())
        finally:
            page.close()
        assert all(p in context.pages and not p.is_closed() for p in originals)
        assert set(context.pages) == set(originals)
        metrics['original_tabs_preserved'] = True
    # Real JSONL session: send multi-megabyte fixture through stdin to Chrome,
    # then prove helper stdout contains only bounded maps/metadata, never rows.
    capture = (args.output/'session-capture.html').resolve()
    injected_html = render(ORIGIN + '/large')[1]
    commands = [
        {'action':'evaluate', 'expression':'html=>{document.open();document.write(html);document.close();return {mounted:true}}', 'arg':injected_html, 'path':str(args.output/'mounted.json')},
        {'action':'map'},
        {'action':'inspect','selector':'main article.record'},
        {'action':'capture','path':str(capture)},
        {'action':'evaluate', 'expression':'html=>{document.open();document.write(html);document.close();return {mounted:true}}', 'arg':adversarial.read_text(), 'path':str(args.output/'adversarial-mounted.json')},
        {'action':'map'},
        {'action':'inspect','selector':'main'},
        {'action':'quit'}]
    helper = Path(__file__).with_name('browser.py')
    proc = subprocess.run([sys.executable, str(helper), 'session', '--endpoint', args.endpoint],
        input=''.join(json.dumps(cmd)+'\n' for cmd in commands), text=True,
        capture_output=True, timeout=45, check=True)
    responses = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(responses) == 8 and not any('error' in r for r in responses)
    assert all(len(line) < 12000 for line in proc.stdout.splitlines())
    assert len(proc.stdout) < 24000
    assert responses[3]['count'] == TOTAL and len(responses[3]['sample']) == 8
    provenance = json.loads(Path(str(capture)+'.meta.json').read_text())
    assert provenance['kind'] == 'rendered-dom' and provenance['observed_at']
    assert provenance['sha256'] == hashlib.sha256(capture.read_bytes()).hexdigest()
    metrics['helper_session_stdout_bytes'] = len(proc.stdout.encode())
    metrics['unicode_bounded_bytes'] = unicode_bounded_bytes
    metrics['helper_offline_and_session_passed'] = True
    metrics['elapsed_seconds'] = round(time.monotonic()-started,3)
    (args.output/'benchmark.json').write_text(json.dumps(metrics, indent=2)+'\n')
    print(json.dumps(metrics))


if __name__ == '__main__':
    main()
