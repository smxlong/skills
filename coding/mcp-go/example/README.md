# prolog-mcp

An MCP server that exposes SWI-Prolog to an agent as a set of logic tools. It is
the worked example for the [mcp-go skill](../SKILL.md), so the code is commented
with the reasoning behind each decision.

The agent builds a persistent knowledge base of Prolog facts and rules,
organised into **units**. Only units that are **in scope** are visible to a
query, so the agent can reason under different sets of assumptions by changing
what is loaded.

## Tools

| Tool | Purpose |
| --- | --- |
| `prolog_list_units` | List units and the current scope |
| `prolog_create_unit` | Create a named unit |
| `prolog_delete_unit` | Delete a unit |
| `prolog_show_unit` | Show a unit's source with clause positions |
| `prolog_set_unit_source` | Rewrite a unit wholesale |
| `prolog_assert` | Add clauses to a unit |
| `prolog_retract` | Remove clauses whose head unifies with a pattern |
| `prolog_find_clauses` | Search all units for clauses matching a pattern |
| `prolog_check` | Load and report syntax errors, warnings and predicates |
| `prolog_load_unit` | Bring a unit into query scope |
| `prolog_unload_unit` | Take a unit out of query scope |
| `prolog_set_scope` | Replace the whole scope at once |
| `prolog_query` | Enumerate solutions with variable bindings |
| `prolog_prove` | Yes/no plus the first solution's bindings |
| `prolog_explain` | Return a proof tree for a goal |

## Running

```sh
docker build -t prolog-mcp .
docker run --rm -i -v prolog-state:/home/prolog/state prolog-mcp
```

Client configuration (stdio):

```json
{
  "servers": {
    "prolog": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-v", "prolog-state:/home/prolog/state", "prolog-mcp"]
    }
  }
}
```

Streamable HTTP instead of stdio:

```sh
docker run --rm -p 8080:8080 prolog-mcp -http :8080
```

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `-http` | *(stdio)* | Serve Streamable HTTP on this address |
| `-state` | `prolog-kb.json` | Knowledge base file; empty keeps state in memory |
| `-swipl` | `swipl` | Path to the SWI-Prolog executable |
| `-timeout` | `10s` | Default time limit for a goal |
| `-stack-limit` | `512m` | SWI-Prolog stack limit per query process |
| `-max-solutions` | `200` | Hard cap on solutions per query |
| `-sandbox` | `true` | Reject goals `library(sandbox)` considers unsafe |
| `-allow-directives` | `false` | Permit arbitrary `:- Goal.` directives in unit source |
| `-serialize` | `true` | Run tool calls one at a time |

## Development

The tests run against a real `swipl`, so use the dev image:

```sh
docker build --target dev -t prolog-mcp-dev .
docker run --rm -v "$PWD:/src" -w /src prolog-mcp-dev go test ./...
```

`smoke.sh` drives the built image over real stdio JSON-RPC.

## Design

`driver.pl` is embedded in the binary with `go:embed` and executed as a
short-lived `swipl` subprocess per request. A fresh process per query is slower
than a persistent toplevel, but it makes each query hermetic: a goal that
corrupts the database, exhausts the stack, or wedges the engine cannot affect
the next one. Durable state lives in Go, not in the Prolog process.

Inputs reach `swipl` as files and plain argv scalars, never as interpolated
command text, so agent-supplied Prolog cannot escape into a shell. Goals are
additionally screened by `library(sandbox)` and bounded by both an in-Prolog
time limit and a process-level deadline.

Directives (`:- Goal.`) are filtered separately from goals, because they run at
load time before `library(sandbox)` sees anything; only declarations such as
`dynamic` and `discontiguous` are permitted by default.

## Concurrency

Tool calls are serialised by default (`-serialize`). The MCP SDK executes every
request except `initialize` concurrently, and several tools here are
read-modify-write: `prolog_retract`, for instance, asks SWI-Prolog which clause
*positions* match and then deletes those positions. Overlapping calls apply
positions computed against a different version of the unit and delete the wrong
clauses — reproducibly, not occasionally.

Serialisation guarantees mutual exclusion, not arrival order. The SDK releases
each request for concurrent execution before middleware runs, so a client that
needs one call to observe another's effect must still wait for the first
response before sending the second. Mutating tools return the resulting state
(`scope`, `remaining`, the written clauses) so that a follow-up read is usually
unnecessary.

## Undefined predicates

A rule may refer to a predicate defined in a unit that is not currently in
scope. Such a reference simply fails, in keeping with the closed-world
assumption, rather than raising an error — so loading or unloading a unit
changes conclusions instead of breaking queries.

A predicate named *directly* in a goal must still exist, so typos are reported
rather than silently failing. `library(sandbox)` distinguishes the two cases by
reporting the chain of goals through which it reached the missing predicate.
