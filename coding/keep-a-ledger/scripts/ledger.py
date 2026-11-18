#!/usr/bin/env python3
"""Read, append, merge, and validate a ledger with the Python 3.10+ standard library."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time


HEADER = re.compile(
    r"# (?P<id>L-(?:[0-9a-f]{8}|[0-9]{4,})) "
    r"(?P<title>\S(?:[^\t]*\S)?) "
    r"(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\Z"
)
TOP_LEVEL = re.compile(r" {0,3}#(?!#)")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)\Z")


class LedgerError(Exception):
    """An invalid ledger or an unavailable staged snapshot."""


def parse_ledger(content, *, label="LEDGER.md"):
    """Return complete entries, or report the first error with a line number."""
    seen = {}
    entries = []
    previous = None
    closing_fence = None
    fence_line = None
    offset = 0
    for line_number, raw in enumerate(content.splitlines(keepends=True), start=1):
        start = offset
        offset += len(raw)
        line = raw.rstrip("\r\n")
        if closing_fence is not None:
            if closing_fence.fullmatch(line):
                closing_fence = None
            continue

        fence = FENCE.fullmatch(line)
        if fence:
            marker, info = fence.groups()
            # Backticks in an info string prevent a backtick fence opener.
            if marker[0] == "~" or "`" not in info:
                closing_fence = re.compile(
                    r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*"
                )
                fence_line = line_number
                continue

        if not TOP_LEVEL.match(line):
            continue
        prefix = f"{label}:{line_number}"
        match = HEADER.fullmatch(line)
        if match is None:
            raise LedgerError(
                f"{prefix}: expected '# L-<8 lowercase hex digits> Title YYYY-MM-DDTHH:MM:SSZ' "
                "(legacy numeric IDs of four or more digits are also accepted)"
            )
        item_id, title, timestamp = match.group("id", "title", "timestamp")
        if "\t" in title:
            raise LedgerError(f"{prefix}: header title must not contain tabs")
        try:
            instant = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise LedgerError(f"{prefix}: invalid UTC timestamp {timestamp}") from exc
        if item_id in seen:
            raise LedgerError(f"{prefix}: duplicate ID {item_id}; first appears on line {seen[item_id]}")
        if previous is not None and instant < previous[0]:
            raise LedgerError(
                f"{prefix}: timestamp {timestamp} precedes {previous[1]} "
                f"from line {previous[2]}; timestamps must be non-decreasing"
            )
        seen[item_id] = line_number
        entries.append({
            "id": item_id, "title": title, "timestamp": timestamp,
            "line": line_number, "offset": start,
        })
        previous = (instant, timestamp, line_number)

    if closing_fence is not None:
        raise LedgerError(f"{label}:{fence_line}: unclosed fenced code block")
    if content.strip() and not seen:
        raise LedgerError(f"{label}: nonempty ledger has no entry headers")
    for index, entry in enumerate(entries):
        end = entries[index + 1]["offset"] if index + 1 < len(entries) else len(content)
        # Blank-line separators belong to the file layout, not the entry body.
        entry["text"] = content[entry["offset"]:end].rstrip("\r\n")
    return entries


def read_text(path):
    # Preserve historical line endings when appending to a working file.
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


@contextmanager
def lock(path, *, timeout=10.0):
    directory = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            directory.mkdir()
            break
        except FileExistsError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LedgerError(
                    f"timed out waiting for ledger lock {directory}; inspect owner.json "
                    "and remove the lock directory only after confirming no writer is active"
                ) from None
            time.sleep(min(0.05, remaining))
    owner = directory / "owner.json"
    try:
        owner.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}), encoding="utf-8")
        yield
    finally:
        owner.unlink(missing_ok=True)
        directory.rmdir()


def save(path, content):
    parse_ledger(content, label=str(path))
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fresh_id(existing):
    item_id = "L-" + secrets.token_hex(4)
    while item_id in existing:
        item_id = "L-" + secrets.token_hex(4)
    return item_id


def append(path, *, title, body, item_id=None, timestamp=None):
    if not title.strip() or title != title.strip() or any(c in title for c in "\r\n\t"):
        raise LedgerError("title must be a nonempty single line without surrounding whitespace or tabs")
    if not body.strip():
        raise LedgerError("entry body must not be empty")
    with lock(path):
        content = read_text(path)
        entries = parse_ledger(content, label=str(path))
        existing = {entry["id"] for entry in entries}
        item_id = fresh_id(existing) if item_id is None else item_id
        if item_id in existing:
            raise LedgerError(f"duplicate ID: {item_id}")
        if timestamp is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        heading = f"# {item_id} {title} {timestamp}"
        if not HEADER.fullmatch(heading):
            raise LedgerError("invalid entry ID, title, or timestamp format")
        separator = ("\n" if content.endswith("\n") else "\n\n") if content else ""
        candidate = content + separator + heading + "\n\n" + body.rstrip("\r\n") + "\n"
        after = parse_ledger(candidate, label=str(path))
        if len(after) != len(entries) + 1 or after[-1]["id"] != item_id:
            raise LedgerError("entry body introduces another top-level entry; put examples inside fenced code blocks")
        save(path, candidate)
    return after[-1]


def preamble(content, entries):
    return content[:entries[0]["offset"]] if entries else content


def reference_pattern(ids):
    return re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(re.escape(item_id) for item_id in ids) + r")(?![A-Za-z0-9_-])")


def merge_contents(base, ours, theirs):
    contents = (base, ours, theirs)
    parsed = [parse_ledger(content, label=side) for content, side in zip(contents, ("base", "ours", "theirs"))]
    original, local, incoming = [{entry["id"]: entry for entry in entries} for entries in parsed]
    for item_id, old in original.items():
        for side, mapping in (("ours", local), ("theirs", incoming)):
            if item_id not in mapping or mapping[item_id]["text"] != old["text"]:
                raise LedgerError(f"{side} changed or removed historical entry {item_id}; preserve it and append a correction")

    prefixes = [preamble(content, entries).rstrip("\r\n") for content, entries in zip(contents, parsed)]
    if prefixes[1] == prefixes[2] or prefixes[2] == prefixes[0]:
        prefix = prefixes[1]
    elif prefixes[1] == prefixes[0]:
        prefix = prefixes[2]
    else:
        raise LedgerError("both branches changed the ledger preamble; resolve that context explicitly")

    occupied = set(local) | set(incoming)
    renames = {}
    for item_id, entry in incoming.items():
        if item_id in local and entry["text"] != local[item_id]["text"]:
            # Historical collisions were already rejected above.
            renames[item_id] = fresh_id(occupied)
            occupied.add(renames[item_id])
    references = reference_pattern(renames) if renames else None
    combined = list(local.values())
    for item_id, entry in incoming.items():
        if item_id in local and item_id not in renames:
            if item_id not in original and references and references.search(entry["text"]):
                raise LedgerError(f"shared new entry {item_id} references a colliding ID; resolve its branch provenance explicitly")
            continue
        if references:
            text = references.sub(lambda match: renames[match.group()], entry["text"])
            entry = parse_ledger(text, label="renamed incoming entry")[0]
        combined.append(entry)
    # Stable sorting retains branch order and puts target additions first on ties.
    combined.sort(key=lambda entry: entry["timestamp"])
    text = "\n\n".join(entry["text"] for entry in combined)
    if prefix:
        text = prefix + ("\n\n" + text if text else "")
    if text:
        text += "\n"
    parse_ledger(text, label="merged ledger")
    return text, renames


def git_run(directory, *arguments):
    return subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True,
        encoding="utf-8", errors="strict", check=False,
    )


def repository_path(path):
    root_result = git_run(path.parent, "rev-parse", "--show-toplevel")
    if root_result.returncode:
        raise LedgerError("staged validation requires a Git working tree")
    root = Path(root_result.stdout.strip()).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise LedgerError("ledger must be inside the Git working tree") from exc
    return root, relative


def staged_content(path, *, if_present=False):
    root, relative = repository_path(path)
    entries = git_run(root, "ls-files", "--stage", "--", relative)
    if entries.returncode:
        raise LedgerError(entries.stderr.strip() or "cannot inspect Git index")
    if not entries.stdout:
        previous = git_run(root, "cat-file", "-e", f"HEAD:{relative}")
        if previous.returncode == 0:
            raise LedgerError("ledger is deleted from the index; restore it before committing")
        if if_present:
            return None
        raise LedgerError("ledger is not present in the Git index")
    snapshot = git_run(root, "show", f":{relative}")
    if snapshot.returncode:
        raise LedgerError("cannot read staged ledger; resolve any merge conflicts first")
    return snapshot.stdout


def conflict_contents(path):
    root, relative = repository_path(path)
    snapshots = [git_run(root, "show", f":{stage}:{relative}") for stage in (1, 2, 3)]
    if any(snapshot.returncode for snapshot in snapshots[1:]):
        raise LedgerError("merge --staged requires both ours and theirs conflict stages; deletion conflicts require explicit review")
    # An add/add conflict has no common file, hence no stage-1 snapshot.
    return [snapshot.stdout if snapshot.returncode == 0 else "" for snapshot in snapshots]


def public(entry, *, full=False):
    result = {key: entry[key] for key in ("id", "title", "timestamp", "line")}
    if full:
        result["text"] = entry["text"]
    return result


def emit_entries(entries, *, as_json=False):
    if as_json:
        print(json.dumps([public(entry) for entry in entries], ensure_ascii=False, indent=2))
    else:
        for entry in entries:
            print(entry["text"].splitlines()[0])


def positive_integer(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--file", default="LEDGER.md", help="ledger path (default: LEDGER.md)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create an empty ledger without overwriting an existing file")
    commands.add_parser("read", help="read the whole validated ledger")
    append_parser = commands.add_parser("append", help="append a new entry without changing existing text")
    append_parser.add_argument("title")
    bodies = append_parser.add_mutually_exclusive_group(required=True)
    bodies.add_argument("--body", help="free-form entry body")
    bodies.add_argument("--body-file", help="UTF-8 body file, or - for stdin")
    append_parser.add_argument("--id", help="preserve an ID when replaying an entry; otherwise generated randomly")
    append_parser.add_argument("--timestamp", help="preserve a UTC timestamp when replaying; otherwise current UTC")
    append_parser.add_argument("--json", action="store_true")
    for command in ("list", "search"):
        listing = commands.add_parser(command, help="list entry headings" if command == "list" else "search full entry contents")
        if command == "search":
            listing.add_argument("query", help="case-insensitive substring to find")
        else:
            listing.add_argument("--search", help="case-insensitive full-text filter")
        listing.add_argument("--recent", type=positive_integer, help="return the last N matching entries")
        listing.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="read a complete entry by stable ID")
    show.add_argument("item_id")
    show.add_argument("--json", action="store_true")
    validation = commands.add_parser("validate", help="check headers, unique IDs, and non-decreasing timestamps")
    validation.add_argument("--staged", action="store_true", help="validate the Git index instead of the working copy")
    validation.add_argument("--if-present", action="store_true", help="allow initial absence, but not staged deletion")
    merging = commands.add_parser("merge", help="resolve a three-way merge of ledger snapshots")
    merging.add_argument("--staged", action="store_true", help="read the base, ours, and theirs from Git's unresolved conflict stages")
    for side in ("base", "ours", "theirs"):
        merging.add_argument(f"--{side}", help=f"path to the {side} ledger snapshot; supply all three instead of --staged")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    path = Path(args.file).resolve()
    try:
        if args.command == "init":
            with lock(path):
                if path.exists():
                    raise LedgerError(f"refusing to overwrite existing file: {path}")
                save(path, "")
            print(f"Initialized {path}")
        elif args.command == "append":
            body = args.body
            if args.body_file is not None:
                body = sys.stdin.read() if args.body_file == "-" else read_text(Path(args.body_file))
            entry = append(path, title=args.title, body=body, item_id=args.id, timestamp=args.timestamp)
            if args.json:
                print(json.dumps(public(entry, full=True), ensure_ascii=False, indent=2))
            else:
                emit_entries([entry])
        elif args.command == "merge":
            files = (args.base, args.ours, args.theirs)
            if (args.staged and any(files)) or (not args.staged and not all(files)):
                raise LedgerError("merge requires either --staged or all of --base, --ours, and --theirs")
            with lock(path):
                snapshots = conflict_contents(path) if args.staged else [read_text(Path(value).resolve()) for value in files]
                content, renames = merge_contents(*snapshots)
                save(path, content)
            print(f"Merged and validated {path}")
            for old, new in renames.items():
                print(f"Renamed incoming {old} -> {new}")
        elif args.command == "validate" and args.staged:
            content = staged_content(path, if_present=args.if_present)
            if content is None:
                print("No ledger to validate")
            else:
                entries = parse_ledger(content, label=str(path))
                print(f"Ledger is valid ({len(entries)} entries)")
        elif args.command == "validate" and args.if_present and not path.exists():
            print("No ledger to validate")
        else:
            with lock(path):
                content = read_text(path)
                entries = parse_ledger(content, label=str(path))
            if args.command == "validate":
                print(f"Ledger is valid ({len(entries)} entries)")
            elif args.command == "read":
                print(content, end="")
            elif args.command == "show":
                entry = next((entry for entry in entries if entry["id"] == args.item_id), None)
                if entry is None:
                    raise LedgerError(f"unknown ledger ID: {args.item_id}")
                if args.json:
                    print(json.dumps(public(entry, full=True), ensure_ascii=False, indent=2))
                else:
                    print(entry["text"])
            else:
                query = args.query if args.command == "search" else args.search
                if query is not None:
                    entries = [entry for entry in entries if query.casefold() in entry["text"].casefold()]
                if args.recent is not None:
                    entries = entries[-args.recent:]
                emit_entries(entries, as_json=args.json)
    except (LedgerError, OSError, UnicodeError, ValueError) as exc:
        print(f"ledger: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
