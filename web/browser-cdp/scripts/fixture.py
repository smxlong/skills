"""Deterministic, entirely intercepted browser challenge pages (no HTTP server)."""
from html import escape
from urllib.parse import urlparse, parse_qs

ORIGIN = 'https://browser-challenge.invalid'
TOTAL = 12000


def render(url):
    """Return (content_type, body) for Playwright route.fulfill."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if parsed.path == '/large':
        records = ''.join(
            f'<article class="record" data-id="R{i:05d}"><h2>Item {i}</h2>'
            f'<a href="/detail/{i}">Details</a><span class="value">{i * 7}</span>'
            f'<p>{escape("nested observation " * 8)}<em>category {i % 13}</em></p></article>'
            for i in range(TOTAL))
        return 'text/html', shell('<h1>Inventory</h1>' + records,
            '<aside><h2>Item 9999</h2><article class="record" data-id="DECOY">Wrong scope</article></aside>')
    if parsed.path == '/pages':
        page = int(query.get('page', ['0'])[0])
        start, end = page * 90, min(page * 90 + 100, 1000)
        rows = ''.join(f'<tr data-id="P{i:04d}"><td>{i}</td><td>{i * 3}</td></tr>' for i in range(start, end))
        link = f'<a rel="next" href="/pages?page={page+1}">Next</a>' if end < 1000 else '<span>End</span>'
        return 'text/html', shell(f'<h1>Results</h1><table><tbody>{rows}</tbody></table>{link}')
    if parsed.path == '/virtual':
        return 'text/html', shell('''<h1>Virtual results</h1><div id="rows"></div><button id="more">Load next window</button>
<script>
let offset=0;
function draw(){document.querySelector('#rows').innerHTML=Array.from({length:Math.min(20,240-offset)},(_,j)=>`<article data-id="V${offset+j}">Record ${offset+j}</article>`).join('');document.querySelector('#more').disabled=offset>=220;}
document.querySelector('#more').onclick=()=>{offset+=20;draw()};draw();
</script>''')
    if parsed.path == '/controls':
        return 'text/html', shell('''<h1>Operations sandbox</h1><label>Query <input aria-label="Query"></label><button id="save">Save locally</button><output id="saved"></output>
<iframe title="Embedded controls" srcdoc="&lt;label&gt;Frame input&lt;input aria-label='Frame input'&gt;&lt;/label&gt;"></iframe><div id="shadow"></div><div id="late"></div>
<script>
document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<button>Shadow action</button><output></output>';
document.querySelector('#shadow').shadowRoot.querySelector('button').onclick=()=>document.querySelector('#shadow').shadowRoot.querySelector('output').textContent='shadow done';
document.querySelector('#save').onclick=()=>document.querySelector('#saved').textContent=document.querySelector('input').value;
setTimeout(()=>document.querySelector('#late').innerHTML='<button>Delayed action</button>',120);
</script>''')
    return 'text/html', shell('<h1>Challenge index</h1><a href="/large">Large inventory</a><a href="/pages">Paginated</a><a href="/virtual">Virtual</a><a href="/controls">Controls</a>')


def shell(main, extra=''):
    return '<!doctype html><html><head><title>Offline browser challenge</title></head><body><nav><h2>Inventory</h2><a href="/">Home</a></nav>' + extra + '<main>' + main + '</main></body></html>'


def install(page):
    def fulfill(route):
        content_type, body = render(route.request.url)
        route.fulfill(status=200, content_type=content_type, body=body)
    page.route(ORIGIN + '/**', fulfill)
