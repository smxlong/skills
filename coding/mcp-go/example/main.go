// Command prolog-mcp is an MCP server that exposes SWI-Prolog as a set of logic
// tools: an agent can build up a knowledge base of facts and rules, organise it
// into units, choose which units are in scope, and then query, prove and
// explain goals against them.
//
// It is also the worked example for the mcp-go skill, so the code is commented
// with the reasoning behind each choice.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// version is stamped into the server's Implementation, which clients see during
// initialization.
const version = "v0.1.0"

func main() {
	var (
		httpAddr     = flag.String("http", "", "serve Streamable HTTP on this address instead of stdio (e.g. :8080)")
		statePath    = flag.String("state", envOr("PROLOG_MCP_STATE", "prolog-kb.json"), "path to the knowledge base file; empty means keep state in memory only")
		swiplPath    = flag.String("swipl", envOr("PROLOG_MCP_SWIPL", "swipl"), "path to the swipl executable")
		timeout      = flag.Duration("timeout", 10*time.Second, "default time limit for a single goal")
		stackLimit   = flag.String("stack-limit", "512m", "SWI-Prolog stack limit for each query process")
		maxSolutions = flag.Int("max-solutions", 200, "hard cap on solutions returned by a single query")
		sandbox      = flag.Bool("sandbox", true, "reject goals that library(sandbox) considers unsafe")
		allowDirs    = flag.Bool("allow-directives", false, "allow arbitrary ':- Goal.' directives in unit source (unsafe: directives run at load time, outside the sandbox)")
		serialize    = flag.Bool("serialize", true, "run tool calls one at a time; disable only if every tool is independent and side-effect free")
	)
	flag.Parse()

	if err := run(*httpAddr, *statePath, *swiplPath, *stackLimit, *timeout, *maxSolutions, *sandbox, *allowDirs, *serialize); err != nil {
		// Never write to stdout on the stdio transport: stdout carries the
		// JSON-RPC stream and any stray byte corrupts the session.
		fmt.Fprintln(os.Stderr, "prolog-mcp:", err)
		os.Exit(1)
	}
}

func run(httpAddr, statePath, swiplPath, stackLimit string, timeout time.Duration, maxSolutions int, sandbox, allowDirs, serialize bool) error {
	kb, err := NewKB(statePath)
	if err != nil {
		return err
	}
	runner, err := NewRunner(swiplPath, stackLimit, sandbox, timeout)
	if err != nil {
		return err
	}

	ps := &PrologServer{
		kb:              kb,
		runner:          runner,
		maxSolutions:    maxSolutions,
		allowDirectives: allowDirs,
	}

	opts := &mcp.ServerOptions{
		// Instructions are shown to the model once, at initialization. They are
		// the right place for cross-cutting guidance that would otherwise have to
		// be repeated in every tool description.
		Instructions: instructions,
	}
	if httpAddr != "" {
		// Keepalive pings detect peers that vanish without closing the
		// connection, which only happens on a network transport. On stdio a
		// closed stdin already signals disconnection, and an in-flight ping
		// racing with that EOF turns a clean shutdown into an error.
		opts.KeepAlive = 30 * time.Second
	}

	server := mcp.NewServer(&mcp.Implementation{
		Name:    "prolog",
		Title:   "SWI-Prolog logic tools",
		Version: version,
	}, opts)
	ps.Register(server)

	if serialize {
		server.AddReceivingMiddleware(serializeToolCalls())
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if httpAddr == "" {
		// One process, one client, framed over stdin/stdout.
		if err := server.Run(ctx, &mcp.StdioTransport{}); !isCleanShutdown(err) {
			return err
		}
		return nil
	}

	// Streamable HTTP: one process, many clients. The handler is called per
	// request and returns the server to use for that session.
	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server {
		return server
	}, nil)
	httpServer := &http.Server{
		Addr:              httpAddr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
	}()
	log.SetOutput(os.Stderr)
	log.Printf("listening on %s", httpAddr)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}

// serializeToolCalls returns receiving middleware that lets at most one
// tools/call run at a time.
//
// The SDK calls jsonrpc2.Async on every request except initialize, so tool
// calls that a client issues as a batch execute concurrently by default. That
// is fine for a stateless server and wrong for this one: two calls that both
// read the knowledge base, act on it and write it back can interleave, and a
// read issued alongside a mutation can observe the state from before it.
//
// Only tools/call is serialised. tools/list, ping and the rest stay concurrent,
// so a long-running query does not make the server look hung.
//
// This guarantees mutual exclusion, not arrival order: the SDK releases each
// request for concurrent execution before any middleware runs, so by the time
// this lock is contended the original ordering is already lost. A client that
// needs B to observe A's effect must wait for A's response before sending B --
// no server-side setting can substitute for that.
func serializeToolCalls() mcp.Middleware {
	var mu sync.Mutex
	return func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
			if method != "tools/call" {
				return next(ctx, method, req)
			}
			mu.Lock()
			defer mu.Unlock()
			// Do not start work the caller has already given up on.
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			return next(ctx, method, req)
		}
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// codeServerClosing is the JSON-RPC error code the SDK reports when a session
// ends because the peer went away. On stdio that is exactly what a client
// closing stdin looks like, so Server.Run always returns an error wrapping it
// at the end of a normal session. The SDK does not export the constant (it is
// jsonrpc2.ErrServerClosing internally), so it is repeated here.
const codeServerClosing = -32004

// isCleanShutdown reports whether an error from Server.Run represents an
// ordinary end of session rather than a failure. Without this, every stdio
// server exits non-zero every time its client disconnects.
func isCleanShutdown(err error) bool {
	if err == nil || errors.Is(err, io.EOF) || errors.Is(err, context.Canceled) {
		return true
	}
	var wireErr *jsonrpc.Error
	return errors.As(err, &wireErr) && wireErr.Code == codeServerClosing
}

const instructions = `This server gives you a persistent Prolog knowledge base and the ability to
reason over it.

Facts and rules live in units: named groups of clauses. Only units that are in
scope are visible to queries, so you can reason under different sets of
assumptions by changing what is loaded.

A normal working pattern is:

  1. prolog_list_units to see what already exists.
  2. prolog_create_unit, then prolog_assert to add facts and rules.
  3. prolog_load_unit (or prolog_set_scope) to choose what is in scope.
  4. prolog_query / prolog_prove to draw conclusions, and prolog_explain when
     you need to justify or debug one.

Goals are written without a trailing '.'. Variables start with a capital letter
or an underscore; those starting with '_' are not reported in results.

State persists across calls, so treat the knowledge base as long-lived: check
what is there before adding to it, and prefer editing a unit over creating a
near-duplicate.`
