# Browser operations: act, observe, verify

Use this reference for interactive work and secondary browser mechanisms. All
examples assume Python sync Playwright and a `page` owned by the current task.
Keep the existing browser/context alive. Attach with
`pw.chromium.connect_over_cdp('http://127.0.0.1:9223')`; create a page from
`browser.contexts[0]`, track it and any popups, and close only those pages in
`finally`. Install the cleanup boundary immediately after creating the page,
before other setup can throw. For recoverable long runs, journal its CDP target
ID locally before further work; after a disconnect, an unmarked blank tab cannot
be safely distinguished from an existing personal tab. Exiting `sync_playwright()` disconnects the client. Do not call
`browser.close()` or close the borrowed context. Context-wide route handlers,
permissions, timeouts and download settings affect other tabs: prefer page scope
and remove any necessary context handler by its exact function identity.

## Built-in session commands

For the persistent JSON-lines helper, use `within` to scope controls and `frame`
for an iframe selector. Inspect the resulting status after an action:

```json
{"action":"fill","within":"main form","label":"Title","value":"Synthetic title"}
{"action":"select_option","label":"Mode","value":"ready"}
{"action":"check","label":"Enabled"}
{"action":"press","label":"Title","value":"Tab"}
{"action":"click","within":"main form","role":"button","name":"Save"}
{"action":"wait","role":"status","state":"visible","timeout":7000}
{"action":"inspect","role":"status"}
{"action":"fill","frame":"iframe[title=Editor]","label":"Frame value","value":"Approved value"}
{"action":"screenshot","selector":"main","path":"artifacts/main.png"}
{"action":"new"}
{"action":"switch","owned_tab":0}
```

`new` returns the owned tab index. These are task-owned indices, not the person's
existing tab numbers. The helper tracks popups for cleanup; use a task script
with an event wait when you need to work inside one. `hover` takes the same
locator fields as `click`; `scroll` takes optional `selector` and `pixels`.
`wait` observes attachment/visibility state, not a server-side save: inspect the
new value or matching response too. `evaluate` writes complete results to its
required fresh path, reserving it before JavaScript runs. An execution failure
leaves an error artifact; use a new path after inspecting state before retry.
URL observations omit query, fragment and userinfo; capture/evaluate provenance
retains the full URL on disk. This is not general secret detection for paths or
page text. Use task-appropriate care with diagnostic output.

## Locators, forms and navigation

Inspect names/attributes within a small region first. Prefer accessible role +
observed name, label or stable test ID, scoped to the relevant form/card/dialog.
Do not use `.first`/`.nth(0)` to conceal a duplicate match. A locator should match
exactly one intended element before an action; indexing is useful only when the
ordering itself was inspected. Wrapping a `<select>` in a `<label>` can include
option text in its accessible name: don't infer names from visual text alone.

```python
from playwright.sync_api import expect
page.set_default_timeout(7000)
main = page.get_by_role('main')
form = main.locator('form').filter(has=page.get_by_label('Title', exact=True))
expect(form).to_have_count(1)
form.get_by_label('Title', exact=True).fill('Synthetic title')
form.get_by_label('Enabled', exact=True).set_checked(True)
form.get_by_label('Mode', exact=True).select_option(label='Ready')
form.get_by_role('button', name='Save', exact=True).click()
expect(page.get_by_role('status')).to_have_text('Saved Synthetic title')
```

`fill`/`set_checked`/`select_option` express the intended state and tolerate
reruns better than blind click toggles. Use `press('Enter')`, hover or keyboard
shortcuts only when the site's interaction calls for them. `locator.click()`
waits for actionability; `force=True` can hide overlays, stale state or a wrong
locator. Inspect the obstruction or open dialog before forcing anything.

```python
page.goto(url, wait_until='domcontentloaded', timeout=30000)
expect(page.get_by_role('heading', name='Results', exact=True)).to_be_visible()
# For a transition triggered by a control:
page.get_by_role('link', name='Continue', exact=True).click()
page.wait_for_url('**/next')
expect(page.get_by_role('heading', name='Next step')).to_be_visible()
```

A successful click or URL change is insufficient evidence of a saved action;
verify a receipt/status, resulting field, URL + meaningful page state, or the
specific successful response. SPA transitions may retain the URL. Wait for
that state rather than `networkidle` or a fixed sleep. For public research,
retain required site pacing between interactions even when readiness is fast.

## Popups and dialogs

Register event waits **before** triggering the action. Keep cleanup ownership
when a popup opens unexpectedly too (`page.on('popup', lambda popup: owned.append(popup))`).

```python
with page.expect_popup() as popup_event:
    page.get_by_role('link', name='Open report', exact=True).click()
popup = popup_event.value
popup.wait_for_load_state('domcontentloaded')
expect(popup.get_by_role('main')).to_be_visible()
# Track popup for close in finally; do not close an unrelated newly noticed tab.

# Attach only for an expected dialog and resolve it inside the handler.
def handle(dialog):
    if dialog.type == 'confirm' and dialog.message == 'Continue?':
        dialog.accept()
    else:
        dialog.dismiss()
page.once('dialog', handle)
page.get_by_role('button', name='Confirm', exact=True).click()
```

Native JavaScript dialogs block the page when a listener leaves them unresolved.
An HTML modal is a DOM region: scope locators to `get_by_role('dialog')`. Accepting
an unexpected confirmation is not a recovery strategy. For a write with an
uncertain result, inspect state before retrying; replay can duplicate submission.

## Downloads and uploads across Windows/WSL or remote CDP

```python
with page.expect_download() as download_event:
    page.get_by_role('link', name='Export CSV', exact=True).click()
download = download_event.value
download.save_as(output_dir / 'report.csv')  # chosen safe local filename
```

**Observed in a Windows Chrome CDP probe:** the borrowed default context emitted a
download event but `save_as` raised `Download.save_as: canceled`. Treat this as a
capability check, not evidence that a file was saved. Don't globally enable
browser downloads or change a person's download directory to repair it. A
separate task-owned context with `accept_downloads=True` may be appropriate for
public work that does not need the existing session; test it first. This was
not needed or tested in the recorded probe.

For an inspected, authorized same-origin **GET** export, capture that known
response's bytes to disk or fetch it in the page and write the result directly
in Python. Check status, content type, size and expected records; an HTML login
page is not a CSV. For modest text exports whose UTF-8 text decoding is intended (not a raw-byte
archive):

```python
text = page.evaluate('''async url => {
  const r = await fetch(url, {credentials:'same-origin'});
  if (!r.ok) throw Error(`HTTP ${r.status}`);
  return await r.text();
}''', observed_export_url)
path.write_text(text, encoding='utf-8')
# Print file metadata/counts only; do not print text.
```

For byte-exact or legacy-encoded exports use the chunked `fetch_bytes` helper;
CDP response bodies can be text-transcoded on this endpoint. For ordinary JSON
responses, response-body capture is often sufficient;
see extraction guidance. Never repeat a POST export/payment operation simply
because the download failed. Browser-origin fetch follows CORS, CSP and session
rules; an error doesn't authorize a bypass.

An upload is an external submission: use only the file/data authorized by the
current task. A remote Windows browser may not resolve the agent's Linux path.
A Playwright file payload transfers bytes without assuming shared filesystem:

```python
page.get_by_label('Attachment', exact=True).set_input_files({
    'name': 'approved.txt', 'mimeType': 'text/plain',
    'buffer': approved_path.read_bytes(),
})
```

For a custom file chooser, wrap its trigger in `page.expect_file_chooser()` and
call `event.value.set_files(...)`. Verify the selected filename and any upload
completion state separately. The payload form was tested against Windows
Chrome over CDP; the file-chooser variation is the standard API recipe.

## Frames, shadow DOM and visual fallback

```python
editor = page.frame_locator('iframe[title="Editor"]')
editor.get_by_label('Frame value').fill('Approved value')
expect(editor.get_by_label('Frame value')).to_have_value('Approved value')
# Role/CSS locators pierce open shadow roots automatically.
expect(page.get_by_role('button', name='Shadow action', exact=True)).to_be_visible()
```

`document.querySelector` doesn't cross frames or shadow roots. Inspect frame
count/name/URL metadata and select the intended frame; keep frame content on
disk too. Cross-origin frames require frame APIs rather than parent-page DOM
JavaScript. Standard locators traverse open shadow roots; XPath doesn't, and
closed shadow roots aren't supported by that mechanism. Canvas/map controls
may require a viewport screenshot and coordinate input. After visual input,
verify an observable state; coordinates alone are fragile evidence.

```python
page.screenshot(path=str(output_dir / 'viewport.png'), full_page=False)
# Prefer a region when it answers the question:
page.get_by_role('main').screenshot(path=str(output_dir / 'main.png'))
```

Avoid full-page screenshots of enormous feeds. Capture the viewport or scoped
region; inspect the saved image only when DOM/state inspection cannot answer.
HTML snapshots don't contain canvas pixels, closed shadow content or the live
property state of every form control.

## Network and CDP fallback

For a known read response, arm the wait before the UI action and constrain both
URL and method. Write bytes directly to the agent filesystem, retaining source
URL/status/content type in a sidecar; don't return response JSON to conversation.

```python
with page.expect_response(lambda r:
        r.url.endswith('/api/data') and r.request.method == 'GET') as event:
    page.get_by_role('button', name='Load data', exact=True).click()
response = event.value
assert response.ok
(output_dir / 'response.json').write_bytes(response.body())
```

Allocate a unique capture ID and filename **before** calling `response.body()`
in a listener: synchronous Playwright calls can dispatch other callbacks while
waiting, so a mutable list length can change and cause file collisions.
Install listeners before navigation for discovery. Filter resource types and
same-site paths; log bounded metadata (method/status/content type/size), not
cookies, authorization headers or arbitrary request bodies. Retain only the
responses needed for the task. A broad `response.body()` collector can consume
unbounded memory and capture unrelated private data. Service workers or caches
can change interception behavior; recording what actually arrived is stronger
than assuming each click caused a network request.

Prefer Playwright's page APIs. Use page-scoped CDP for a missing primitive and
detach that session afterwards:

```python
cdp = page.context.new_cdp_session(page)
try:
    metrics = cdp.send('Page.getLayoutMetrics')
    viewport = metrics['cssLayoutViewport']
finally:
    cdp.detach()
```

Don't send `Browser.close`, reset contexts, clear storage, change global download
behavior, or navigate an existing personal target as incidental troubleshooting.
CDP attachment is documented as lower fidelity than native Playwright protocol:
a known library recipe still needs an endpoint capability check.

## Verification evidence and sources

The reproducible probe is `scripts/test_operations.py`. It route-fulfills
`https://operations.test/` entirely locally; the only uploaded data are synthetic
bytes to its synthetic input (no submission). It exercises duplicate-name
scoping, forms, confirmation, popup, download failure + verified GET fallback,
byte-payload upload, iframe, open shadow root, response capture, delayed state,
viewport screenshot, page-scoped CDP and navigation, then checks original tabs
remain. On 2026-11-10 at 16:17 UTC it passed in 5.672 seconds. This is an
interaction-mechanism test, not evidence of success on every production site.

Primary documentation was read in an owned Chrome tab, saved to disk and
inspected through bounded local processing:
[locators](https://playwright.dev/python/docs/locators),
[downloads](https://playwright.dev/python/docs/downloads),
[dialogs](https://playwright.dev/python/docs/dialogs), and
[CDP connection](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).
