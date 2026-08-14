---
name: mcp-go
description: Build MCP (Model Context Protocol) servers in Go with the official modelcontextprotocol/go-sdk. Use when creating, extending, or debugging an MCP server or client in Go — defining tools, schemas, transports, error handling, testing, or wrapping an external program as a tool server.
---

# Building MCP servers in Go

## Use the official SDK

Use `github.com/modelcontextprotocol/go-sdk`, pinned to a `v1.x` release.

```sh
go get github.com/modelcontextprotocol/go-sdk@v1.7.0
```

Why this one, and not `mark3labs/mcp-go` (which has more GitHub stars):

- The official SDK is on `v1`, and its `design/design.md` commits to it:
  *"Subsequent to that release, new APIs will be added in minor versions, and
  breaking changes will require a v2 release of the module."* Comparing the
  exported API of `./mcp` between `v1.0.0` and `v1.7.0` shows ~90 additions and
  no genuine removals.
- `mark3labs/mcp-go` is still pre-1.0 (`v0.58.0`, `v1.0.0-beta.1`) after 88
  releases, so Go's module system provides no compatibility guarantee.
- `metoro-io/mcp-golang` is unmaintained; `ThinkInAIXYZ/go-mcp` is at `v0.2.x`
  with little activity.

Code written against the official SDK will not need rewriting.

Packages: `mcp` (servers and clients), `jsonrpc` (transport authors and error
codes), `auth` and `oauthex` (OAuth). Feature documentation lives in `docs/` in
the repository.

## Minimal server

```go
package main

import (
	"context"
	"log"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type GreetIn struct {
	Name string `json:"name" jsonschema:"who to greet"`
}

type GreetOut struct {
	Greeting string `json:"greeting" jsonschema:"the greeting"`
}

func greet(ctx context.Context, req *mcp.CallToolRequest, in GreetIn) (*mcp.CallToolResult, GreetOut, error) {
	return nil, GreetOut{Greeting: "Hi " + in.Name}, nil
}

func main() {
	server := mcp.NewServer(&mcp.Implementation{Name: "greeter", Version: "v1.0.0"}, nil)
	mcp.AddTool(server, &mcp.Tool{Name: "greet", Description: "Greet someone."}, greet)
	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}
```

## Defining tools

Always use the package-level generic `mcp.AddTool[In, Out]`, not the
`(*Server).AddTool` method. The generic form derives both JSON schemas from the
type parameters, validates and defaults arguments before the handler runs,
populates `StructuredContent` from the output value, and converts a returned
error into a *tool* error. `(*Server).AddTool` is the raw escape hatch for when
schemas come from elsewhere; it does none of that.

`mcp.AddTool` **panics** on a bad tool definition. Register every tool during
startup so the panic happens immediately rather than mid-session.

### Schema inference rules

Schemas come from `github.com/google/jsonschema-go`. The rules that actually
matter:

- A field is **required unless** its JSON tag has `omitempty` or `omitzero`.
  This is the single most common mistake: every optional parameter needs
  `json:"limit,omitempty"`.
- The `jsonschema:"..."` struct tag supplies the property **description**. Write
  one for every field; it is the model's only guidance.
- Structs become objects with `additionalProperties: false`.
- Use `struct{}` as `In` for a no-argument tool. `any` also works but is less
  self-documenting.
- Set `Tool.InputSchema` / `Tool.OutputSchema` explicitly to override inference;
  `jsonschema.For[T](&jsonschema.ForOptions{TypeSchemas: ...})` handles custom
  types with their own marshalling.

```go
type QueryIn struct {
	Goal      string `json:"goal" jsonschema:"a Prolog goal, without the trailing '.'"`
	Limit     int    `json:"limit,omitempty" jsonschema:"maximum solutions to return (default 25)"`
	TimeoutMs int    `json:"timeoutMs,omitempty" jsonschema:"give up after this many milliseconds"`
}
```

produces `"required": ["goal"]`.

### Descriptions are the interface

Tool and parameter descriptions are the API contract with the model. Say what
the tool is *for* and when to reach for it, not just what it does. Put
cross-cutting guidance — the intended workflow, conventions, the fact that state
persists — in `ServerOptions.Instructions`, which is delivered once at
initialization instead of being repeated in every description.

### Annotations

```go
&mcp.ToolAnnotations{ReadOnlyHint: true, IdempotentHint: true, OpenWorldHint: ptr(false)}
&mcp.ToolAnnotations{DestructiveHint: ptr(true), OpenWorldHint: ptr(false)}
```

`DestructiveHint` and `OpenWorldHint` are `*bool` because their spec defaults
are `true`; `ReadOnlyHint` and `IdempotentHint` are plain `bool` defaulting to
`false`. Clients use these to decide what needs confirmation, so getting them
right is a usability and safety feature.

### Tool names

`[a-zA-Z0-9_.-]` only, at most 128 characters. Prefix them with the server's
domain (`prolog_query`, not `query`) so they stay unambiguous when a client has
several servers connected.

## Errors: tool errors vs protocol errors

This distinction is the difference between an agent that recovers and one that
gives up.

- **Return a plain `error`** for anything the model could fix — bad input, a
  goal that does not parse, a missing resource. The SDK sets
  `CallToolResult.IsError` and puts the message in `Content`, so the model sees
  it and can retry.
- **Return a `*jsonrpc.Error`** only for genuine protocol faults. These are
  invisible to the model.
- Argument validation failures are already turned into tool errors by the SDK.

Make error messages instructive: state what was rejected *and* what is allowed.

```go
return nil, editOut{}, fmt.Errorf(
	"directive not permitted: %s\nPermitted directives are: dynamic, discontiguous, multifile, table, op", d)
```

## Results

The handler returns `(*mcp.CallToolResult, Out, error)`:

- Returning `nil` for the result is fine; the SDK fills `Content` with the JSON
  encoding of `Out`.
- Returning an explicit result with `TextContent` gives the model a compact,
  readable rendering while `StructuredContent` keeps the machine-readable form.
  Do this whenever the JSON is bulky or awkward to read — a table of query
  solutions, a proof tree, a diff.

```go
return &mcp.CallToolResult{
	Content: []mcp.Content{&mcp.TextContent{Text: humanReadable}},
}, structured, nil
```

Never return an empty `Content` and no output; some clients render nothing.

## Transports

### stdio

```go
server.Run(ctx, &mcp.StdioTransport{})
```

- **Nothing may ever be written to stdout** except the JSON-RPC stream. Send all
  logging and diagnostics to stderr. A single stray `fmt.Println` corrupts the
  session.
- `Server.Run` returns an error wrapping the SDK's `ErrServerClosing`
  (JSON-RPC code `-32004`) at the end of *every* normal session, because a
  client closing stdin is indistinguishable from the peer going away. Treat it
  as a clean exit or the server exits non-zero every single time:

```go
const codeServerClosing = -32004 // jsonrpc2.ErrServerClosing; not exported

func isCleanShutdown(err error) bool {
	if err == nil || errors.Is(err, io.EOF) || errors.Is(err, context.Canceled) {
		return true
	}
	var wireErr *jsonrpc.Error
	return errors.As(err, &wireErr) && wireErr.Code == codeServerClosing
}
```

- Do **not** set `ServerOptions.KeepAlive` on stdio. Keepalive exists to detect
  peers that vanish without closing the connection, which cannot happen over a
  pipe; an in-flight ping racing with EOF only muddies shutdown.

### Streamable HTTP

```go
handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server {
	return server // or build a per-session server from the request
}, nil)
http.ListenAndServe(addr, handler)
```

It is an ordinary `http.Handler`, so it mounts in any `net/http` router. Set
`KeepAlive` here, and `http.Server.ReadHeaderTimeout`. The SDK applies DNS
rebinding protection for loopback listeners by default; CORS is off unless
configured.

## Concurrency: the SDK runs your tools in parallel

**This is the single easiest way to ship a broken MCP server.** The SDK calls
`jsonrpc2.Async(ctx)` for every request except `initialize`, so a client that
issues several tool calls at once gets them executed **concurrently**. Nothing
in the API hints at this, and agents batch calls constantly.

Two distinct problems follow, and they need different fixes.

### 1. No mutual exclusion — this is yours to fix

Any tool that reads state, decides something, and then writes it back is a
read-modify-write. Two of them running at once corrupt each other, even when
every individual state accessor is mutex-protected: the lock protects each step,
not the sequence.

A concrete measurement from the reference server, whose `retract` tool asks the
backend which clause *positions* match and then deletes those positions. Ten
facts, five concurrent retracts of distinct patterns, run five times:

- serialised: correct every time
- unserialised: **wrong clauses deleted in 5 runs out of 5**

Positions computed against one version of the data were applied to another.
Fine-grained locking inside the store did not help, because the window was
*between* the two locked sections.

Serialise tool calls with receiving middleware:

```go
func serializeToolCalls() mcp.Middleware {
	var mu sync.Mutex
	return func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
			if method != "tools/call" {
				return next(ctx, method, req) // keep tools/list, ping, ... concurrent
			}
			mu.Lock()
			defer mu.Unlock()
			if err := ctx.Err(); err != nil {
				return nil, err // the caller already gave up
			}
			return next(ctx, method, req)
		}
	}
}

server.AddReceivingMiddleware(serializeToolCalls())
```

Serialise only `tools/call`, so a long-running tool does not stall `tools/list`
or `ping` and make the server look hung. Make it a flag: a genuinely stateless
server should stay parallel, and a server with one slow tool may prefer
finer-grained locking (per-resource keyed mutexes) over one global lock.

### 2. No ordering guarantee — this one you cannot fix

Middleware **cannot** restore arrival order. The SDK releases each request for
concurrent execution *before* any middleware runs, so by the time your lock is
contended the original order is already lost. A mutex gives you mutual
exclusion, never sequencing.

So if a client sends `delete X` and `list` together, `list` may legitimately run
first. That is not a bug to fix server-side — it is what "concurrent" means. A
client that needs B to observe A's effect must wait for A's response before
sending B.

Design tools so this matters less: make each one self-sufficient rather than a
step in a required sequence, and have mutating tools **return the resulting
state** so the caller never needs a follow-up read. The reference server's
scope-changing tools all return the new scope for exactly this reason.

### Consequences for testing

Deliberately fire a batch of concurrent calls in a test and assert the final
state is correct. Sequential tests will never catch this class of bug. The
reference server's serialisation test failed 5/5 with the middleware removed and
passes reliably with it.

Piping a batch of JSON-RPC messages into a stdio server also proves nothing
about ordering — send one request and wait for its response.

### Cancellation

Respect `ctx`: it is cancelled when the client cancels the request or the
session ends. Pass it into every subprocess, HTTP call and query. When tool
calls are serialised, re-check `ctx.Err()` after acquiring the lock — a request
that waited behind a slow one may already have been abandoned.

## Testing

Connect a real client to a real server over an in-memory transport. This
exercises schema generation, argument validation and marshalling — the parts
most likely to be wrong — without spawning a process.

```go
ctx := context.Background()
st, ct := mcp.NewInMemoryTransports()
server := mcp.NewServer(&mcp.Implementation{Name: "s", Version: "test"}, nil)
registerTools(server)
if _, err := server.Connect(ctx, st, nil); err != nil {
	t.Fatal(err)
}
cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "test"}, nil).
	Connect(ctx, ct, nil)
```

Then call tools as a client does, and decode `res.StructuredContent`:

```go
res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: "prolog_query", Arguments: map[string]any{"goal": "p(X)"}})
// err       -> protocol error
// res.IsError -> tool error; the message is in res.Content
raw, _ := json.Marshal(res.StructuredContent)
json.Unmarshal(raw, &out)
```

Worth asserting explicitly:

- Every tool has a description and an input schema (`cs.Tools(ctx, nil)` is an
  iterator over the live list).
- Failures are tool errors, not protocol errors, and the message is actionable.
- Failed mutations leave state unchanged.

## Wrapping an external program

Many useful MCP servers are a wrapper around a CLI. What this costs in
performance it repays in isolation.

**Run a fresh process per request** unless you have measured that you cannot.
A short-lived subprocess makes each request hermetic: a crash, a runaway loop or
a corrupted in-memory state cannot leak into the next one. Keep durable state in
Go and regenerate the subprocess's input each time.

**Never build a shell command from user input.** Write inputs to files in a
per-request temporary directory and pass file paths plus fixed scalar arguments.
Use `exec.CommandContext`, not `sh -c`.

```go
dir, _ := os.MkdirTemp("", "req-")
defer os.RemoveAll(dir)
os.WriteFile(filepath.Join(dir, "input.txt"), []byte(userInput), 0o600)

cmd := exec.CommandContext(ctx, tool, "--input", filepath.Join(dir, "input.txt"))
cmd.Dir = dir
cmd.Env = []string{"LANG=C.UTF-8", "HOME=" + dir} // do not inherit the parent env
cmd.WaitDelay = 2 * time.Second
```

**Bound everything, twice.** An internal time limit gives a clean, explainable
error; a process-level deadline is what actually saves you when the internal one
cannot fire. Cap memory or stack where the tool supports it, and cap result
sizes so one query cannot flood the model's context.

**Frame the output deliberately.** Have the child emit one machine-readable
record and parse the *last* non-empty line — child programs and their libraries
write to stdout at inconvenient moments. Capture the child's own diagnostics and
return them as structured data rather than letting them pollute the stream.

**Embed helper assets** with `go:embed` and materialise them to a temp file at
startup, so the binary stays self-contained:

```go
//go:embed driver.pl
var driverSource []byte
```

**Pin the runtime in a container.** Distro packages are frequently subsets: the
Debian `swi-prolog-core` package silently omits `library(time)` and
`library(http/json)`. A multi-stage Dockerfile with a `dev` stage (runtime image
+ Go toolchain, for tests against the real binary), a `build` stage, and a
minimal `runtime` stage removes a whole class of "works here" failures.

## Security checklist

- Treat all tool arguments as untrusted. Validate names used in paths;
  reject anything that is not a plain identifier.
- No shell. No string-interpolated commands. Files and argv only.
- Drop the environment for child processes and run as a non-root user.
- Enforce timeouts, memory limits and output caps on every request.
- If the wrapped tool has a sandbox, use it — and check whether it covers
  *everything*. Directives, macros, load-time hooks and plugin loading often run
  before a goal sandbox is consulted and need separate filtering.
- Write persistent state atomically (temp file plus rename) with `0o600`.
- Mark destructive tools with `DestructiveHint` so clients can prompt.

## Reference implementation

`example/` is a complete, tested MCP server that wraps SWI-Prolog and exposes 15
logic tools. It demonstrates every point above: typed tools with inferred
schemas, tool-vs-protocol errors, dual text/structured results, stdio and HTTP
transports, concurrency-safe state, subprocess isolation, embedded assets,
sandboxing, and end-to-end tests over in-memory transports.
