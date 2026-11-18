#!/usr/bin/env python3
"""Manage a signed JSON TODO list using only the Python 3.10+ standard library."""

from __future__ import annotations

import argparse
import copy
from collections import deque
from contextlib import contextmanager
import hashlib
import hmac
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


STATUSES = ("todo", "in_progress", "paused", "completed", "abandoned", "rejected")
OPEN = frozenset(("todo", "in_progress", "paused"))
DISCARDED = frozenset(("abandoned", "rejected"))
ID_RE = re.compile(r"T-[0-9a-f]{8}\Z")
SIGNATURE_RE = re.compile(r"[0-9a-f]{64}\Z")
ITEM_KEYS = frozenset(("id", "title", "details", "status", "depends_on", "signature"))


class TodoError(Exception):
    """A failed operation that must leave the list unchanged."""


def unique_mapping(pairs):
    """Do not let duplicate JSON keys silently override signed content."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise TodoError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise TodoError(f"invalid JSON constant: {value}")


def digest(kind, value):
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"keep-a-todo-list:v1:" + kind.encode() + b"\0" + canonical).hexdigest()


def unsigned(value):
    return {key: content for key, content in value.items() if key != "signature"}


def seal(document):
    for item in document["items"]:
        item["signature"] = digest("item", unsigned(item))
    document["signature"] = digest("document", unsigned(document))


def check_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise TodoError(f"{label} must contain exactly: {', '.join(sorted(keys))}")


def validate(document, *, signatures=True):
    check_keys(document, {"version", "items", "signature"}, "document")
    if type(document["version"]) is not int or document["version"] != 1:
        raise TodoError("unsupported TODO format version; expected integer 1")
    if not isinstance(document["items"], list):
        raise TodoError("items must be a list")
    if not isinstance(document["signature"], str) or not SIGNATURE_RE.fullmatch(document["signature"]):
        raise TodoError("document has a missing or malformed signature")

    by_id = {}
    for item in document["items"]:
        check_keys(item, ITEM_KEYS, "item")
        item_id = item["id"]
        if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
            raise TodoError("item IDs must be T- followed by eight lowercase hex digits")
        if item_id in by_id:
            raise TodoError(f"duplicate item ID: {item_id}")
        by_id[item_id] = item
        title = item["title"]
        if not isinstance(title, str) or not title.strip() or any(c in title for c in "\r\n\t"):
            raise TodoError(f"{item_id}: title must be a nonempty single line without tabs")
        if not isinstance(item["details"], str):
            raise TodoError(f"{item_id}: details must be a string")
        if not isinstance(item["status"], str) or item["status"] not in STATUSES:
            raise TodoError(f"{item_id}: invalid status")
        dependencies = item["depends_on"]
        if not isinstance(dependencies, list) or any(
            not isinstance(dep, str) or not ID_RE.fullmatch(dep) for dep in dependencies
        ):
            raise TodoError(f"{item_id}: depends_on must be a list of TODO IDs")
        if len(set(dependencies)) != len(dependencies):
            raise TodoError(f"{item_id}: duplicate dependencies")
        signature = item["signature"]
        if not isinstance(signature, str) or not SIGNATURE_RE.fullmatch(signature):
            raise TodoError(f"{item_id}: missing or malformed signature")
        if signatures and not hmac.compare_digest(signature, digest("item", unsigned(item))):
            raise TodoError(f"{item_id}: integrity check failed; restore a verified version and replay changes through this script")

    if signatures and not hmac.compare_digest(
        document["signature"], digest("document", unsigned(document))
    ):
        raise TodoError("document integrity check failed; restore a verified version and replay changes through this script")

    reverse = {item_id: [] for item_id in by_id}
    remaining = {}
    for item in document["items"]:
        item_id = item["id"]
        remaining[item_id] = len(item["depends_on"])
        for dependency in item["depends_on"]:
            if dependency not in by_id:
                raise TodoError(f"{item_id}: missing dependency {dependency}")
            reverse[dependency].append(item_id)
            dependency_status = by_id[dependency]["status"]
            if item["status"] == "completed" and dependency_status != "completed":
                raise TodoError(f"{item_id}: completed item requires completed dependency {dependency}")
            if item["status"] in OPEN and dependency_status in DISCARDED:
                raise TodoError(f"{item_id}: open item depends on {dependency_status} item {dependency}; update or discard the dependent first")

    queue = deque(item_id for item_id, count in remaining.items() if count == 0)
    visited = 0
    while queue:
        visited += 1
        for dependent in reverse[queue.popleft()]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                queue.append(dependent)
    if visited != len(by_id):
        unresolved = ", ".join(item_id for item_id, count in remaining.items() if count)
        raise TodoError(f"dependency cycle detected; unresolved items: {unresolved}")
    return by_id


def parse_document(content):
    try:
        document = json.loads(
            content, object_pairs_hook=unique_mapping, parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise TodoError(f"invalid TODO JSON: {exc}") from exc
    validate(document)
    return document


def load(path):
    return parse_document(path.read_text(encoding="utf-8"))


@contextmanager
def lock(path, *, timeout=10.0):
    # mkdir is an exclusive creation operation on Windows, macOS, and Linux.
    # Never steal a lock based on age: a slow writer may still be using it.
    directory = path.with_name(f".{path.name}.lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            directory.mkdir()
            break
        except FileExistsError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TodoError(
                    f"timed out waiting for TODO lock {directory}; inspect owner.json "
                    "and remove the lock directory only after confirming no writer is active"
                ) from None
            time.sleep(min(0.05, remaining))
    owner = directory / "owner.json"
    try:
        owner.write_text(json_text({"pid": os.getpid(), "host": socket.gethostname()}), encoding="utf-8")
        yield
    finally:
        owner.unlink(missing_ok=True)
        directory.rmdir()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def save(path, document):
    validate(document, signatures=False)
    seal(document)
    content = json_text(document)
    # Validate serialization too, before replacing the last valid file.
    parse_document(content)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def lookup(document, item_id):
    for item in document["items"]:
        if item["id"] == item_id:
            return item
    raise TodoError(f"unknown TODO: {item_id}")


def make_item(document, args):
    existing = {item["id"] for item in document["items"]}
    item_id = args.id
    if item_id is None:
        item_id = "T-" + secrets.token_hex(4)
        while item_id in existing:
            item_id = "T-" + secrets.token_hex(4)
    elif item_id in existing:
        raise TodoError(f"item ID already exists: {item_id}")
    return {
        "id": item_id, "title": args.title, "details": args.details or "",
        "status": "todo", "depends_on": args.depends_on or [], "signature": "0" * 64,
    }


def change_item(item, args):
    if args.command == "complete":
        status = "completed"
    elif args.command == "abandon":
        if item["status"] not in OPEN:
            raise TodoError(f"{item['id']}: only open items can be abandoned")
        status = "rejected" if item["status"] == "todo" else "abandoned"
    else:
        status = args.status

    if status == "rejected" and item["status"] not in ("todo", "rejected"):
        raise TodoError("rejected is reserved for an item discarded while still todo")
    if status == "abandoned" and item["status"] == "todo":
        status = "rejected"
    if status is not None:
        item["status"] = status
    for field in ("title", "details"):
        value = getattr(args, field, None)
        if value is not None:
            item[field] = value
    if args.command == "update":
        if args.depends_on is not None:
            item["depends_on"] = args.depends_on
        elif args.clear_dependencies:
            item["depends_on"] = []


def mutate(path, args):
    with lock(path):
        if args.command == "init":
            if path.exists():
                raise TodoError(f"refusing to overwrite existing file: {path}")
            document = {"version": 1, "items": [], "signature": "0" * 64}
            result = None
        else:
            document = load(path)  # Never bless preexisting corruption.
            if args.command == "create":
                result = make_item(document, args)
                document["items"].append(result)
            else:
                result = lookup(document, args.item_id)
                change_item(result, args)
        save(path, document)
    return result


def merge_documents(base, ours, theirs):
    maps = [{item["id"]: item for item in doc["items"]} for doc in (base, ours, theirs)]
    original, local, incoming = maps
    for item_id in original:
        if item_id not in local or item_id not in incoming:
            raise TodoError(f"merge removed {item_id}; retain items and use a terminal status")
    merged = []
    order = list(local) + [item_id for item_id in incoming if item_id not in local]
    for item_id in order:
        old, left, right = (mapping.get(item_id) for mapping in maps)
        if left == right or right == old:
            chosen = left
        elif left == old:
            chosen = right
        else:
            raise TodoError(f"merge conflict in {item_id}; restore a verified side and replay the intended changes through commands")
        merged.append(copy.deepcopy(chosen))
    document = {"version": 1, "items": merged, "signature": "0" * 64}
    validate(document, signatures=False)
    return document


def merge(path, args):
    with lock(path):
        # Explicit recovery operation: validate all three source snapshots.
        # The destination may contain Git conflict markers.
        sources = [load(Path(value).resolve()) for value in (args.base, args.ours, args.theirs)]
        document = merge_documents(*sources)
        save(path, document)


def related(document, item_id, *, reverse=False, transitive=False):
    by_id = {item["id"]: item for item in document["items"]}
    lookup(document, item_id)
    edges = {key: list(item["depends_on"]) for key, item in by_id.items()}
    if reverse:
        edges = {key: [] for key in by_id}
        for item in document["items"]:
            for dependency in item["depends_on"]:
                edges[dependency].append(item["id"])
    seen = set()
    queue = deque(edges[item_id])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        if transitive:
            queue.extend(edges[current])
    return [item for item in document["items"] if item["id"] in seen]


def filter_items(document, args):
    by_id = {item["id"]: item for item in document["items"]}
    if args.depends_on:
        lookup(document, args.depends_on)
    statuses = set(args.status) if args.status else (set(STATUSES) if args.all else OPEN)
    result = []
    for item in document["items"]:
        if item["status"] not in statuses:
            continue
        complete = all(by_id[dep]["status"] == "completed" for dep in item["depends_on"])
        if args.ready and not (item["status"] == "todo" and complete):
            continue
        if args.blocked and not (item["status"] in OPEN and not complete):
            continue
        if args.depends_on and args.depends_on not in item["depends_on"]:
            continue
        if args.search and args.search.casefold() not in (
            item["id"] + "\n" + item["title"] + "\n" + item["details"]
        ).casefold():
            continue
        result.append(item)
    return result


def git_run(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True,
        encoding="utf-8", errors="strict", check=False,
    )


def validate_staged(path, *, if_present=False):
    root_result = git_run(path.parent, "rev-parse", "--show-toplevel")
    if root_result.returncode:
        raise TodoError("staged validation requires a Git working tree")
    root = Path(root_result.stdout.strip()).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise TodoError("TODO file must be inside the Git working tree") from exc
    # Distinguish absence from unresolved merge stages and intentional deletion.
    entries = git_run(root, "ls-files", "--stage", "--", relative)
    if entries.returncode:
        raise TodoError(entries.stderr.strip() or "cannot inspect Git index")
    if not entries.stdout:
        previous = git_run(root, "cat-file", "-e", f"HEAD:{relative}")
        if previous.returncode == 0:
            raise TodoError("TODO file is deleted from the index; restore it before committing")
        if if_present:
            return False
        raise TodoError("TODO file is not present in the Git index")
    snapshot = git_run(root, "show", f":{relative}")
    if snapshot.returncode:
        raise TodoError("cannot read staged TODO file; resolve any merge conflicts first")
    parse_document(snapshot.stdout)
    return True


def emit(value, *, as_json=False, summary=False):
    if isinstance(value, list):
        public = [unsigned(item) for item in value]
    else:
        public = unsigned(value)
    if summary and not as_json:
        if not public:
            print("(no items)")
        for item in public:
            print(f"{item['id']}\t{item['status']}\t{item['title']}")
    else:
        print(json_text(public), end="")


def details_options(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--details", help="free-form details; replaces existing details on update")
    group.add_argument("--details-file", help="read UTF-8 details from a file, or - for stdin")


def output_options(parser):
    parser.add_argument("--json", action="store_true", help="emit JSON without integrity metadata")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--file", default="TODO.json", help="TODO file (default: TODO.json in the current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create a new, empty signed list")

    create = commands.add_parser("create", help="create a todo item")
    create.add_argument("title")
    create.add_argument("--id", help="preserve a known ID when replaying a verified branch")
    details_options(create)
    create.add_argument("--depends-on", action="append", help="dependency ID; repeat for multiple dependencies")
    output_options(create)

    listing = commands.add_parser("list", help="list open items by default")
    scope = listing.add_mutually_exclusive_group()
    scope.add_argument("--open", action="store_true", help="todo, in_progress, and paused (default)")
    scope.add_argument("--all", action="store_true", help="include terminal statuses")
    scope.add_argument("--status", choices=STATUSES, action="append", help="status filter; repeat to include several")
    availability = listing.add_mutually_exclusive_group()
    availability.add_argument("--ready", action="store_true", help="todo items whose dependencies are all completed")
    availability.add_argument("--blocked", action="store_true", help="open items with incomplete dependencies")
    listing.add_argument("--search", help="case-insensitive search of IDs, titles, and details")
    listing.add_argument("--depends-on", help="items directly depending on this ID")
    output_options(listing)

    for command in ("show", "deps", "dependents"):
        sub = commands.add_parser(command, help={
            "show": "show one item's full contents",
            "deps": "list an item's dependencies",
            "dependents": "list items depending on an item",
        }[command])
        sub.add_argument("item_id")
        if command != "show":
            sub.add_argument("--transitive", action="store_true", help="include the full dependency chain")
        output_options(sub)

    update = commands.add_parser("update", help="update content, status, or dependencies")
    update.add_argument("item_id")
    update.add_argument("--title")
    details_options(update)
    update.add_argument("--status", choices=STATUSES)
    dependencies = update.add_mutually_exclusive_group()
    dependencies.add_argument("--depends-on", action="append", help="replace the dependency list; repeat for each ID")
    dependencies.add_argument("--clear-dependencies", action="store_true")
    output_options(update)

    complete = commands.add_parser("complete", aliases=["close"], help="mark completed after all dependencies complete")
    complete.add_argument("item_id")
    details_options(complete)
    output_options(complete)
    abandon = commands.add_parser("abandon", help="reject an unstarted todo, or abandon other open work")
    abandon.add_argument("item_id")
    details_options(abandon)
    output_options(abandon)

    validation = commands.add_parser("validate", help="check schema, signatures, and dependency rules")
    validation.add_argument("--staged", action="store_true", help="validate the Git index instead of the working file")
    validation.add_argument("--if-present", action="store_true", help="allow absence, but not staged deletion of an existing list")

    merging = commands.add_parser("merge", help="merge three verified snapshots, preserving IDs")
    for side in ("base", "ours", "theirs"):
        merging.add_argument(f"--{side}", required=True, help=f"path to the {side} signed JSON snapshot")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "close":
        args.command = "complete"
    path = Path(args.file).resolve()
    try:
        if getattr(args, "details_file", None) is not None:
            args.details = sys.stdin.read() if args.details_file == "-" else Path(args.details_file).read_text(encoding="utf-8")
        if args.command in ("init", "create", "update", "complete", "abandon"):
            if args.command == "update" and not any((
                args.title is not None, args.details is not None, args.status is not None,
                args.depends_on is not None, args.clear_dependencies,
            )):
                raise TodoError("update requires at least one changed field")
            item = mutate(path, args)
            if item is None:
                print(f"Initialized {path}")
            else:
                emit(item, as_json=args.json)
        elif args.command == "merge":
            merge(path, args)
            print(f"Merged and validated {path}")
        elif args.command == "validate":
            if args.staged:
                present = validate_staged(path, if_present=args.if_present)
            elif args.if_present and not path.exists():
                present = False
            else:
                with lock(path):
                    load(path)
                present = True
            print("TODO list is valid" if present else "No TODO list to validate")
        else:
            # Serialize working-file readers too, so Windows writers can replace
            # the destination without another script process holding it open.
            with lock(path):
                document = load(path)
            if args.command == "list":
                emit(filter_items(document, args), as_json=args.json, summary=True)
            elif args.command == "show":
                emit(lookup(document, args.item_id), as_json=args.json)
            else:
                emit(related(
                    document, args.item_id, reverse=args.command == "dependents",
                    transitive=args.transitive,
                ), as_json=args.json, summary=True)
    except (TodoError, OSError, UnicodeError, ValueError, RecursionError) as exc:
        print(f"todo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
