# Extraction: disk as the data plane

## Reconnaissance and HTML drilldown

Keep browser automation in one persistent process. Save HTML with
`Path(...).write_text(page.content(), encoding='utf-8')`; never return the string
to an agent tool result. Record actual URL, UTC time and SHA256 next to it.
`page.content()` serializes rendered DOM; to preserve original response bytes,
install a response listener before navigation and save the selected response.
The two captures answer different questions. **CDP `response.body()` can itself
be a decoded/reencoded text representation**, as observed on a legacy Census
CSV. When byte fidelity matters, use browser Fetch bytes with the chunked
`fetch_bytes` helper described below, and compare declared encoding/length.
These are HTTP response-body bytes after browser content decompression, not
compressed wire packets.

The supplied offline `map` makes one complete parser pass and emits bounded
headings, landmarks and frequency counts. Its first regions/headings are a
sample, not exhaustive discovery. If a target is missing from that sample,
use `map --scope "#observed-region"` (or live `{ "action":"map", "selector":"#observed-region" }`)
to map one unique region, or query a specific tag/class/id pattern in a local script and emit counts plus
bounded identifiers. Avoid `rg` against one-line HTML: one matching line can be
megabytes. Similarly, `head -n 5` on JSON/HTML does not bound characters.

Example offline drilldown after a map shows repeated `.record`:

```python
from browser import soup_file, html_extract
soup = soup_file('page.html')
# Compare candidate scopes, not just global class count.
for selector in ('main .record', 'aside .record', '#results > .record'):
    rows = soup.select(selector)
    print({'selector': selector, 'count': len(rows),
           'first_ids': [(n.get('data-id') or '')[:100] for n in rows[:3]]})
# Then write every selected record without printing it.
report = html_extract('page.html', 'records.jsonl', '#results > .record', {
    'id': {'attr': 'data-id'}, 'title': {'selector': 'h2'},
    'href': {'selector': 'a', 'attr': 'href'},
})
print(report)
```

`html_extract` selects the first matching descendant per field, and emits null
for absent nodes/attributes; empty text remains an empty string. Validate that
field cardinality matches the schema. Nested matched **rows** are rejected by default to prevent ancestor/descendant
text multiplication; refine the selector or explicitly pass `allow_nested=True`
(`--allow-nested` in the CLI) for intended hierarchy. For repeated nested fields (authors,
prices, tags), custom code should use `select()` and preserve arrays, rather than
silently taking the first. Normalize text deliberately; retain raw HTML for
later corrections. `href` is the literal attribute; use `urllib.parse.urljoin`
with the metadata's source URL. Locale-dependent numbers, displayed dates and
invisible text require task-specific handling.

The helper parses HTML in memory with BeautifulSoup, appropriate for ordinary
multi-megabyte captures. For hundreds of MB or unbounded datasets, stream source
pages to individual files and use an incremental parser (`lxml.etree.iterparse`
for well-formed XML; an HTML-aware streaming parser for irregular HTML), or
capture row batches from the browser. Do not concatenate an entire crawl into
one huge soup. DOM `textContent` includes hidden text; use scoped `inner_text()`
when visible wording matters, with a bounded element count.

## Chunked rendered records

Use a task script when a single projection would be huge. Transfer bounded
batches from the browser to Python; write each batch immediately:

```python
import json
selector = 'main #results > article.record'
rows = page.locator(selector)
initial_count = rows.count()
with open('records.jsonl', 'x', encoding='utf-8') as out:
    for start in range(0, initial_count, 500):
        batch = rows.evaluate_all('''(nodes, s) => nodes.slice(s,s+500).map(n => ({
          id:n.dataset.id, title:n.querySelector('h2')?.textContent?.trim() ?? null
        }))''', start)
        for record in batch:
            out.write(json.dumps(record, ensure_ascii=False)+'\n')
```

This assumes a stable DOM during collection. If sorting/hydration/updates can
change indices, freeze the snapshot to disk first, or collect by stable IDs and
retry to a clear checkpoint. `evaluate_all` above materializes the node list;
for enormous live DOMs use app pagination or a narrower locator per batch. Never
infer the source total from the current DOM count on a virtualized list.

## Observe JSON, avoid redundant UI scraping

Install listeners **before** the triggering action. Narrow by observed host,
path, method, resource type and status; do not persist all responses or headers.
HAR and storage-state dumps often contain credentials and unrelated activity.

```python
from pathlib import Path
# URL pattern must come from an observed request, not a guessed private API.
with page.expect_response(lambda r: '/api/items?' in r.url and
                          r.request.method == 'GET' and r.status == 200) as pending:
    page.get_by_role('button', name='Next', exact=True).click()
response = pending.value
Path('page-002.json').write_bytes(response.body())
# Parse and count locally; save only the necessary provenance fields.
```

Response bodies still buffer in Playwright/Python. For enormous downloads use
the download facility and `save_as`; for a public permitted endpoint stream with
a local HTTP client when the task permits it. Browser-context `fetch`/API requests
can share authorization but are subject to origin, CORS, CSP and server controls.
Use UI-observed endpoints only when allowed; do not copy cookie jars or tokens
into shell arguments. Changing a UI GET into an undocumented write is not
research. Stop on access denials, challenge pages, rate limits or session loss;
do not treat a login HTML response as an empty JSON page. Honor Retry-After and
site/task pacing, with a bounded retry count.

## Preserve legacy/binary bytes

`fetch(...).text()` assumes text decoding; writing it as UTF-8 can introduce
replacement characters or change bytes. `response.body()` is not always a raw
escape hatch over CDP: a Census CSV was 1,769,182 bytes via Fetch ArrayBuffer but
1,769,183 via `response.body()`, which reencoded one legacy character. Keep
representation and encoding explicit; hashes of transformed text do not verify
source-byte fidelity.

For a known authorized GET URL, use the supplied reusable helper:

```python
from fetch_bytes import fetch_bytes
report = fetch_bytes(page, observed_url, 'source.csv', max_bytes=64*1024*1024)
print(report)  # metadata only
# Decode strictly using the source's declared/verified encoding, then parse CSV.
```

It enforces a download byte cap while reading in Chrome, retains bounded source
data in a JS handle, transfers base64 slices to Python, and disposes the handle.
Base64 is an internal transport encoding, never tool output. Peak Chrome memory
can exceed the source size due to buffered chunks and the assembled byte array;
use a proper streaming download/client for files larger than the chosen cap.
A failed transfer may leave a partial file; accept the artifact only after
successful completion, expected-length/format checks and a hash. This GET uses
the page's normal origin/session/CORS/CSP rules and does not export cookies.
Do not use it to replay an uncertain POST or to bypass an access restriction.

## Pagination and resumability

A collection loop needs these invariants:

1. Save each raw page/window with source URL/cursor and time before advancing.
2. Parse records and preserve observations; deduplicate a derived dataset by a
   stable source ID or explicit composite key, not title or row position. A
   displayed section number can have multiple effective versions; preserve
   version/date/context in the key rather than discarding repeated numbers.
3. Commit checkpoint **after** the raw data and derived output are durable. On
   resume, reprocessing the last raw page must be idempotent. Atomic replace a
   checkpoint; use SQLite transactions for larger incremental datasets.
4. Detect repeated cursors, repeated page fingerprints and no-new-ID windows.
   They indicate a stall, not necessarily success. Stop with a reason and keep
   partial output rather than looping forever or declaring exhaustion.
5. Advance the observed next link/cursor; wait for the new cursor, first ID or
   matching response before extracting. Avoid a race where old rows are read
   while a loading indicator is already visible.
6. Stop successfully on documented cursor exhaustion/disabled next/end marker.
   Compare unique count to the source total when available, recording filters
   and time scope. Report caps, missing pages and changing source totals.

A useful run manifest is `{started_at, source, schema, pages, observations,
unique_ids, duplicates, missing_fields, expected_total, checkpoint,
termination_reason, complete, artifacts:[{path,sha256,bytes}]}`.
Keep sensitive query strings in protected provenance files, not conversation.

## Infinite scrolling and virtualized lists

Persist the current rendered window **before** it is recycled. Scroll the
actual list container; `page.mouse.wheel` may scroll the wrong ancestor.
Check new stable IDs and the observed loading/end state after every movement.
Virtual lists can keep their node count and scroll height constant; even visible
text can repeat. Overlap windows and deduplicate IDs. A finite no-new-ID budget
is a useful operational stop but is only `stalled`/`partial` unless an explicit
end or known total confirms completion. Rows can mount inside open shadow DOM
or frames; default `document.querySelectorAll` and HTML parsers won't see them.
Prefer the app's observed data responses when they offer more reliable coverage.

## Offline QA

Verify unique IDs, null counts, first/last page boundaries, expected totals,
pagination overlap and a small deterministic sample against raw captures.
Preserve both raw observations and deduplicated results; don't silently discard
conflicting versions of a record. A changing source is not a transactionally
consistent snapshot. Cite source URLs and capture times, with limitations.
