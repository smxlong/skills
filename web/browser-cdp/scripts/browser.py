# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.63.0", "beautifulsoup4==4.14.3"]
# ///
"""Disk-first browser session and offline HTML tools. Run with uv run --script."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit


def bounded(value, budget=10000):
    """Bound serialized UTF-8 output, including adversarial attribute strings."""
    for cap, items in ((160,30),(80,12),(40,8),(20,4)):
        def clip(v):
            if isinstance(v, str): return v[:cap]
            if isinstance(v, list): return [clip(x) for x in v[:items]]
            if isinstance(v, tuple): return [clip(x) for x in v[:items]]
            if isinstance(v, dict): return {str(k)[:cap]:clip(x) for k,x in list(v.items())[:30]}
            return v
        result = clip(value)
        if result != value and isinstance(result, dict): result['projection_truncated'] = True
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= budget:
            return result
    return dict(bounded=True, summary_omitted=True, reason='Refine the scope; structural summary exceeded output budget')


def public_url(url):
    parts = urlsplit(url)
    # Strip URL credentials, query and fragment from control-plane observations.
    authority = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parts.scheme, authority, parts.path, '', ''))[:300]


def evaluate_to_file(page, path, expression, arg=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve BEFORE executing JavaScript: output collision must have no effects.
    with path.open('x', encoding='utf-8') as dst:
        try:
            value = page.evaluate(expression, arg)
            envelope = dict(url=page.url, observed_at=stamp(), result=value)
        except Exception as exc:
            json.dump(dict(failed=True, error=type(exc).__name__, observed_at=stamp()), dst)
            raise
        json.dump(envelope, dst, ensure_ascii=False)
        dst.write('\n')
    return metadata(path)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def metadata(path, **extra):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open('rb') as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(chunk)
    return dict(path=str(path.resolve()), bytes=path.stat().st_size,
                sha256=digest.hexdigest(), **extra)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as dst:
        json.dump(value, dst, ensure_ascii=False)
        dst.write('\n')
    return metadata(path)


def soup_file(path):
    from bs4 import BeautifulSoup
    with Path(path).open(encoding='utf-8') as src:
        return BeautifulSoup(src, 'html.parser')


def html_map(path, limit=12, scope_selector=None):
    """Analyze the complete saved DOM but emit only structural evidence."""
    soup = soup_file(path)
    matches = soup.select(scope_selector) if scope_selector else [soup]
    if len(matches) != 1:
        raise ValueError(f'Map scope must match one region, found {len(matches)}')
    root = matches[0]
    def brief(node):
        # get_text on a heading only; never on a large container.
        return dict(tag=node.name, id=node.get('id'), classes=node.get('class', [])[:5],
                    text=node.get_text(' ', strip=True)[:160])
    tags = Counter(n.name for n in root.find_all(True))
    classes = Counter(c for n in root.find_all(True) for c in n.get('class', []))
    regions = []
    for n in root.select('main,article,section,nav,aside,form,table,[role=main],[role=dialog]')[:limit]:
        regions.append(dict(tag=n.name, id=n.get('id'), classes=n.get('class', [])[:5],
                            direct_children=len(n.find_all(recursive=False)),
                            descendants=len(n.find_all(True))))
    return bounded(dict(path=str(Path(path).resolve()), title=(soup.title.get_text()[:160] if soup.title else None),
                tags=tags.most_common(limit), repeated_classes=classes.most_common(limit),
                headings=[brief(n) for n in root.select('h1,h2,h3,h4,h5,h6')[:limit]],
                regions=regions, links=len(root.select('a[href]')), forms=len(root.select('form')),
                scripts=len(root.select('script')), frames=len(root.select('iframe')),
                bounded=True, limit=limit, scope=scope_selector))


def html_extract(source, output, row_selector, fields, allow_nested=False):
    """Stream records to JSONL from saved HTML. Missing values remain null."""
    soup = soup_file(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = soup.select(row_selector)
    selected_ids = {id(node) for node in selected}
    nested = sum(any(id(parent) in selected_ids for parent in node.parents) for node in selected)
    if nested and not allow_nested:
        raise ValueError(f'Selector matches {nested} nested rows; refine scope, use direct text, or explicitly allow_nested')
    count = 0
    missing = Counter()
    with output.open('x', encoding='utf-8') as dst:
        for row in selected:
            record = {}
            for name, spec in fields.items():
                node = row.select_one(spec['selector']) if spec.get('selector') else row
                value = None if node is None else (node.get(spec['attr']) if spec.get('attr') else node.get_text(' ', strip=True))
                record[name] = value
                if value is None:
                    missing[name] += 1
            dst.write(json.dumps(record, ensure_ascii=False) + '\n')
            count += 1
    return metadata(output, rows=count, missing=dict(missing), nested_rows=nested, source=str(Path(source).resolve()), row_selector=row_selector)


MAP_JS = """root => {
 root=root||document;
 const counts = {};
 for(const n of root.querySelectorAll('*')) counts[n.tagName]=(counts[n.tagName]||0)+1;
 const brief = n => ({tag:n.tagName,id:n.id.slice(0,160),classes:[...n.classList].slice(0,5).map(s=>s.slice(0,160)),role:n.getAttribute('role')?.slice(0,160)});
 return {url:location.origin+location.pathname.slice(0,300),title:document.title.slice(0,160),ready:document.readyState,
  tags:Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,12),
  headings:[...root.querySelectorAll('h1,h2,h3')].slice(0,12).map(n=>({...brief(n),text:n.textContent.slice(0,160)})),
  regions:[...root.querySelectorAll('main,article,nav,form,table,[role=dialog]')].slice(0,12).map(n=>({...brief(n),children:n.childElementCount})),
  frames:[...root.querySelectorAll('iframe')].slice(0,8).map(n=>({id:n.id.slice(0,160),name:n.name.slice(0,160),src:(n.getAttribute('src')||'').split(/[?#]/)[0].slice(0,300)})),
  links:root.querySelectorAll('a[href]').length};
}"""


def locate(scope, spec):
    if 'role' in spec:
        loc = scope.get_by_role(spec['role'], name=spec.get('name'), exact=spec.get('exact', True))
    elif 'label' in spec:
        loc = scope.get_by_label(spec['label'], exact=spec.get('exact', True))
    elif 'text' in spec:
        loc = scope.get_by_text(spec['text'], exact=spec.get('exact', True))
    else:
        loc = scope.locator(spec['selector'])
    if 'within' in spec:
        # Scope first, then resolve role/text inside it.
        return locate(scope.locator(spec['within']), {k:v for k,v in spec.items() if k != 'within'})
    return loc


def session(endpoint):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        context = browser.contexts[0]
        owned = []
        def register(tab):
            owned.append(tab)
            tab.on('popup', register)
        page = context.new_page()
        register(page)
        print(json.dumps(dict(ready=True, owned_tab=0, observed_at=stamp())), flush=True)
        try:
            for line in sys.stdin:
                started = time.monotonic()
                try:
                    cmd = json.loads(line)
                    action = cmd['action']
                    if action == 'quit':
                        break
                    scope = page.frame_locator(cmd['frame']) if 'frame' in cmd else page
                    result = {}
                    if action == 'new':
                        page = context.new_page()
                        register(page)
                        result = dict(owned_tab=len(owned)-1)
                    elif action == 'switch':
                        page = owned[cmd['owned_tab']]
                    elif action == 'goto':
                        if not cmd['url'].startswith(('https://','http://')):
                            raise ValueError('goto requires HTTP(S)')
                        response = page.goto(cmd['url'], wait_until='domcontentloaded', timeout=30000)
                        result = dict(url=public_url(page.url), status=response.status if response else None)
                    elif action == 'map':
                        result = bounded(page.locator(cmd['selector']).evaluate(MAP_JS) if 'selector' in cmd else page.evaluate(MAP_JS))
                    elif action == 'capture':
                        path = Path(cmd['path']); path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open('x', encoding='utf-8') as dst:
                            dst.write(page.content())
                        result = metadata(path, url=page.url, observed_at=stamp(), kind='rendered-dom')
                        save_json(str(path)+'.meta.json', result)
                        result = {**result, 'url': public_url(result['url'])}
                    elif action == 'evaluate':
                        # Arbitrary JavaScript is a capability, not an authorization boundary.
                        result = evaluate_to_file(page, cmd['path'], cmd['expression'], cmd.get('arg'))
                    elif action == 'inspect':
                        loc = locate(scope, cmd)
                        result = dict(count=loc.count(), sample=loc.evaluate_all("ns => ns.slice(0,8).map(n=>({tag:n.tagName,id:n.id.slice(0,160),role:n.getAttribute('role')?.slice(0,160),text:n.textContent.slice(0,160)}))"))
                    elif action == 'wait':
                        locate(scope, cmd).wait_for(state=cmd.get('state', 'visible'), timeout=cmd.get('timeout', 10000))
                    elif action in ('click','fill','press','hover','check','select_option'):
                        loc = locate(scope, cmd)
                        # Strict Playwright resolution intentionally rejects ambiguous controls.
                        if action in ('fill','press','select_option'):
                            getattr(loc, action)(cmd['value'], timeout=10000)
                        else:
                            getattr(loc, action)(timeout=10000)
                    elif action == 'scroll':
                        locate(scope, cmd).evaluate('(el,dy)=>el.scrollBy(0,dy)', cmd.get('pixels',650)) if 'selector' in cmd else page.mouse.wheel(0,cmd.get('pixels',650))
                    elif action == 'screenshot':
                        path = Path(cmd['path']); path.parent.mkdir(parents=True, exist_ok=True)
                        if path.exists(): raise FileExistsError(path)
                        target = locate(scope,cmd) if 'selector' in cmd else page
                        if target is page:
                            target.screenshot(path=str(path), full_page=cmd.get('full_page',False))
                        else: target.screenshot(path=str(path))
                        result = metadata(path)
                    else:
                        raise ValueError('Unsupported action; use a task script for event-driven workflows')
                    if action in ('map','inspect','goto'): result = bounded(result)
                    result['elapsed_s'] = round(time.monotonic()-started,3)
                    # No arbitrary page result is printed. map/inspect are bounded above.
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                except Exception as exc:
                    print(json.dumps(dict(error=type(exc).__name__, message=str(exc)[:1000], elapsed_s=round(time.monotonic()-started,3))), flush=True)
        finally:
            for tab in reversed(owned):
                if not tab.is_closed(): tab.close()
            # Do not call browser.close(): this is a borrowed browser.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    live = sub.add_parser('session'); live.add_argument('--endpoint', default='http://127.0.0.1:9223')
    mapping = sub.add_parser('map'); mapping.add_argument('html'); mapping.add_argument('--limit', type=int, default=12); mapping.add_argument('--scope', help='Unique CSS region to map')
    extract = sub.add_parser('extract'); extract.add_argument('html'); extract.add_argument('output'); extract.add_argument('--rows', required=True); extract.add_argument('--fields', required=True, help='JSON object of field: {selector, attr?}')
    extract.add_argument('--allow-nested', action='store_true', help='Permit intentional overlapping ancestor/descendant rows')
    args = parser.parse_args()
    if args.mode == 'session': session(args.endpoint)
    elif args.mode == 'map': print(json.dumps(html_map(args.html, min(max(args.limit,1),30), args.scope), ensure_ascii=False))
    else: print(json.dumps(html_extract(args.html,args.output,args.rows,json.loads(args.fields), args.allow_nested)))


if __name__ == '__main__': main()
