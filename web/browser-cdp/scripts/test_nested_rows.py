# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4==4.14.3"]
# ///
"""Regression for overlapping rows discovered in legacy government HTML."""
import json
import tempfile
from pathlib import Path
from browser import html_extract

with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    source=root/'nested.html'
    source.write_text('<body><r05>Sec. 1.1 First<r05>Sec. 1.2 Second<r05>Sec. 1.3 Third</body>')
    output=root/'rejected.jsonl'
    try:
        html_extract(source,output,'r05',{'text':{}})
    except ValueError as exc:
        assert 'nested' in str(exc)
    else:
        raise AssertionError('Default extraction accepted overlapping nested rows')
    assert not output.exists(), 'Rejected extraction left a misleading result artifact'
    allowed=root/'allowed.jsonl'
    result=html_extract(source,allowed,'r05',{'text':{}},allow_nested=True)
    rows=[json.loads(line) for line in allowed.read_text().splitlines()]
    assert result['rows']==3 and result['nested_rows']==2
    assert rows[0]['text']=='Sec. 1.1 First Sec. 1.2 Second Sec. 1.3 Third'
    assert rows[-1]['text']=='Sec. 1.3 Third'
    scoped=root/'scoped.jsonl'
    result=html_extract(source,scoped,'r05:not(:has(r05))',{'text':{}})
    assert result['rows']==1 and result['nested_rows']==0
    assert json.loads(scoped.read_text())['text']=='Sec. 1.3 Third'
    print(json.dumps({'nested_default_rejected':True,'rejection_left_no_output':True,'explicit_overlap_count_verified':True,'leaf_scope_verified':True}))
