---
name: prolog
description: "Use the persistent SWI-Prolog MCP tools for bounded symbolic search, rule-based inference, scoped assumptions, counterexample checking, finite solution enumeration with truncation detection, goal-expansion proof trees, and plan verification. Use when a task benefits from explicit facts and rules, recursive relations, comparing assumption sets, checking a generated sequence, or finding witnesses and counterexamples. Covers all prolog_* tools, durable units, scope control, mutation patterns, query conventions, and sharp edges."
---

# Reasoning with the Prolog tools

Use these tools when a problem can be expressed more reliably as explicit
facts, rules, invariants, and goals than as informal chain-of-thought. Prolog is
especially useful for:

- transitive relationships, dependency and reachability analysis;
- policy evaluation with defaults, exceptions, sanctions, and overrides;
- planning, recursive construction, state-transition validation, and search;
- enumerating all solutions or finding a witness/counterexample;
- comparing conclusions under different sets of assumptions;
- checking that a generated schedule, route, sequence, or proof certificate
	satisfies explicit invariants;
- showing the goal-expansion structure of one successful derivation.

Prolog proves consequences of the model supplied to it. It does **not** prove
that the model accurately represents the outside world. State assumptions
explicitly, separate them from derived rules, and independently encode the
properties that matter.

## Mental model

The server has a long-lived knowledge base made of **units**. A unit is a named
group of Prolog facts, rules, and permitted declarations. Units and the ordered
query scope persist across tool calls and may persist across conversations or
server restarts.

Only units in the current **scope** are combined into the program seen by
`prolog_query`, `prolog_prove`, and `prolog_explain`. Units outside scope still
exist and can be inspected or edited. This makes scope a useful assumption
boundary:

- put stable observations in a facts unit;
- put inference logic in a rules unit;
- put optional assumptions, outages, overrides, or competing scenarios in
	separate units;
- load or unload scenario units to compare conclusions without rewriting the
	base model.

Each reasoning call runs in a fresh, isolated SWI-Prolog process. Side effects
inside a goal, such as `assertz/1`, do not update durable units and disappear
after that call. Durable changes must use `prolog_assert`,
`prolog_set_unit_source`, `prolog_retract`, or the unit/scope tools.

## Standard workflow

Follow this sequence unless the task clearly requires less:

1. Call `prolog_list_units` before changing anything. State is persistent; do
	 not assume the knowledge base is empty.
2. Reuse and inspect a relevant unit with `prolog_show_unit`, or create one
	 with a distinctive domain-oriented name and description.
3. Add a coherent group of clauses with `prolog_assert`; use
	 `prolog_set_unit_source` when replacing or substantially restructuring a
	 unit.
4. Run `prolog_check` after a substantial edit. Resolve errors and inspect
	 singleton-variable warnings before trusting conclusions.
5. Establish the exact scope. Prefer `prolog_set_scope` for a reproducible
	 reasoning context; use load/unload for one-at-a-time scenario changes.
6. Use `prolog_query` to explore solutions, `prolog_prove` for a yes/no claim,
	 and `prolog_explain` only when a proof tree is useful.
7. Test important negative cases and counterfactuals, not only the intended
	 positive result.
8. Restore the prior scope when temporarily changing it. Delete disposable QA
	 units; unload reusable task units if they should not affect later work.

Do dependent mutations sequentially. Wait for the response to operation A
before issuing operation B if B must observe A. Calls are mutually excluded by
the default server configuration, but parallel requests have no guaranteed
arrival order.

## Prolog syntax expected by the tools

### Clauses versus goals

- Source passed to `prolog_assert` or `prolog_set_unit_source` contains ordinary
	Prolog clauses, **each ending in a period**.
- A goal passed to query/prove/explain has **no trailing period**.
- Atoms normally begin lowercase: `alice`, `backup_site`.
- Variables begin uppercase or underscore: `Person`, `Cost`, `_Ignored`.
- Variables beginning with `_` are omitted from returned bindings. Use them for
	intentionally ignored values and to keep results compact.
- `_` is a fresh anonymous variable at every occurrence. Use a named variable
	when two positions must be equal.
- Rules use `Head :- Body.`; conjunction is `,`; disjunction is `;`.
- Negation `\+ Goal` is negation as failure under the current scope, not
	classical negation.
- Unification is `=`. Arithmetic evaluation is `is`; numeric comparisons use
	`=:=`, `=\=`, `<`, `=<`, `>`, and `>=`.
- Lists use `[a,b]`, `[Head|Tail]`, and `[]`.

Example source:

```prolog
parent(alice, bob).
parent(bob, carol).

ancestor(X, Y) :- parent(X, Y).
ancestor(X, Y) :- parent(X, Z), ancestor(Z, Y).
```

Example goal: `ancestor(alice, Who)`.

### Result terms are strings

Bindings are returned as canonical Prolog text, for example `"[a,b,c]"`,
`"state([],[],[1,2,3])"`, or `"6"`. Treat them as Prolog terms represented as
strings, not typed JSON arrays or numbers. A named variable that remains
unbound may appear as an implementation-generated name such as `_512`; avoid
requesting irrelevant variables or prefix their names with `_`.

## Tool selection and exact behavior

### Inspect and orient

#### `prolog_list_units`

Always start here for persistent or shared state. It returns every unit, its
description and clause count, whether it is in scope, and the ordered scope.
Do not create a near-duplicate merely because a likely unit is not loaded.

#### `prolog_show_unit`

Returns the complete source and clauses in their current 1-based order. Use it
before rewriting unfamiliar state and after unexpected results. Clause
positions are transient: edits and retractions can change them. Positions and
clause IDs are inspection aids only; `prolog_retract` cannot target either one
directly. Use a sufficiently selective head pattern, or rewrite the unit when
clauses with identical heads must be distinguished.

#### `prolog_find_clauses`

Searches clause **heads**, not rule bodies. It searches all units—including
unloaded units—unless `units` is supplied. Patterns unify with heads:

- `ancestor(_, _)` finds definitions compatible with that shape;
- `ancestor/2` selects by predicate name and arity;
- `parent(alice, _)` selects clauses whose first argument is `alice`.

Use this before editing or retracting definitions whose location is unknown.
No matches is a successful search result, not an error.

### Create and edit durable knowledge

#### `prolog_create_unit`

Creates an empty unit. Names are at most 64 characters and contain letters,
digits, `_`, or `-`. Names must be unique. Use `load: true` only if the new unit
should immediately affect reasoning; otherwise establish scope explicitly.

Choose semantic names such as `network_facts`, `network_rules`, and
`network_outages`, or a clearly disposable name such as `qa_route_2026`.

#### `prolog_assert`

Appends one or more complete clauses. The server validates the entire resulting
unit before committing. Missing periods, syntax errors, and disallowed
directives fail atomically: the unit remains unchanged.

Prefer one assertion containing a coherent set of mutually recursive clauses
over many tiny calls. This is faster, easier to validate, and avoids exposing a
half-built model.

#### `prolog_set_unit_source`

Atomically replaces the entire unit. Use it for a deliberate rewrite,
generated model refresh, or cleanup after experimentation. Read the unit first
unless it is wholly owned by the current task. Empty source clears the unit.

#### `prolog_retract`

Removes clauses whose **heads unify** with `pattern`:

- default `all: false` removes only the first match in source order;
- `all: true` removes all matches;
- `parent(alice, _)` is selective;
- `parent/2` matches every fact or rule defining `parent/2`, but removes all of
	them only with `all: true`; otherwise it removes the first match.

It does not retract a body occurrence. A no-match retraction is an error. For
high-impact patterns, call `prolog_find_clauses` or `prolog_show_unit` first and
inspect the matches. Never issue dependent retracts concurrently.

#### `prolog_delete_unit`

Permanently deletes the unit and removes it from scope. Prefer unloading when
the knowledge may be useful later. Delete only disposable or explicitly
obsolete units.

### Validate

#### `prolog_check`

With `unit`, checks that unit alone. Without `unit`, checks the complete current
scope. It reports whether loading succeeded, diagnostics, and defined
predicates with arity and dynamic status.

Check a unit immediately after editing, then check the full intended scope to
catch cross-unit conflicts. A successful edit can still return warnings; read
them. Singleton-variable warnings often indicate a misspelled variable or a
missing equality.

### Control assumptions

#### `prolog_load_unit`

Appends an existing unit to scope if it is not already present. Loading twice
does not duplicate it.

#### `prolog_unload_unit`

Removes a unit from scope but preserves it. Unloading a unit that is not in
scope is an error.

#### `prolog_set_scope`

Atomically replaces the complete ordered scope. Unknown units cause failure
without changing the old scope; duplicates are removed while preserving first
occurrence order. An empty list clears scope.

Scope order affects clause order and therefore first-solution behavior when a
predicate is defined across units. Use an explicit order for reproducible
results. Save the prior scope from `prolog_list_units` before a temporary
experiment and restore it afterward.

### Reason

#### `prolog_query`

The main exploration tool. It enumerates solutions and returns:

- `succeeded`: whether at least one solution exists;
- `count`: number returned;
- `solutions`: named-variable bindings;
- `truncated`: whether another solution exists beyond the limit;
- `scope`: the exact units used;
- diagnostics and any captured textual output.

Default limit is 25. The normal server hard cap is configurable and commonly
200. Request a deliberate limit within the cap; a nonpositive limit or one
above the configured cap falls back to the default rather than requesting “all
solutions.” Increase `timeoutMs` only when the search is expected to be
expensive. The default is commonly 10 seconds.

Always inspect `truncated`. If it is true, do not claim the returned solutions
are exhaustive. Tighten the goal, aggregate inside Prolog, or rerun with a
larger valid limit. Keep result terms small: instead of returning a huge plan
when only its size matters, ask `length(Moves, Count)` and omit `Moves` by
naming it `_Moves` or by structuring the goal not to expose it.

To set `truncated` accurately, the server probes for one solution beyond the
requested limit. Returning $N$ solutions can therefore require finding
solution $N+1$ or proving that no such solution exists. If that tail is
expensive or nonterminating, the whole query can time out even though the first
$N$ solutions were found internally. Use a more selective goal or
`prolog_prove` when only one witness matters.

Use `setof/3` for sorted unique solutions when appropriate and `findall/3` when
duplicates and an empty list are meaningful. Be careful: returning a large
collected list can consume the agent context even when query `limit` is 1.

#### `prolog_prove`

Use when only existence/truth and one witness matter. It returns `proved` and
the first solution's bindings. It is cheaper and less noisy than enumerating
with `prolog_query`.

A false result means the goal is not derivable from the current closed-world
scope; it does not establish universal real-world falsehood. To prove a
universal property over a finite/generated domain, search for a counterexample
and prove none exists, for example `\+ (candidate(X), \+ valid(X))`, while
ensuring `candidate/1` actually enumerates the intended domain.

#### `prolog_explain`

Use after proving a focused goal when derivation structure or debugging is
valuable. It verifies truth with native Prolog and returns a goal-expansion
tree for one successful derivation. Built-ins and imported predicates are
marked; a shallow tree may contain `% depth limit reached` while still checking
the elided subgoal. The tree does not include unit names, clause IDs, source
positions, or complete provenance. Corroborate important steps with
`prolog_find_clauses` and `prolog_show_unit`.

Proof trees can become much larger than ordinary query results, especially when
a binding is a long list. Prefer a ground or compact goal, reduce `depth`, and
explain a key lemma rather than an entire 127-step plan. An empty proof with
`proved: false` is normal. The explanation is one successful derivation, not a
complete enumeration of all derivations.

Explanation evaluates the goal once with native Prolog and again while building
the tree. Explain only pure, preferably ground goals. Do not use it for goals
that print output, use randomness, mutate process-local predicates, or perform
other side effects; use `prolog_prove` or `prolog_query` instead.

## Modeling patterns that work well

### Separate facts, rules, and scenarios

Keep independent concerns in separate units:

```text
access_facts       people, roles, training
access_policy      derived authorization rules
access_sanctions   optional bans
access_override    temporary emergency assumptions
```

Then compare the same goal under scopes such as:

```text
[access_facts, access_policy]
[access_facts, access_policy, access_sanctions]
[access_facts, access_policy, access_sanctions, access_override]
```

Record the scope with every conclusion. Never compare results from different
scopes as though they arose from the same assumptions.

### Construct, execute, and verify

For plans or sequences, do not trust a generator alone. Encode three layers:

1. **Constructor** — generates a candidate plan recursively.
2. **Transition semantics** — applies each action only when legal.
3. **Verifier** — executes the candidate from the initial state and checks the
	 final state and other invariants.

For example:

```prolog
run_moves([], State, State).
run_moves([Move|Moves], S0, S) :-
		apply_move(Move, S0, S1),
		run_moves(Moves, S1, S).

valid_solution(N, Moves) :-
		initial_state(N, Start),
		run_moves(Moves, Start, Finish),
		solved_state(N, Finish).
```

Then prove the generated plan is accepted by the independent transition model.
Also mutate or truncate the plan and verify that the checker rejects it. These
negative controls catch vacuous or overly permissive verifiers.

### Establish optimality carefully

“A valid plan has length 127” is not by itself a proof that no shorter plan
exists. Encode or derive a lower-bound theorem/recurrence, connect it to the
problem assumptions, and check the candidate meets it. Be explicit when a
recurrence is assumed as a rule rather than derived inside the model.

For recursive constructions, useful independent checks include:

- expected length or cost;
- legal execution from the exact initial state;
- exact final state;
- required landmark actions and their positions;
- rejection of a changed action;
- rejection of a shortened prefix;
- absence of a cheaper/shorter counterexample within a complete search domain.

### Avoid nontermination

Order goals so cheap, selective, and grounding predicates run before recursive
or arithmetic predicates. Add base cases before recursive clauses. Track
visited nodes in graph search. Use `member/2` checks to avoid cycles and an
accumulator for costs. Bound generated domains with predicates such as
`between/3` where possible.

Do not recursively generate an unbounded domain and filter it afterward. A
goal like `nat(N), N < 0` never finds a result and runs until timeout.

### Closed-world optional predicates

With the default sandbox enabled, a predicate named directly in a goal must
exist; a typo such as `parnt(X,Y)` is reported instead of silently failing. A
missing predicate reached only through a loaded rule body is treated as having
no clauses, allowing optional units to implement closed-world assumptions.
Loading a unit that defines the predicate can therefore change the conclusion.
Sandbox-disabled deployments may instead report an unknown-procedure error.

This is useful for rules such as:

```prolog
allowed(P) :- person(P), \+ banned(P).
```

With no in-scope `banned/1` clauses, `banned(P)` fails. Loading a sanctions unit
can withdraw `allowed(P)`. This is scope-sensitive default reasoning, not
classical monotonic logic.

## Sharp edges and safety

### Persistent shared state

- Never assume an empty knowledge base.
- Inspect before editing.
- Use distinctive names and descriptions.
- Prefer unloading to deleting reusable knowledge.
- Restore scope after experiments.
- Do not leave half-finished temporary units loaded.

### Mutations and concurrency

The server normally serializes calls to prevent read-modify-write corruption,
especially in retraction. Serialization does not preserve the order of calls
sent together. Never batch these dependent sequences in parallel:

- create, then assert;
- assert, then check/query;
- find/show, then retract;
- load/unload/set-scope, then reason;
- mutate, then inspect the resulting state.

Independent read-only calls against an unchanged scope are semantically safe to
parallelize, but the default server serializes all tool calls, so doing so
normally gives no speedup and obscures response ordering. Prefer sequential
calls unless the deployment explicitly disables serialization.

### Sandbox and directives

With the default configuration, goals are screened by SWI-Prolog's sandbox.
Filesystem, shell, and other unsafe operations are expected to be rejected. Do
not attempt to bypass the sandbox. Each goal also has a Prolog time limit, a
process-level deadline, and a stack limit; a timed-out or crashed query does not
corrupt later queries.

Directives run while source loads, outside the goal sandbox. The normal server
uses a syntactic directive allowlist, but it is not a security boundary agents
should test or rely on. Submit no directives unless strictly necessary. When a
declaration is needed, use exactly one simple declaration per directive; never
combine it with another goal. Declarations such as `dynamic`, `discontiguous`,
`multifile`, `table`, `op`, `set_prolog_flag`, and `style_check` may be accepted.
Avoid `op`, `set_prolog_flag`, and global style changes unless essential because
they alter how subsequent source in the combined scope is interpreted. Never
submit load-time side effects. Prefer plain facts and rules.

Sandbox acceptance of a side-effecting goal does not make it durable. Query
processes are disposable, and durable state lives only in units.

### Scope and predicate collisions

All in-scope unit sources are loaded into one Prolog module. Same-name,
same-arity predicates across units contribute clauses to one predicate; they
are not namespaced. This can be intentional for scenario facts but can also
cause accidental collisions. Use domain-specific predicate names, inspect the
full scope, and run `prolog_check` after composing units.

### Negation and incompleteness

`\+ p(X)` means Prolog failed to prove `p(X)` under the current scope. It does
not mean an explicit negative fact was proved. Ground variables before
negation when possible. Changing scope can reverse conclusions that rely on
negation as failure.

### Large outputs

Long lists and deep proof trees can overwhelm the conversation even when the
server handles them correctly. Ask for summaries and landmarks first:

```prolog
hanoi(7, a, c, b, _Moves), length(_Moves, Count)
```

Because `_Moves` starts with underscore, it is not returned. Query specific
elements with `nth1/3`, aggregates with `aggregate_all/3` where sandbox support
allows it, or prove properties directly. Request the full object only when the
user actually needs it.

## Error recovery

Treat tool errors as actionable feedback and retry after correction:

- **unknown unit** — list units and correct the name or create it;
- **unit already exists** — inspect and reuse it; do not create a duplicate;
- **missing period / syntax error** — fix source; failed edits are atomic;
- **singleton warning** — inspect variable spelling and intended equality;
- **undefined direct goal** — check spelling, definition location, and scope;
- **no retract match** — find clauses and use a head-compatible pattern;
- **sandbox rejection** — reformulate with safe logical predicates;
- **time limit exceeded** — improve termination and selectivity before merely
	raising `timeoutMs`;
- **truncated query** — narrow the goal, aggregate, or rerun with a larger valid
	limit;
- **huge result** — omit irrelevant named variables and query summaries.

After any unexpected mutation error, use `prolog_show_unit`; source validation
errors should leave the unit unchanged. After an unexpected reasoning result,
confirm the returned scope, inspect all definitions with
`prolog_find_clauses`, run `prolog_check`, and explain a small ground instance.

## Completion checklist

Before reporting a symbolic conclusion:

- [ ] The relevant units and exact scope were inspected.
- [ ] The model distinguishes facts, rules, and optional assumptions.
- [ ] Edited units and the composed scope pass `prolog_check`.
- [ ] Goals omit the trailing period; source clauses include it.
- [ ] `truncated` was checked before claiming enumeration is complete.
- [ ] Important claims were tested with negative cases or counterexamples.
- [ ] Generated plans were executed by an independent legality verifier.
- [ ] “Optimal” claims include a justified lower bound, not only a candidate
			length.
- [ ] Proof explanations are focused and do not flood context.
- [ ] Temporary scope changes were restored and disposable units cleaned up.
- [ ] The report states assumptions, scope, witnesses/counts, and what was
			actually proved.
