---
name: keep-a-todo-list
description: Track work across coding sessions in a JSON TODO list using a portable Python script that maintains integrity signatures, explicit dependencies, and task statuses.
---

# Keep a TODO list

Maintain `TODO.json` at the project root through the bundled
[management script](scripts/todo.py). Capture work as it becomes clear, keep its
description current, and use the list to recover what needs attention across
sessions. Planning sessions can produce items, and discoveries during any other
work can produce them too.

## Use the script

Run from the project root. Use the script at its installed skill path, or copy
it into the project, for example as `scripts/todo.py`. The commands below assume
that project-local path. Choose a different location if it would overwrite an
existing tool.

The script uses only the Python 3.10+ standard library and supports Windows,
macOS, and Linux. Run `python3 scripts/todo.py ...`, or use `python` or `py`
according to the installed Python launcher. Hooks must use the same available
runtime. Git is required only for staged validation and hook integration.

```bash
python3 scripts/todo.py init
python3 scripts/todo.py create 'Handle expired sessions' --details 'Refresh once; surface a useful error if refresh fails.'
python3 scripts/todo.py list --open
python3 scripts/todo.py list --ready
python3 scripts/todo.py list --blocked
python3 scripts/todo.py list --status paused
python3 scripts/todo.py list --all --search session
python3 scripts/todo.py show T-a7c91e2f
python3 scripts/todo.py deps T-a7c91e2f --transitive
python3 scripts/todo.py dependents T-a7c91e2f --transitive
python3 scripts/todo.py update T-a7c91e2f --status in_progress
python3 scripts/todo.py update T-a7c91e2f --depends-on T-5bd208c4
python3 scripts/todo.py update T-a7c91e2f --clear-dependencies
python3 scripts/todo.py complete T-a7c91e2f
python3 scripts/todo.py abandon T-5bd208c4 --details 'The upstream API now provides this functionality.'
python3 scripts/todo.py validate
```

Example IDs are placeholders; use the IDs returned by `create`. Dependency flags
can be repeated. On `update`, `--depends-on` replaces the whole dependency list;
`--clear-dependencies` explicitly empties it. Title and details updates replace
those fields too: preserve useful existing context when revising them. Use
`--details-file PATH` or `--details-file -` for multiline details. Single-item
results use JSON; `--json` also requests full JSON for list and dependency
queries instead of summary lines. `close` aliases `complete`. `list` defaults
to open items. `--file PATH`, placed before the command, selects another TODO
file.

## Read and track work

At session start, list open items and inspect the ones relevant to the current
request. Query ready work, dependencies, or dependents as useful; a ready item
has status `todo` and all prerequisites completed. A blocked item is open with
incomplete prerequisites. Read full details before acting. The list records
work; the user's current instructions establish what to work on.

Create an item when there is concrete work worth remembering. Use a clear title
and free-form details sufficient to recover its purpose, scope, and what would
count as completion. Update items as understanding changes. Avoid speculative
task trees and duplicate items; inspect existing work before adding another
record of the same obligation. Small immediate actions need not each become an
item.

Dependencies mean that completing this item requires the other item to be
completed. Use them for actual prerequisites. Splitting work into items does not
by itself establish dependency edges.

Statuses are:

- `todo`: identified work awaiting action.
- `in_progress`: work currently underway.
- `paused`: deliberately suspended work; preserve why and how to resume in details.
- `completed`: the described work is finished, including its completion conditions.
- `abandoned`: work discarded after moving beyond `todo`.
- `rejected`: work discarded while still `todo`.

Open statuses are `todo`, `in_progress`, and `paused`. Only `completed` satisfies
a dependency. The script refuses completion with incomplete prerequisites,
missing dependencies, duplicate dependencies, and dependency cycles. It also
refuses to discard a prerequisite while open items still depend on it. Revisit
those dependents explicitly: revise a dependency that is no longer needed,
substitute a prerequisite, or discard dependent work first.

`abandon` chooses `rejected` for an item currently `todo`, and `abandoned` for
other open items. An update from `todo` to `abandoned` likewise becomes
`rejected`. Preserve items and their IDs after completion or rejection. To
resume terminal work, update its status and details. Every update must leave
the graph valid: reopening a prerequisite requires first reopening any completed
dependents that would otherwise rely on incomplete work.

## Integrity and commits

Never edit `TODO.json` directly or recompute its signatures yourself. All normal
mutations go through the script, which validates the existing list before
changing it. It serializes concurrent local writers and replaces the file
atomically after checking the resulting state.

Format version 1 contains a list of items with stable `T-` plus eight random
hexadecimal-digit IDs, titles, details, statuses, dependencies, and signatures.
SHA-256 signatures cover each item's semantic contents and the whole ordered
collection, including its format version. They detect content changes,
additions, deletions, and reordering made without updating the signatures.
Whitespace and JSON object key order do not affect signatures. These are
unkeyed integrity digests for detecting edits outside the workflow; they do not
authenticate the author or prevent deliberate forgery.

Working-file operations coordinate through an atomically created sibling lock
directory, `.TODO.json.lock`, which is removed when the operation exits. Lock
contention waits up to ten seconds, then fails without changing the list or
removing the existing lock. An interrupted process can leave a stale lock:
inspect its `owner.json` (PID and host), confirm the process is no longer active,
then remove that lock directory and retry. Never remove a lock merely because
it is old. Keep transient lock directories out of commits.

Validate before committing. The [Python pre-commit example](scripts/pre-commit)
checks the staged JSON rather than the working copy and requires no shell. When
setting up enforcement, add
its validation command to the project's existing hook mechanism. If there is no
existing mechanism, it can be installed as an executable Git pre-commit hook.
Preserve existing hooks and adapt the script path and Python runtime. The hook
allows an absent list before initial adoption, but rejects staged deletion of an
existing list.

If validation fails, stop normal mutations. Restore a verified version and
replay intended changes through the script. There is no general-purpose command
to bless an invalid file.

For branch merges, export the base, ours, and theirs verified JSON snapshots to temporary
files, then run:

```bash
python3 scripts/todo.py merge --base PATH_TO_BASE.json --ours PATH_TO_OURS.json --theirs PATH_TO_THEIRS.json
```

This explicit recovery operation verifies all three sources and combines
independent item changes. It may replace a destination containing conflict
markers. Conflicting changes to the same item or an invalid combined dependency
graph stop the merge without changing the destination. Resolve these by
restoring a verified side and replaying the intended changes through commands;
`create --id ID` preserves incoming item identities. Do not silently discard
branch-specific work or reassign an existing ID to different work.

## Work with a ledger

When a project also keeps a ledger, use stable TODO IDs to connect live work to
its reasoning and history. Significant clarifications, discoveries, and changes
of direction may warrant ledger entries referring to the affected items.
Routine status updates need not each become ledger entries.

Keep the two accounts consistent: a ledger entry claiming to close an item must
agree with the TODO state. Revising or reopening work should preserve material
reasons in the item details or a linked ledger entry. The TODO script does not
edit or validate the ledger.
