# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4==4.14.3"]
# ///
"""Offline scope regression: canonical body excludes noisy navigation and sidebar."""
import json
from pathlib import Path
import tempfile
from browser import html_map

with tempfile.TemporaryDirectory() as directory:
    source=Path(directory)/'page.html'
    source.write_text('''<html><head><title>Scope fixture</title></head><body>
<nav><h2>Misleading title</h2><a href="/decoy">Decoy</a></nav>
<main><article id="body"><h1>Canonical content</h1>
<section><h2>Dataset</h2><a href="/record/1">One</a><a href="/record/2">Two</a></section></article>
<aside><h2>Related content</h2><a href="/related">Related</a></aside></main>
<footer><h2>Misleading title</h2><a href="/decoy">Decoy</a></footer></body></html>''',encoding='utf-8')
    global_map=html_map(source)
    scoped=html_map(source,scope_selector='#body')
    assert global_map['links']==5 and scoped['links']==2
    assert scoped['scope']=='#body'
    assert [heading['text'] for heading in scoped['headings']]==['Canonical content','Dataset']
    assert dict(scoped['tags'])['a']==2
    for selector in ('.missing','h2'):
        try:html_map(source,scope_selector=selector)
        except ValueError as exc:assert 'match one region' in str(exc)
        else:raise AssertionError('Ambiguous or absent region was accepted')
    print(json.dumps({'scoped_counts_verified':True,'navigation_sidebar_excluded':True,'ambiguous_missing_scope_rejected':True}))
