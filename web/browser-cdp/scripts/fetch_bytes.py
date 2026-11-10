"""Byte-preserving same-origin GET through an owned page; no cookie export."""
import base64
from pathlib import Path
from browser import metadata, public_url, stamp


def fetch_bytes(page, url, output, max_bytes=64*1024*1024, chunk_size=48*1024):
    """Save browser ArrayBuffer in bounded CDP chunks. Buffers max_bytes in Chrome.

    The URL must already be inspected and authorized. No POST support. CORS/CSP
    and normal browser session rules apply. Does not change browser settings.
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError('chunk_size must be a positive integer')
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError('max_bytes must be a nonnegative integer')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as out:
        handle = page.evaluate_handle('''async ({url,maxBytes}) => {
            const response = await fetch(url, {credentials:'same-origin'});
            if (!response.ok) throw Error(`HTTP ${response.status}`);
            const declared = Number(response.headers.get('content-length'));
            if (declared && declared>maxBytes) throw Error('Response exceeds byte limit');
            // Read incrementally to enforce the limit even without Content-Length.
            const reader=response.body.getReader(), chunks=[];
            let total=0;
            try {
                while (true) {
                    const {value,done}=await reader.read(); if(done) break;
                    total+=value.byteLength;
                    if(total>maxBytes) {await reader.cancel();throw Error('Response exceeds byte limit');}
                    chunks.push(value);
                }
            } finally {reader.releaseLock();}
            const bytes=new Uint8Array(total);let offset=0;
            for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
            return {bytes,url:response.url,status:response.status,
                    content_type:response.headers.get('content-type')};
        }''', dict(url=url,maxBytes=max_bytes))
        try:
            info = handle.evaluate('(v)=>({url:v.url,status:v.status,content_type:v.content_type,size:v.bytes.length})')
            for start in range(0, info['size'], chunk_size):
                encoded = handle.evaluate('''(v,{start,n})=>{
                    let text='';for(const b of v.bytes.subarray(start,start+n))text+=String.fromCharCode(b);
                    return btoa(text);
                }''', dict(start=start,n=chunk_size))
                out.write(base64.b64decode(encoded, validate=True))
            if out.tell() != info['size']:
                raise IOError('Transferred byte count does not match source size')
        finally:
            handle.dispose()
    return metadata(output, source_url=public_url(info['url']), status=info['status'],
                    content_type=info['content_type'], observed_at=stamp(), representation='fetch-response-bytes')
