# Complex government trees: measured decision patterns

These public-source probes test browser mechanisms and extraction quality. They
are not legal, tax or benefits advice. Raw captures and datasets stay in the
chosen local artifact directory; the skill includes only probe code and public
aggregate evidence. Run only the relevant probe when validating a site, with
site-appropriate pacing and a fresh output directory.

## Prefer a verified aggregate to a crawl

On [GOV.UK Benefits](https://www.gov.uk/browse/benefits), observed links descended
three levels to one guide section. The leaf had 99 global links but only 31 inside
`main`; even `main` included Contents, previous/next and related topics.
`main .gem-c-govspeak` uniquely isolated 23 content blocks including one table.
Thus a landmark is a starting scope, not automatically the canonical body.

The observed **View a printable version of the whole guide** link yielded seven
section bodies/135 semantic blocks in 88 KB, compared with 94 KB/23blocks for the
single leaf. The print headings covered all seven observed Contents entries,
and its table matched the leaf. One validated aggregate capture avoided six
additional sibling navigations. A visible **Next** here was a sibling guide
section, not a data pagination cursor. Classify edges before following them.

Probe/evidence: `scripts/probe_govuk.py`, [govuk-results.json](govuk-results.json).
The initial four-page traversal took 27.528 s with 8 s spacing; the separately
observed print page loaded in 1.025 s. These are measured runs, not speed promises.

## Preserve bytes, then validate data relationships

On [Census datasets](https://www2.census.gov/programs-surveys/popest/datasets/),
observed directory links descended through a release range, geographic level
and totals directory. The directory pages had no useful heading hierarchy;
scoping the listing table removed parent/sort/footer/social links. Preserve
filename, href, size and timestamp as records before deciding which files to
retrieve. Do not recursively follow every link on a directory page.

One observed CSV contained 3,195 rows ×83 columns (265,185 cells), yielding7 MB of
JSONL entirely offline. County rows reconciled to all 51 state-level rows. This
checks both parsing and internal relationships, beyond merely counting lines.

Encoding was the consequential failure: `fetch(...).text()` replaced one legacy
character; CDP `response.body()` expanded the source by one byte through text
transcoding. Browser Fetch bytes were1,769,182bytes; strict CP1252 decoding and
roundtrip preserved them. The supplied `fetch_bytes` helper reproduced the exact
byte hash using chunked transport. Data counts alone would have missed damage.
Treat file type/encoding and source representation as part of provenance.

Probe/evidence: `scripts/probe_census.py`, [census-results.json](census-results.json).
Traversal/export took 33.316 s with 8.1 s spacing. `scripts/test_fetch_bytes.py`
checks binary fidelity, chunking, config errors, byte limits and output-collision
behavior without external requests.

## Dynamic trees need branch-state readiness

On the [Oregon Revised Statutes index](https://www.oregonlegislature.gov/bills_laws/Pages/ORS.aspx),
the Volume→Title→Chapter tree used regenerated SharePoint IDs and lazy content.
Copying an observed control ID into a later visit failed because its numeric
prefix changed. Image alt text (`expand`) also changed the accessible name from
the visible word `Volume`. The working locator used the current row's observed
label, then its unique anchor. Role+guessed name was not reliable here.

Expanded state persisted across visits. Blindly clicking a parent sometimes
**collapsed** it, leaving its child's locator present but invisible. Check the
intended descendant's visibility, expand only if needed, then wait for the
child's actual visible link before reading its href. Resolve literal relative
hrefs against the current page URL. This avoids both stale placeholders and
invented deep URLs.

The observed chapter was 1.22 MB of Word-generated HTML: one h1,3,409 paragraphs,
no semantic section heading tree. Offline discovery identified bold numeric
paragraph prefixes; ordinary numbered paragraphs also occurred in the Contents
and were unsuitable boundaries. Partitioning the paragraphs preserved 208 section
occurrences plus421 preamble paragraphs. Only207 section numbers were distinct:
a repeated section had an additional future-operative version. Deduplicating
by displayed section number would silently lose source material. Preserve an
occurrence/version key and surrounding effective-date notes. Validate that the
ordered partition covers every selected paragraph exactly once.

Probe: `scripts/probe_statutes.py`; evidence [statutes-results.json](statutes-results.json).
The complete scope is the selected chapter's paragraph sequence, not the whole
statute collection and not a legal-currentness assertion.

## Malformed legacy HTML can multiply data

[GovInfo](https://www.govinfo.gov/browse-a-z) exercised a dynamic browse tree:
collection→year→title→volume→available renditions. The initial app shell was
small; meaningful content arrived after hydration and branch expansion. Wait
for the requested branch's rows/links, not just `DOMContentLoaded` or an arbitrary
landmark (the meaningful container did not use `main`). Use targeted maps when
navigation/search scaffolding buries the content in the first global headings.

A574 KB full-text rendition contained 294 nested, unclosed custom `<R05>` tags.
Calling recursive `get_text()` for every matching row would emit111 million
characters, about 194times the source. The helper now rejects nested matched
rows by default, before opening the output; `--allow-nested` permits deliberate
hierarchical extraction. Inspect direct text children/sibling boundaries or use
a structured rendition rather than recursively duplicating every descendant.

Flat section-number regexes also matched Contents and prose examples. Node-local
HTML parsing found 274 unique candidate section IDs, while the observed source XML
contained 288including14reserved ranges. HTML alone was therefore partial.
The independent XML inventory explains the difference; a larger regex count
would have hidden it. Compare identifiers and classifications across renditions,
not just totals.

Navigating to a `.xml` URL yielded rendered XHTML through Chrome's XSLT display;
that document's tag vocabulary was HTML, not the expected source schema. A
normal same-origin Fetch of the observed URL produced the actual XML. Check
root element/namespace and expected records after a format choice; an empty
schema query is a diagnostic, not proof of zero source records. A suffix or MIME
type is insufficient to establish which representation was captured.

Probe/evidence: `scripts/probe_govinfo.py`, [govinfo-results.json](govinfo-results.json).
`test_nested_rows.py` reproduces the overlapping-row guard offline.

## Transfer the lessons, not selectors

Scout official aggregate/structured views; classify navigation edges; wait for
specific branches; inspect actual accessible names; detect overlapping row
scopes; preserve bytes and source versions; reconcile independent structures.
Keep selectors tied to observed captures and fail clearly when a site changes.
Do not run a whole reference benchmark to answer an ordinary research question.
