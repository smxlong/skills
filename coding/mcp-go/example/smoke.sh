#!/bin/sh
# Smoke-test the built image over real stdio JSON-RPC.
#
# Requests are sent one at a time, waiting for each response, because the SDK
# handles every call except "initialize" asynchronously: piping a whole batch at
# once gives no ordering guarantee between tool calls.
set -eu

IMAGE="${1:-prolog-mcp}"

exec 3> >(docker run --rm -i "$IMAGE" -state /tmp/kb.json 2>/dev/null > /tmp/prolog-mcp-smoke.jsonl)

send() {
	printf '%s\n' "$1" >&3
	sleep "${2:-1}"
}

send '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
send '{"jsonrpc":"2.0","method":"notifications/initialized"}'
send '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"prolog_create_unit","arguments":{"name":"demo","load":true}}}'
send '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"prolog_assert","arguments":{"unit":"demo","clauses":"likes(mary, wine).\nlikes(john, X) :- likes(mary, X)."}}}'
send '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"prolog_query","arguments":{"goal":"likes(Who, wine)"}}}'
send '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"prolog_explain","arguments":{"goal":"likes(john, wine)"}}}' 2

exec 3>&-
sleep 1

python3 - <<'PY'
import json
for line in open("/tmp/prolog-mcp-smoke.jsonl"):
    msg = json.loads(line)
    result = msg.get("result", {})
    if "content" not in result:
        continue
    print(f"--- id {msg.get('id')} (isError={result.get('isError', False)}) ---")
    for c in result["content"]:
        print(c.get("text", ""))
PY
