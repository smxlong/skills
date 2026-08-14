package main

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

//go:embed driver.pl
var driverSource []byte

// Diagnostic is a warning or error raised by SWI-Prolog while loading a program
// or running a goal.
type Diagnostic struct {
	Kind    string `json:"kind" jsonschema:"error or warning"`
	Message string `json:"message" jsonschema:"the diagnostic text as SWI-Prolog would print it"`
}

// Predicate is a predicate defined by a program.
type Predicate struct {
	Name    string `json:"name" jsonschema:"predicate name"`
	Arity   int    `json:"arity" jsonschema:"predicate arity"`
	Dynamic bool   `json:"dynamic" jsonschema:"whether the predicate is declared dynamic"`
}

// Match is a clause whose head unified with a search pattern.
type Match struct {
	Index int    `json:"index" jsonschema:"1-based position of the clause within its unit"`
	Text  string `json:"text" jsonschema:"the clause as SWI-Prolog prints it"`
}

// driverResult is the union of every field driver.pl can emit. Only the fields
// relevant to the requested mode are populated.
type driverResult struct {
	OK          bool         `json:"ok"`
	Error       string       `json:"error"`
	Diagnostics []Diagnostic `json:"diagnostics"`
	Output      string       `json:"output"`

	// query
	Succeeded bool                `json:"succeeded"`
	Count     int                 `json:"count"`
	Truncated bool                `json:"truncated"`
	Solutions []map[string]string `json:"solutions"`

	// prove
	Proved   bool              `json:"proved"`
	Bindings map[string]string `json:"bindings"`

	// explain
	Proof string `json:"proof"`

	// check
	Predicates []Predicate `json:"predicates"`

	// match
	Total   int     `json:"total"`
	Matches []Match `json:"matches"`
}

// Runner executes driver.pl under SWI-Prolog.
type Runner struct {
	swipl      string
	driverPath string
	stackLimit string
	sandbox    bool
	timeout    time.Duration
}

func NewRunner(swipl, stackLimit string, sandbox bool, timeout time.Duration) (*Runner, error) {
	path, err := exec.LookPath(swipl)
	if err != nil {
		return nil, fmt.Errorf("swipl not found (%q): %w; this server requires a full SWI-Prolog installation", swipl, err)
	}
	dir, err := os.MkdirTemp("", "prolog-mcp-driver-")
	if err != nil {
		return nil, err
	}
	driver := filepath.Join(dir, "driver.pl")
	if err := os.WriteFile(driver, driverSource, 0o600); err != nil {
		return nil, err
	}
	return &Runner{
		swipl:      path,
		driverPath: driver,
		stackLimit: stackLimit,
		sandbox:    sandbox,
		timeout:    timeout,
	}, nil
}

type runRequest struct {
	mode    string
	program string // Prolog source; empty means "no program"
	goal    string // goal text or match pattern; empty means "none"
	limit   int
	timeout time.Duration
	depth   int
}

func (r *Runner) Run(ctx context.Context, req runRequest) (*driverResult, error) {
	dir, err := os.MkdirTemp("", "prolog-mcp-run-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)

	programArg := "-"
	if req.program != "" {
		p := filepath.Join(dir, "program.pl")
		if err := os.WriteFile(p, []byte(req.program), 0o600); err != nil {
			return nil, err
		}
		programArg = p
	}
	goalArg := "-"
	if req.goal != "" {
		g := filepath.Join(dir, "goal.txt")
		if err := os.WriteFile(g, []byte(req.goal), 0o600); err != nil {
			return nil, err
		}
		goalArg = g
	}

	limit := req.limit
	if limit <= 0 {
		limit = 25
	}
	depth := req.depth
	if depth <= 0 {
		depth = 12
	}
	goalTimeout := req.timeout
	if goalTimeout <= 0 {
		goalTimeout = r.timeout
	}

	// The in-Prolog time limit is the first line of defence. The process-level
	// deadline is the second: a goal can wedge the engine in a way that
	// call_with_time_limit/2 cannot interrupt (deep foreign calls, runaway
	// memory), and only killing the process recovers from that.
	hardCtx, cancel := context.WithTimeout(ctx, goalTimeout+5*time.Second)
	defer cancel()

	args := []string{
		"-q",
		"--stack-limit=" + r.stackLimit,
		"--no-tty",
		r.driverPath,
		req.mode,
		programArg,
		goalArg,
		strconv.Itoa(limit),
		strconv.FormatInt(goalTimeout.Milliseconds(), 10),
		strconv.FormatBool(r.sandbox),
		strconv.Itoa(depth),
	}

	cmd := exec.CommandContext(hardCtx, r.swipl, args...)
	cmd.Dir = dir
	// Do not inherit the parent environment: the child runs agent-supplied
	// Prolog and has no business seeing the server's credentials.
	cmd.Env = []string{"LANG=C.UTF-8", "HOME=" + dir}
	cmd.WaitDelay = 2 * time.Second
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()

	// driver.pl writes its JSON result as the last line of stdout. Anything a
	// goal wrote directly to user_output (bypassing with_output_to/2) appears
	// before it, so parse the last non-empty line rather than the whole buffer.
	line := lastNonEmptyLine(stdout.String())
	if line == "" {
		if errors.Is(hardCtx.Err(), context.DeadlineExceeded) {
			return nil, fmt.Errorf("swipl exceeded the hard %s deadline and was killed", goalTimeout+5*time.Second)
		}
		if runErr != nil {
			return nil, fmt.Errorf("swipl failed: %v: %s", runErr, strings.TrimSpace(stderr.String()))
		}
		return nil, fmt.Errorf("swipl produced no result: %s", strings.TrimSpace(stderr.String()))
	}

	var res driverResult
	if err := json.Unmarshal([]byte(line), &res); err != nil {
		return nil, fmt.Errorf("could not parse driver output %q: %w", truncate(line, 200), err)
	}
	if s := strings.TrimSpace(stderr.String()); s != "" {
		res.Diagnostics = append(res.Diagnostics, Diagnostic{Kind: "stderr", Message: s})
	}
	return &res, nil
}

func lastNonEmptyLine(s string) string {
	lines := strings.Split(s, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if t := strings.TrimSpace(lines[i]); t != "" {
			return t
		}
	}
	return ""
}
