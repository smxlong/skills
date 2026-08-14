package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"unicode"
)

// Clause is a single Prolog clause, stored as source text including its
// terminating '.'.
type Clause struct {
	ID   int    `json:"id"`
	Text string `json:"text"`
}

// Unit is a named collection of clauses. Units are the organising principle the
// tools expose to agents: an agent groups related predicates into a unit and
// then brings units in and out of query scope.
type Unit struct {
	Name        string   `json:"name"`
	Description string   `json:"description,omitempty"`
	Clauses     []Clause `json:"clauses"`
	NextID      int      `json:"nextId"`
}

// Source renders the unit as a Prolog source file.
func (u *Unit) Source() string {
	var b strings.Builder
	for _, c := range u.Clauses {
		b.WriteString(c.Text)
		b.WriteString("\n")
	}
	return b.String()
}

// KB is the persistent knowledge base: a set of units plus the ordered list of
// units currently in query scope.
type KB struct {
	mu    sync.Mutex
	path  string
	Units map[string]*Unit `json:"units"`
	Scope []string         `json:"scope"`
}

func NewKB(path string) (*KB, error) {
	kb := &KB{path: path, Units: map[string]*Unit{}}
	if path == "" {
		return kb, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return kb, nil
		}
		return nil, fmt.Errorf("reading state %s: %w", path, err)
	}
	if err := json.Unmarshal(data, kb); err != nil {
		return nil, fmt.Errorf("parsing state %s: %w", path, err)
	}
	if kb.Units == nil {
		kb.Units = map[string]*Unit{}
	}
	return kb, nil
}

// save writes the state to disk. Callers must hold kb.mu.
func (kb *KB) save() error {
	if kb.path == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(kb.path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(kb, "", "  ")
	if err != nil {
		return err
	}
	// Write-then-rename so a crash mid-write cannot truncate the knowledge base.
	tmp := kb.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, kb.path)
}

// validUnitName restricts unit names to characters that are safe in file names
// and readable in Prolog comments.
func validUnitName(name string) error {
	if name == "" {
		return fmt.Errorf("unit name must not be empty")
	}
	if len(name) > 64 {
		return fmt.Errorf("unit name must be at most 64 characters")
	}
	for _, r := range name {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) && r != '_' && r != '-' {
			return fmt.Errorf("unit name %q contains invalid character %q; use letters, digits, '_' and '-'", name, r)
		}
	}
	return nil
}

func (kb *KB) CreateUnit(name, description string) error {
	if err := validUnitName(name); err != nil {
		return err
	}
	kb.mu.Lock()
	defer kb.mu.Unlock()
	if _, ok := kb.Units[name]; ok {
		return fmt.Errorf("unit %q already exists", name)
	}
	kb.Units[name] = &Unit{Name: name, Description: description, NextID: 1}
	return kb.save()
}

func (kb *KB) DeleteUnit(name string) error {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	if _, ok := kb.Units[name]; !ok {
		return fmt.Errorf("unit %q does not exist", name)
	}
	delete(kb.Units, name)
	kb.Scope = removeString(kb.Scope, name)
	return kb.save()
}

// UnitSummary is the listing view of a unit.
type UnitSummary struct {
	Name        string `json:"name" jsonschema:"unit name"`
	Description string `json:"description,omitempty" jsonschema:"what the unit is for"`
	Clauses     int    `json:"clauses" jsonschema:"number of clauses in the unit"`
	InScope     bool   `json:"inScope" jsonschema:"whether the unit is currently loaded into query scope"`
}

func (kb *KB) ListUnits() ([]UnitSummary, []string) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	names := make([]string, 0, len(kb.Units))
	for n := range kb.Units {
		names = append(names, n)
	}
	sort.Strings(names)
	out := make([]UnitSummary, 0, len(names))
	for _, n := range names {
		u := kb.Units[n]
		out = append(out, UnitSummary{
			Name:        u.Name,
			Description: u.Description,
			Clauses:     len(u.Clauses),
			InScope:     containsString(kb.Scope, n),
		})
	}
	return out, append([]string(nil), kb.Scope...)
}

func (kb *KB) UnitSource(name string) (string, string, error) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	u, ok := kb.Units[name]
	if !ok {
		return "", "", fmt.Errorf("unit %q does not exist", name)
	}
	return u.Source(), u.Description, nil
}

// AddClauses appends clauses parsed from src to a unit.
func (kb *KB) AddClauses(name, src string) ([]Clause, error) {
	texts, err := SplitClauses(src)
	if err != nil {
		return nil, err
	}
	if len(texts) == 0 {
		return nil, fmt.Errorf("no clauses found; every clause must end with '.'")
	}
	kb.mu.Lock()
	defer kb.mu.Unlock()
	u, ok := kb.Units[name]
	if !ok {
		return nil, fmt.Errorf("unit %q does not exist", name)
	}
	added := make([]Clause, 0, len(texts))
	for _, t := range texts {
		c := Clause{ID: u.NextID, Text: t}
		u.NextID++
		u.Clauses = append(u.Clauses, c)
		added = append(added, c)
	}
	return added, kb.save()
}

// ReplaceSource replaces the entire contents of a unit.
func (kb *KB) ReplaceSource(name, src string) ([]Clause, error) {
	texts, err := SplitClauses(src)
	if err != nil {
		return nil, err
	}
	kb.mu.Lock()
	defer kb.mu.Unlock()
	u, ok := kb.Units[name]
	if !ok {
		return nil, fmt.Errorf("unit %q does not exist", name)
	}
	u.Clauses = nil
	u.NextID = 1
	for _, t := range texts {
		u.Clauses = append(u.Clauses, Clause{ID: u.NextID, Text: t})
		u.NextID++
	}
	return append([]Clause(nil), u.Clauses...), kb.save()
}

// RemoveAt removes the clauses at the given 1-based positions.
func (kb *KB) RemoveAt(name string, positions []int) ([]Clause, error) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	u, ok := kb.Units[name]
	if !ok {
		return nil, fmt.Errorf("unit %q does not exist", name)
	}
	drop := map[int]bool{}
	for _, p := range positions {
		if p < 1 || p > len(u.Clauses) {
			return nil, fmt.Errorf("clause position %d out of range 1..%d", p, len(u.Clauses))
		}
		drop[p] = true
	}
	var kept, removed []Clause
	for i, c := range u.Clauses {
		if drop[i+1] {
			removed = append(removed, c)
		} else {
			kept = append(kept, c)
		}
	}
	u.Clauses = kept
	return removed, kb.save()
}

func (kb *KB) SetScope(names []string) ([]string, error) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	seen := map[string]bool{}
	scope := make([]string, 0, len(names))
	for _, n := range names {
		if _, ok := kb.Units[n]; !ok {
			return nil, fmt.Errorf("unit %q does not exist", n)
		}
		if seen[n] {
			continue
		}
		seen[n] = true
		scope = append(scope, n)
	}
	kb.Scope = scope
	return append([]string(nil), kb.Scope...), kb.save()
}

func (kb *KB) LoadUnit(name string) ([]string, error) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	if _, ok := kb.Units[name]; !ok {
		return nil, fmt.Errorf("unit %q does not exist", name)
	}
	if !containsString(kb.Scope, name) {
		kb.Scope = append(kb.Scope, name)
	}
	return append([]string(nil), kb.Scope...), kb.save()
}

func (kb *KB) UnloadUnit(name string) ([]string, error) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	if !containsString(kb.Scope, name) {
		return nil, fmt.Errorf("unit %q is not in scope", name)
	}
	kb.Scope = removeString(kb.Scope, name)
	return append([]string(nil), kb.Scope...), kb.save()
}

// Program renders the in-scope units as one Prolog source file.
//
// style_check(-discontiguous) is set because units are an organisational device
// chosen by the agent: the same predicate may legitimately be defined across
// several units. Singleton warnings are deliberately left on, because they are
// almost always a real bug and are surfaced to the agent as diagnostics.
func (kb *KB) Program() (string, []string) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	var b strings.Builder
	b.WriteString(":- style_check(-discontiguous).\n\n")
	var used []string
	for _, n := range kb.Scope {
		u, ok := kb.Units[n]
		if !ok {
			continue
		}
		used = append(used, n)
		fmt.Fprintf(&b, "%% ---- unit %s ----\n", n)
		b.WriteString(u.Source())
		b.WriteString("\n")
	}
	return b.String(), used
}

func (kb *KB) UnitNames() []string {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	names := make([]string, 0, len(kb.Units))
	for n := range kb.Units {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

func containsString(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

func removeString(s []string, v string) []string {
	out := s[:0]
	for _, x := range s {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}

// SplitClauses splits Prolog source text into individual clauses, each
// including its terminating '.'. Comments are preserved and attach to the
// clause that follows them.
//
// This is a lexer, not a parser: it only needs to know where clauses end. The
// cases that matter are the ones where a '.' or a quote does not mean what it
// appears to mean:
//
//   - inside 'quoted atoms', "strings" and `back-quoted strings`, where quotes
//     may be escaped with a backslash or by doubling;
//   - inside % line comments and /* block comments */;
//   - in 0'c character-code literals, where the character after the quote is
//     data (0'. and 0” are both legal).
func SplitClauses(src string) ([]string, error) {
	var (
		out  []string
		cur  strings.Builder
		rs   = []rune(src)
		n    = len(rs)
		i    int
		prev rune // previous significant rune, for disambiguating 0'c
	)
	flush := func() {
		if t := strings.TrimSpace(cur.String()); t != "" {
			out = append(out, t)
		}
		cur.Reset()
	}
	for i < n {
		c := rs[i]
		switch {
		case c == '%':
			for i < n && rs[i] != '\n' {
				cur.WriteRune(rs[i])
				i++
			}

		case c == '/' && i+1 < n && rs[i+1] == '*':
			j := i + 2
			for j+1 < n && !(rs[j] == '*' && rs[j+1] == '/') {
				j++
			}
			if j+1 >= n {
				return nil, fmt.Errorf("unterminated block comment")
			}
			cur.WriteString(string(rs[i : j+2]))
			i = j + 2

		case c == '0' && i+1 < n && rs[i+1] == '\'' && !isAlnum(prev):
			cur.WriteString("0'")
			i += 2
			if i < n {
				if rs[i] == '\\' && i+1 < n {
					cur.WriteRune(rs[i])
					cur.WriteRune(rs[i+1])
					i += 2
				} else if rs[i] == '\'' && i+1 < n && rs[i+1] == '\'' {
					cur.WriteString("''")
					i += 2
				} else {
					cur.WriteRune(rs[i])
					i++
				}
			}
			prev = 'x'

		case c == '\'' || c == '"' || c == '`':
			q := c
			cur.WriteRune(c)
			i++
			for {
				if i >= n {
					return nil, fmt.Errorf("unterminated quoted token (%c)", q)
				}
				if rs[i] == '\\' && i+1 < n {
					cur.WriteRune(rs[i])
					cur.WriteRune(rs[i+1])
					i += 2
					continue
				}
				if rs[i] == q {
					if i+1 < n && rs[i+1] == q { // doubled quote escapes itself
						cur.WriteRune(q)
						cur.WriteRune(q)
						i += 2
						continue
					}
					cur.WriteRune(q)
					i++
					break
				}
				cur.WriteRune(rs[i])
				i++
			}
			prev = 'x'

		case c == '.' && (i+1 >= n || isLayout(rs[i+1]) || rs[i+1] == '%'):
			cur.WriteRune('.')
			i++
			flush()
			prev = 0

		default:
			cur.WriteRune(c)
			if !isLayout(c) {
				prev = c
			}
			i++
		}
	}
	if t := strings.TrimSpace(cur.String()); t != "" && !isAllComment(t) {
		return nil, fmt.Errorf("clause is not terminated by '.': %s", truncate(t, 120))
	}
	return out, nil
}

func isLayout(r rune) bool {
	return r == ' ' || r == '\t' || r == '\n' || r == '\r' || r == '\f' || r == '\v'
}

func isAlnum(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_'
}

func isAllComment(s string) bool {
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "%") {
			return false
		}
	}
	return true
}

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "..."
}
