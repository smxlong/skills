# Behavioral validation and limits

Run the skill's probes only when developing or checking browser compatibility.
They attach to an existing Chrome endpoint; they do not launch a new browser or
install browser binaries. Use a fresh output directory for each run.

```bash
uv run --with playwright==1.63.0 --with beautifulsoup4==4.14.3 python "$SKILL/scripts/test_browser.py" --output artifacts/extraction-run
uv run --with playwright==1.63.0 python "$SKILL/scripts/test_operations.py" --output artifacts/operations-run
uv run --with playwright==1.63.0 --with beautifulsoup4==4.14.3 python "$SKILL/scripts/test_forward.py" --output artifacts/forward-run
```

Run the synthetic probes sequentially: the strict lifecycle tests compare the
whole tab set and would misattribute another probe's concurrent tabs. Fixtures
are route-fulfilled locally. Generated HTML/JSONL/screenshots belong in the
chosen local artifact directory, not in the skill distribution.

## Measured synthetic results

| Challenge | Observed result |
|---|---|
| Large noisy HTML | 3,759,228 bytes; 12,000 exact unique records; sidebar decoy excluded |
| Overlapping pagination | 11 pages, 1,100 observations, 1,000 unique IDs |
| Recycled virtual DOM | 20 mounted nodes; 240 records recovered across 12 windows |
| Offline HTML + actual JSONL helper | 11.954 s total; 5,750 stdout bytes; capture hashes verified |
| Hostile attributes | ASCII and four-byte Unicode IDs/classes bounded; Unicode summary 8,084 bytes |
| Operations | 5.672 s; scoped form, dialog, popup, upload, iframe, shadow, response, delayed state, screenshot/CDP, navigation |
| Independent forward test | 5.483 s; exact extraction, strict ambiguity, error recovery, collision prevention, popup cleanup, URL redaction |

[Extraction evidence](extraction-results.json), [operations evidence](operations-results.json) and
[independent forward evidence](forward-results.json) contain safe aggregate
metrics. Timings are observations from one endpoint, not performance guarantees.
The [government tree probes](government-trees.md) exercise different structures; synthetic
success alone does not establish production coverage.

## Fixes driven by counter-challenges

- Bounding item count alone leaked huge attribute strings; bound strings inside
  browser projections and the final UTF-8 JSON summary.
- An output collision initially happened after evaluated JavaScript ran; reserve
  the output file first so that collision cannot trigger page effects.
- Popups from owned pages need ownership tracking too, not just explicit new tabs.
- Query strings/fragments/userinfo belong in full on-disk provenance, while
  console URL observations omit them. This does not detect secrets in arbitrary
  page text or path components.
- A download event on a borrowed CDP context did not imply successful `save_as`;
  the endpoint returned `canceled`. The probe verified a known authorized GET
  export fallback and did not change browser-wide download policy.

## Scope

The virtual-list fixture advances a recycled window through a button. It tests
ID/progress/end-state logic, not every wheel/scroll implementation. Only open
shadow DOM and same-origin synthetic frames were exercised in the synthetic
suite. Closed roots, browser-native authentication, real payment/submission
flows, giant binary downloads and production CAPTCHA handling are not validated.
No benchmark authorizes external account operations.

The public-government probes additionally cover byte/encoding fidelity, official
print aggregates, lazy SharePoint/React trees, malformed nested HTML, XML/XSLT
representations and preservation of duplicate effective versions. See their
linked evidence for scoped completeness and measured timings.

Offline `test_scoped_map.py` verifies unique region selection and excludes
nav/sidebar data; `test_nested_rows.py` verifies rejection before output creation.
Use each script's inline uv dependency runner. `test_fetch_bytes.py` checks limits,
configuration validation, exact binary transfer and output reservation without
external requests.
