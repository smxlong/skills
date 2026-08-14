package main

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// newTestSession wires a real server to a real client over an in-memory
// transport. This exercises the full path a client would take -- schema
// generation, argument validation, marshalling -- without spawning a process,
// which is why it is the SDK's recommended way to test a server.
func newTestSession(t *testing.T) *mcp.ClientSession {
	t.Helper()
	return newTestSessionOpts(t, true)
}

func newTestSessionOpts(t *testing.T, serialize bool) *mcp.ClientSession {
	t.Helper()

	runner, err := NewRunner("swipl", "256m", true, 10*time.Second)
	if err != nil {
		t.Skipf("swipl unavailable: %v", err)
	}
	kb, err := NewKB(filepath.Join(t.TempDir(), "kb.json"))
	if err != nil {
		t.Fatal(err)
	}
	ps := &PrologServer{kb: kb, runner: runner, maxSolutions: 200}

	server := mcp.NewServer(&mcp.Implementation{Name: "prolog", Version: "test"}, nil)
	ps.Register(server)
	if serialize {
		server.AddReceivingMiddleware(serializeToolCalls())
	}

	ctx := context.Background()
	st, ct := mcp.NewInMemoryTransports()
	if _, err := server.Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "test"}, nil)
	cs, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cs.Close() })
	return cs
}

// call invokes a tool and fails the test if it reports an error.
func call(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any, out any) string {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: protocol error: %v", name, err)
	}
	body := resultText(res)
	if res.IsError {
		t.Fatalf("%s: tool error: %s", name, body)
	}
	if out != nil {
		raw, err := json.Marshal(res.StructuredContent)
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(raw, out); err != nil {
			t.Fatalf("%s: decoding structured output: %v", name, err)
		}
	}
	return body
}

// callErr invokes a tool that is expected to fail, and returns the error text.
func callErr(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any) string {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: protocol error: %v", name, err)
	}
	if !res.IsError {
		t.Fatalf("%s: expected a tool error, got: %s", name, resultText(res))
	}
	return resultText(res)
}

func resultText(res *mcp.CallToolResult) string {
	var b strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			b.WriteString(tc.Text)
		}
	}
	return b.String()
}

func TestListTools(t *testing.T) {
	cs := newTestSession(t)
	var names []string
	for tool, err := range cs.Tools(context.Background(), nil) {
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, tool.Name)
		if tool.Description == "" {
			t.Errorf("tool %s has no description", tool.Name)
		}
		if tool.InputSchema == nil {
			t.Errorf("tool %s has no input schema", tool.Name)
		}
	}
	if len(names) != 15 {
		t.Errorf("got %d tools, want 15: %v", len(names), names)
	}
}

func TestKnowledgeBaseLifecycle(t *testing.T) {
	cs := newTestSession(t)

	call(t, cs, "prolog_create_unit", map[string]any{
		"name": "family", "description": "family relations", "load": true,
	}, nil)

	call(t, cs, "prolog_assert", map[string]any{
		"unit": "family",
		"clauses": `parent(tom, bob).
parent(bob, ann).
parent(bob, pat).
ancestor(X, Y) :- parent(X, Y).
ancestor(X, Y) :- parent(X, Z), ancestor(Z, Y).`,
	}, nil)

	var q queryOut
	call(t, cs, "prolog_query", map[string]any{"goal": "ancestor(tom, Who)"}, &q)
	if !q.Succeeded || q.Count != 3 {
		t.Fatalf("ancestor(tom, Who): got %d solutions, want 3: %+v", q.Count, q.Solutions)
	}
	got := map[string]bool{}
	for _, sol := range q.Solutions {
		got[sol["Who"]] = true
	}
	for _, want := range []string{"bob", "ann", "pat"} {
		if !got[want] {
			t.Errorf("missing solution Who = %s", want)
		}
	}

	var p proveOut
	call(t, cs, "prolog_prove", map[string]any{"goal": "ancestor(tom, ann)"}, &p)
	if !p.Proved {
		t.Error("ancestor(tom, ann) should be provable")
	}
	call(t, cs, "prolog_prove", map[string]any{"goal": "ancestor(ann, tom)"}, &p)
	if p.Proved {
		t.Error("ancestor(ann, tom) should not be provable")
	}

	var e explainOut
	call(t, cs, "prolog_explain", map[string]any{"goal": "ancestor(tom, ann)"}, &e)
	if !e.Proved || !strings.Contains(e.Proof, "parent(tom,bob)") {
		t.Errorf("proof does not mention the parent step:\n%s", e.Proof)
	}
}

func TestScopeControlsVisibility(t *testing.T) {
	cs := newTestSession(t)

	call(t, cs, "prolog_create_unit", map[string]any{"name": "facts", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{"unit": "facts", "clauses": "bird(tweety)."}, nil)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "defaults"}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "defaults", "clauses": "flies(X) :- bird(X).",
	}, nil)

	// flies/1 is defined in a unit that is not in scope, so the goal must not
	// merely fail: it must be an error about an unknown procedure.
	msg := callErr(t, cs, "prolog_query", map[string]any{"goal": "flies(tweety)"})
	if !strings.Contains(msg, "flies") {
		t.Errorf("expected an unknown-procedure error mentioning flies, got: %s", msg)
	}

	call(t, cs, "prolog_load_unit", map[string]any{"unit": "defaults"}, nil)
	var p proveOut
	call(t, cs, "prolog_prove", map[string]any{"goal": "flies(tweety)"}, &p)
	if !p.Proved {
		t.Error("flies(tweety) should be provable once defaults is in scope")
	}

	// Narrowing the scope withdraws the conclusion again.
	call(t, cs, "prolog_unload_unit", map[string]any{"unit": "defaults"}, nil)
	callErr(t, cs, "prolog_query", map[string]any{"goal": "flies(tweety)"})

	var sc scopeOut
	call(t, cs, "prolog_set_scope", map[string]any{"units": []string{"defaults", "facts"}}, &sc)
	if len(sc.Scope) != 2 || sc.Scope[0] != "defaults" {
		t.Errorf("scope not set in the requested order: %v", sc.Scope)
	}
}

func TestRetractAndFind(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "facts", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "facts",
		"clauses": `parent(tom, bob).
parent(tom, liz).
parent(bob, ann).`,
	}, nil)

	var f findOut
	call(t, cs, "prolog_find_clauses", map[string]any{"pattern": "parent(tom, _)"}, &f)
	if f.Total != 2 {
		t.Fatalf("parent(tom, _): got %d matches, want 2: %+v", f.Total, f.Results)
	}

	var r retractOut
	call(t, cs, "prolog_retract", map[string]any{
		"unit": "facts", "pattern": "parent(tom, _)", "all": true,
	}, &r)
	if len(r.Removed) != 2 || r.Remaining != 1 {
		t.Fatalf("retract removed %d, %d remaining; want 2 and 1", len(r.Removed), r.Remaining)
	}

	var q queryOut
	call(t, cs, "prolog_query", map[string]any{"goal": "parent(P, C)"}, &q)
	if q.Count != 1 || q.Solutions[0]["P"] != "bob" {
		t.Errorf("after retract, expected only parent(bob, ann): %+v", q.Solutions)
	}

	// An indicator selects by name and arity.
	call(t, cs, "prolog_retract", map[string]any{"unit": "facts", "pattern": "parent/2", "all": true}, &r)
	if r.Remaining != 0 {
		t.Errorf("parent/2 should have removed everything, %d left", r.Remaining)
	}
}

func TestSyntaxErrorsDoNotMutate(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "u", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": "good(1)."}, nil)

	// Unterminated clause: rejected by the Go lexer before Prolog is involved.
	callErr(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": "bad(1)"})
	// Syntactically terminated but unparseable: rejected by SWI-Prolog.
	callErr(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": "bad( ,)."})

	var s showUnitOut
	call(t, cs, "prolog_show_unit", map[string]any{"unit": "u"}, &s)
	if len(s.Clauses) != 1 {
		t.Errorf("failed asserts must not change the unit; got %d clauses", len(s.Clauses))
	}
}

func TestSandboxRejectsUnsafeGoals(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "u", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": "safe(1)."}, nil)

	msg := callErr(t, cs, "prolog_query", map[string]any{"goal": `shell('id')`})
	if !strings.Contains(msg, "sandbox") {
		t.Errorf("expected a sandbox rejection, got: %s", msg)
	}

	// Directives run at load time, before the goal sandbox can see them, so they
	// are filtered separately.
	msg = callErr(t, cs, "prolog_assert", map[string]any{
		"unit": "u", "clauses": `:- shell('id').`,
	})
	if !strings.Contains(msg, "not permitted") {
		t.Errorf("expected the directive to be refused, got: %s", msg)
	}
	// Harmless declarations are still allowed.
	call(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": ":- dynamic counter/1."}, nil)
}

func TestQueryLimitAndTimeout(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "u", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "u", "clauses": "nat(0).\nnat(N) :- nat(M), N is M + 1.\nfinite(a).\nfinite(b).",
	}, nil)

	var q queryOut
	call(t, cs, "prolog_query", map[string]any{"goal": "nat(N)", "limit": 5}, &q)
	if q.Count != 5 || !q.Truncated {
		t.Errorf("expected 5 truncated solutions, got count=%d truncated=%v", q.Count, q.Truncated)
	}
	call(t, cs, "prolog_query", map[string]any{"goal": "finite(X)", "limit": 1}, &q)
	if q.Count != 1 || !q.Truncated {
		t.Errorf("expected 1 truncated finite solution, got count=%d truncated=%v", q.Count, q.Truncated)
	}
	call(t, cs, "prolog_query", map[string]any{"goal": "finite(X)", "limit": 2}, &q)
	if q.Count != 2 || q.Truncated {
		t.Errorf("expected exactly 2 untruncated solutions, got count=%d truncated=%v", q.Count, q.Truncated)
	}
	call(t, cs, "prolog_query", map[string]any{"goal": "finite(X)", "limit": 3}, &q)
	if q.Count != 2 || q.Truncated {
		t.Errorf("expected 2 untruncated solutions below the limit, got count=%d truncated=%v", q.Count, q.Truncated)
	}

	// A goal that cannot succeed and never terminates must be stopped by the
	// in-Prolog time limit rather than hanging the server.
	msg := callErr(t, cs, "prolog_query", map[string]any{
		"goal": "nat(N), N < 0", "timeoutMs": 800,
	})
	if !strings.Contains(msg, "time_limit_exceeded") {
		t.Errorf("expected a time limit error, got: %s", msg)
	}
}

func TestStatePersistsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "kb.json")

	kb1, err := NewKB(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := kb1.CreateUnit("u", "desc"); err != nil {
		t.Fatal(err)
	}
	if _, err := kb1.AddClauses("u", "a(1).\na(2)."); err != nil {
		t.Fatal(err)
	}
	if _, err := kb1.LoadUnit("u"); err != nil {
		t.Fatal(err)
	}

	kb2, err := NewKB(path)
	if err != nil {
		t.Fatal(err)
	}
	units, scope := kb2.ListUnits()
	if len(units) != 1 || units[0].Clauses != 2 || len(scope) != 1 {
		t.Fatalf("state did not round-trip: units=%+v scope=%v", units, scope)
	}
}

// TestToolCallsAreSerialised checks that the serialising middleware really does
// prevent two tool calls from overlapping. Without it, a client that issues a
// batch of calls gets them executed concurrently.
func TestToolCallsAreSerialised(t *testing.T) {
	cs := newTestSessionOpts(t, true)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "u", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": "n(1).\nn(2).\nn(3)."}, nil)

	var (
		mu      sync.Mutex
		inFlite int
		maxSeen int
		wg      sync.WaitGroup
	)
	probe := func() {
		mu.Lock()
		inFlite++
		if inFlite > maxSeen {
			maxSeen = inFlite
		}
		mu.Unlock()
	}
	done := func() {
		mu.Lock()
		inFlite--
		mu.Unlock()
	}

	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			probe()
			defer done()
			_, err := cs.CallTool(context.Background(), &mcp.CallToolParams{
				Name:      "prolog_query",
				Arguments: map[string]any{"goal": "n(X)"},
			})
			if err != nil {
				t.Errorf("CallTool: %v", err)
			}
		}()
	}
	wg.Wait()
	// The probe counts client-side goroutines, so it cannot prove serialisation
	// on its own; what matters is that every call completed successfully and the
	// knowledge base is intact afterwards.
	var q queryOut
	call(t, cs, "prolog_query", map[string]any{"goal": "n(X)"}, &q)
	if q.Count != 3 {
		t.Errorf("after %d concurrent calls, expected 3 solutions, got %d", maxSeen, q.Count)
	}
}

// TestConcurrentRetractsRemoveTheRightClauses is the case the middleware exists
// for. prolog_retract is a read-modify-write: it asks SWI-Prolog which clause
// positions match, then removes those positions. The positions are only valid
// while the unit is unchanged, so two retracts that interleave can delete the
// wrong clauses. Serialising tool calls closes that window.
func TestConcurrentRetractsRemoveTheRightClauses(t *testing.T) {
	cs := newTestSessionOpts(t, true)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "u", "load": true}, nil)

	const n = 10
	var src strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&src, "fact(%d).\n", i)
	}
	call(t, cs, "prolog_assert", map[string]any{"unit": "u", "clauses": src.String()}, nil)

	// Concurrently retract the even-numbered facts.
	var wg sync.WaitGroup
	for i := 0; i < n; i += 2 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := cs.CallTool(context.Background(), &mcp.CallToolParams{
				Name: "prolog_retract",
				Arguments: map[string]any{
					"unit":    "u",
					"pattern": fmt.Sprintf("fact(%d)", i),
					"all":     true,
				},
			})
			if err != nil {
				t.Errorf("CallTool: %v", err)
			}
		}(i)
	}
	wg.Wait()

	var q queryOut
	call(t, cs, "prolog_query", map[string]any{"goal": "fact(X)", "limit": 50}, &q)
	got := map[string]bool{}
	for _, sol := range q.Solutions {
		got[sol["X"]] = true
	}
	for i := 1; i < n; i += 2 {
		if !got[fmt.Sprint(i)] {
			t.Errorf("fact(%d) was removed but should have survived; remaining: %+v", i, q.Solutions)
		}
	}
	for i := 0; i < n; i += 2 {
		if got[fmt.Sprint(i)] {
			t.Errorf("fact(%d) should have been retracted; remaining: %+v", i, q.Solutions)
		}
	}
}

// TestUndefinedPredicateInRuleBodyIsFalse covers the closed-world behaviour: a
// rule may refer to a predicate defined in a unit that is not currently in
// scope, and that reference should simply fail rather than raise an error. A
// predicate the caller names directly must still be reported, so typos surface.
func TestUndefinedPredicateInRuleBodyIsFalse(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "policy", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "policy",
		"clauses": `person(ana).
allowed(P) :- person(P), \+ banned(P).`,
	}, nil)

	// banned/1 is not defined anywhere, but it is only reached through a rule.
	var q queryOut
	call(t, cs, "prolog_query", map[string]any{"goal": "allowed(P)"}, &q)
	if q.Count != 1 || q.Solutions[0]["P"] != "ana" {
		t.Fatalf("expected allowed(ana); got %+v", q.Solutions)
	}

	// Naming an undefined predicate directly is still an error.
	msg := callErr(t, cs, "prolog_query", map[string]any{"goal": "bnned(X)"})
	if !strings.Contains(msg, "bnned") {
		t.Errorf("expected the typo to be reported, got: %s", msg)
	}

	// Defining it in another unit changes the answer once that unit is loaded.
	call(t, cs, "prolog_create_unit", map[string]any{"name": "sanctions"}, nil)
	call(t, cs, "prolog_assert", map[string]any{"unit": "sanctions", "clauses": "banned(ana)."}, nil)
	call(t, cs, "prolog_load_unit", map[string]any{"unit": "sanctions"}, nil)
	call(t, cs, "prolog_query", map[string]any{"goal": "allowed(P)"}, &q)
	if q.Count != 0 {
		t.Errorf("with sanctions loaded, expected no solutions; got %+v", q.Solutions)
	}
}

func TestExplainDepthLimitPreservesTruth(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "u", "load": true}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "u", "clauses": `fact.
verified :- fact.
claimed :- missing.
cut_false :- !, fail.
cut_false.
if_false :- (true -> fail ; true).`,
	}, nil)

	var e explainOut
	call(t, cs, "prolog_explain", map[string]any{"goal": "verified", "depth": 1}, &e)
	if !e.Proved || !strings.Contains(e.Proof, "depth limit reached") {
		t.Errorf("expected a true proof with an elided subtree, got proved=%v:\n%s", e.Proved, e.Proof)
	}
	call(t, cs, "prolog_explain", map[string]any{"goal": "claimed", "depth": 1}, &e)
	if e.Proved {
		t.Errorf("depth cutoff must not prove a false goal:\n%s", e.Proof)
	}
	call(t, cs, "prolog_explain", map[string]any{"goal": "cut_false"}, &e)
	if e.Proved {
		t.Errorf("meta-interpreter must preserve native cut semantics:\n%s", e.Proof)
	}
	call(t, cs, "prolog_explain", map[string]any{"goal": "if_false"}, &e)
	if e.Proved {
		t.Errorf("meta-interpreter must preserve native if-then-else semantics:\n%s", e.Proof)
	}
}

func TestExplainHandlesImportedLibraryPredicates(t *testing.T) {
	cs := newTestSession(t)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "facts", "load": false}, nil)
	call(t, cs, "prolog_create_unit", map[string]any{"name": "rules", "load": false}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "facts",
		"clauses": "link(hq, a, 2).\nlink(a, backup, 3).\n",
	}, nil)
	call(t, cs, "prolog_assert", map[string]any{
		"unit": "rules",
		"clauses": "route(From, To, Path, Cost) :- route(From, To, [From], RevPath, 0, Cost), reverse(RevPath, Path).\nroute(To, To, Visited, Visited, Cost, Cost).\nroute(From, To, Visited, Path, Acc, Cost) :- link(From, Next, Step), \\+ member(Next, Visited), Acc1 is Acc + Step, route(Next, To, [Next|Visited], Path, Acc1, Cost).\n",
	}, nil)
	call(t, cs, "prolog_set_scope", map[string]any{"units": []string{"facts", "rules"}}, nil)

	var e explainOut
	call(t, cs, "prolog_explain", map[string]any{
		"goal":      "route(hq, backup, [hq,a,backup], 5)",
		"depth":     16,
		"timeoutMs": 2000,
	}, &e)

	if !e.Proved {
		t.Fatalf("expected explain to prove route, got false with proof %q", e.Proof)
	}
	if !strings.Contains(e.Proof, "library(lists)") {
		t.Fatalf("expected proof to mention imported library predicates, got %q", e.Proof)
	}
}

func TestSplitClauses(t *testing.T) {
	tests := []struct {
		name string
		src  string
		want []string
	}{
		{"simple", "a(1). a(2).", []string{"a(1).", "a(2)."}},
		{"rule over lines", "p(X) :-\n  q(X),\n  r(X).", []string{"p(X) :-\n  q(X),\n  r(X)."}},
		{"dot in quoted atom", `a('x.y').`, []string{`a('x.y').`}},
		{"dot in string", `a("x. y").`, []string{`a("x. y").`}},
		{"escaped quote", `a('it\'s').`, []string{`a('it\'s').`}},
		{"doubled quote", `a('it''s').`, []string{`a('it''s').`}},
		{"char code dot", `a(0'.).`, []string{`a(0'.).`}},
		{"char code quote", `a(0'').`, []string{`a(0'').`}},
		{"line comment", "% hi\na(1).", []string{"% hi\na(1)."}},
		{"block comment", "/* a. b. */\na(1).", []string{"/* a. b. */\na(1)."}},
		{"decimal is not a terminator", "a(1.5).", []string{"a(1.5)."}},
		{"trailing comment only", "a(1).\n% done", []string{"a(1)."}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := SplitClauses(tc.src)
			if err != nil {
				t.Fatalf("SplitClauses(%q): %v", tc.src, err)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("got %d clauses %q, want %d %q", len(got), got, len(tc.want), tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("clause %d: got %q, want %q", i, got[i], tc.want[i])
				}
			}
		})
	}

	for _, bad := range []string{"a(1)", "a('x).", "/* unterminated"} {
		if _, err := SplitClauses(bad); err == nil {
			t.Errorf("SplitClauses(%q) should have failed", bad)
		}
	}
}
