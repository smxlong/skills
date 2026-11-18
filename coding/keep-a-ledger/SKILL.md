---
name: keep-a-ledger
description: Maintain an append-only LEDGER.md at the project root to carry reasoning, discoveries, user clarifications, and unfinished work across coding sessions.
---

# Keep a ledger file

Maintain a `LEDGER.md` file at the project root level. This file is an
append-only account of the project's evolving understanding. Help future
sessions recover the context, judgment, and direction needed to resume
productive work.

Use the bundled [ledger script](scripts/ledger.py) to append, query, merge, and
validate entries. It maintains entry IDs and timestamps while preserving the
free-form contents.

## Why

Code and commit history preserve the implementation, but much of the experience
that shaped it can disappear: rejected alternatives, negative results,
conversations with the user, unresolved questions, and intentions for the next
stretch of work. Preserve that experience so future sessions can build on it.

Writing an entry is also an opportunity to reflect on what the work established,
what remains uncertain, and what should happen next.

## When to read

Read the whole ledger at the start of a new work session when it fits
comfortably in context. Recover the trajectory of the work as well as its
current constraints and unfinished questions.

If the ledger becomes too large for a full read, scan its headings first, then
read recent entries and entries relevant to the task, following cross-references.
A headings-only pass is an orientation step; read the relevant entry bodies
before relying on them.

Consult it again before:

- Proposing an architectural change or selecting between competing approaches.
- Debugging a recurring issue or revisiting an area that previously failed.

Before treating an entry as a current constraint or settled decision, search for
later entries that reference its ID and may correct, supersede, or reverse it.
Preserve the distinction between historical reasoning and current direction.

## When to write (and when not to)

Commits are a natural writing checkpoint: usually append an entry around a
commit when there is useful context to carry forward. Capture important
discoveries or changes of direction when they arise, even without a commit.

Append when there is useful context to preserve about:

- An architectural, design, or tooling decision (record the chosen path,
  the rationale, and any rejected alternatives).
- A failed experiment, disappointing benchmark, or reverted approach
  (negative results are high-value entries).
- A non-obvious root cause or environment/infrastructure constraint.
- Changes in scope, requirements, or project-level assumptions.
- User conversations that clarify priorities, preferences, tradeoffs, or intent.
  Preserve the reason and scope of a clarification so it can be applied sensibly.
- A checkpoint that establishes where the work stands, what remains unresolved, or
  what the next session should investigate or implement.

Choose material by its value to a future session. Routine implementation may
warrant an entry when it carries useful context or establishes where to resume.
Skip mechanical summaries of formatting, dependency bumps, or refactors when
the diff and commit message already convey everything worth carrying forward.

Use concise, free-form prose. Entries may be mini-ADRs, discoveries,
conversational context, progress accounts, or open questions; they need not all
be the same kind. Include the context needed to understand each entry without
reconstructing the original session.

Distinguish observations, explanations, and decisions. Preserve material
uncertainty and the conditions under which a result was obtained. Record enough
about a failed approach to recognize when changed conditions might justify
revisiting it. Make clear when something is proposed, attempted, or completed.

## Immutability and corrections

Never edit, rewrite, or delete existing entries, except to interleave entries
or resolve ID collisions and affected references during a merge as described
below. If a prior decision is changed, a fix fails, or an earlier entry was
mistaken, append a new entry explaining the change and link it to the prior
entry. Correcting a claim should preserve what was learned about why it was wrong.

## Merge conflicts

Keep shared entries once and retain every branch-specific entry. Interleave
complete entries by increasing timestamp, preserving each branch's internal
order and putting target-branch entries first for ties between branches.
Preserve IDs, timestamps, and substantive text. This merge-only reordering keeps
the combined ledger's timestamps non-decreasing; normal updates append entries.

If independently created entries happen to share an ID, give the incoming entry
a fresh ID and update references to it using branch provenance to distinguish
them. Leave all other IDs unchanged. Do not renumber entries for presentation.

Use `merge --staged` to resolve Git's unresolved ledger conflict directly, then
review and stage the result. The script verifies the base, ours, and theirs
snapshots, deduplicates shared entries, interleaves additions by timestamp, and
renames incoming ID collisions with references updated in incoming-only entries.
Historical entries changed or removed since the base, ambiguous references to
colliding IDs, or conflicting preamble edits stop the merge without replacing
the destination. Modify/delete conflicts require explicit review.

For exported snapshots, use `merge --base PATH --ours PATH --theirs PATH`.
Either form may replace a destination containing conflict markers. Neither
form stages the result automatically.

## Entry format

Ledger entries consist of a top-level section heading containing a stable ID,
a short title, and a timestamp in UTC ISO 8601 with second precision. Use exactly
`# ID Title YYYY-MM-DDTHH:MM:SSZ`, with one space around the ID and timestamp,
no indentation, and a nonempty title. IDs must be unique throughout the file.
Entry timestamps must be non-decreasing in file order; equal timestamps are
allowed. Use a valid UTC calendar timestamp ending in `Z`, without fractions or
an offset.

For new entries, use `L-` followed by eight random lowercase hexadecimal digits,
for example `L-a7c91e2f`. Generate the suffix randomly and check that the ID is
unused in the ledger before appending. IDs identify entries, not their order.
Preserve existing numeric IDs such as `L-0014`; the validator accepts numeric
suffixes of four or more digits as well as the new eight-digit hexadecimal IDs.

After the heading are one or more concise paragraphs of text content.

Optionally, a second-level heading called `Cross references` may occur,
followed by a bullet list of cross references to other ledger entries or to
TODO items with stable IDs. Use clear relationship types such as:

- `corrects` — corrects a factual claim or explanation in an earlier entry.
- `supersedes` — replaces an earlier decision or ruling.
- `reverses` — undoes or explicitly rejects a prior decision.
- `depends-on` — relies on a constraint or decision established in another
  entry.
- `implements` — executes a decision or closes out a planned item.
- `closes` — completes a tracked TODO item.
- `caused-by` — traces an outcome or bug back to an earlier event.

Example of a ledger entry:

```markdown
# L-a7c91e2f Investigate SQLite contention before replacing it 2026-11-16T14:36:10Z

The concurrent-worker benchmark hit lock contention at ~120 writes/sec with
the current transaction handling. Long transactions may be responsible; this
has not been isolated yet. The user clarified that preserving durability is
more urgent than increasing throughput, so we deferred the proposed WAL
replacement while investigating the existing store.

Next, measure transaction lifetimes and rerun the same workload after reducing
lock duration. A WAL remains an option if contention persists. T-0123 is still
open.

## Cross references

- L-5bd208c4: supersedes (plan to replace SQLite immediately).
- T-0123: relates-to (session-store throughput investigation).
```

## Use the script

The script uses only the Python 3.10+ standard library and supports Windows,
macOS, and Linux. Run it at its installed skill path, or copy it into the
project as `scripts/ledger.py` without overwriting an existing tool. From the
project root:

```bash
python3 scripts/ledger.py init
python3 scripts/ledger.py read
python3 scripts/ledger.py append 'Investigate session-store contention' --body-file entry.md
python3 scripts/ledger.py list
python3 scripts/ledger.py list --recent 10
python3 scripts/ledger.py search L-a7c91e2f
python3 scripts/ledger.py show L-a7c91e2f
python3 scripts/ledger.py merge --staged
python3 scripts/ledger.py validate
python3 scripts/ledger.py validate --staged --if-present
```

Use `python` or `py` if that is the installed Python launcher. `--file PATH`
before the command selects another ledger location. Git is needed only for
staged validation and reading conflict stages. `init` creates an empty ledger
and refuses to overwrite an existing file.

`append` accepts `--body TEXT`, `--body-file PATH`, or `--body-file -` for stdin.
It generates a random unused ID and current UTC timestamp, validates the
existing ledger and the new entry, and atomically writes the append while
preserving existing bytes. A timestamp earlier than the previous entry is
rejected; investigate the clock or use merge mode for historical branch entries.
Optional `--id` and `--timestamp` preserve identities when replaying an entry,
and must still satisfy all validation rules. Fence code examples that contain
top-level headings so they do not become ledger entries.

`read` returns the whole validated file. `list` returns headings, with optional
`--recent N` and `--search TEXT` filters. `search` matches full entry contents,
including cross-references; search an entry's ID to find later corrections.
`show` returns a complete entry by ID. Append, list, search, and show support
`--json` for structured results. Choose full or selective reading according to
the guidance above.

Working-file operations coordinate through an atomically created sibling
directory, `.LEDGER.md.lock`, and wait up to ten seconds for contention. An
interrupted process may leave a stale lock: inspect its `owner.json` (PID and
host), confirm that no process still owns it, then remove that directory and
retry. Never remove a lock merely because it is old. Keep transient lock
directories out of commits.

## Validate before committing

The validator checks top-level entry headers, unique IDs, and non-decreasing
timestamps. It reports the first error with a line number. Headings inside
fenced code blocks are ignored; close all such blocks. Other top-level headings
are reserved for entries. Empty ledgers are allowed.

The [Python pre-commit example](scripts/pre-commit) checks the staged ledger,
so a valid working copy cannot conceal invalid staged content. Integrate its
validation command with existing hooks, including TODO-list validation when
both skills are used. Preserve existing hook behavior; install the example as
an executable Git hook only when there is no existing mechanism. Adapt the
script path and Python runtime to the project.

`--if-present` allows initial adoption before the ledger exists. Staged deletion
of an existing ledger and unresolved merge conflicts are rejected. Header
validation does not compare historical entry bodies for immutability; continue
to obey the append-only and correction rules above.
