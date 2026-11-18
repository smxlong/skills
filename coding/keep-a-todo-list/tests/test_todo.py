"""Behavioral tests for integrity, dependency rules, concurrency, and Git hooks."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "todo.py"
HOOK = SKILL / "scripts" / "pre-commit"
SPEC = importlib.util.spec_from_file_location("todo", SCRIPT)
todo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(todo)


class TodoCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "TODO.json"
        self.run_cli("init")

    def run_cli(self, *arguments, success=True, path=None, input=None):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(SCRIPT), "--file", str(path or self.path), *arguments],
            capture_output=True, encoding="utf-8", input=input, cwd=self.root,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def create(self, title, *arguments, path=None):
        return json.loads(self.run_cli("create", title, *arguments, "--json", path=path).stdout)["id"]

    def items(self, *arguments):
        return json.loads(self.run_cli("list", *arguments, "--json").stdout)

    def item(self, item_id):
        return json.loads(self.run_cli("show", item_id, "--json").stdout)

    def rejected_without_change(self, *arguments):
        before = self.path.read_bytes()
        result = self.run_cli(*arguments, success=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.run_cli("validate")
        return result


class TodoTests(TodoCase):
    def test_free_form_details_round_trip_and_stable_ids(self):
        details = "Context: café\n\nThe user asked for this.\n- Preserve spacing.\n"
        created = self.run_cli("create", "Handle sessions", "--details-file", "-", "--json", input=details)
        item_id = json.loads(created.stdout)["id"]
        self.run_cli("update", item_id, "--status", "in_progress", "--title", "Refresh sessions")
        item = self.item(item_id)
        self.assertEqual(item["details"], details)
        self.assertEqual(item["title"], "Refresh sessions")
        self.assertEqual(item["id"], item_id)
        self.assertEqual(item["status"], "in_progress")
        self.run_cli("update", item_id, "--details", "")
        self.assertEqual(self.item(item_id)["details"], "")

    def test_completion_requires_every_dependency(self):
        first = self.create("First prerequisite")
        second = self.create("Second prerequisite")
        target = self.create("Target", "--depends-on", first, "--depends-on", second)
        self.rejected_without_change("complete", target)
        self.run_cli("complete", first)
        self.rejected_without_change("close", target)
        self.run_cli("update", second, "--status", "paused")
        self.rejected_without_change("update", target, "--status", "completed")
        self.run_cli("complete", second)
        self.run_cli("close", target)
        self.assertEqual(self.item(target)["status"], "completed")

    def test_abandonment_requires_resolving_open_dependents(self):
        prerequisite = self.create("Prerequisite")
        target = self.create("Target", "--depends-on", prerequisite)
        self.run_cli("update", target, "--status", "paused")
        self.rejected_without_change("abandon", prerequisite)
        self.rejected_without_change("update", prerequisite, "--status", "rejected")
        self.run_cli("abandon", target)
        self.assertEqual(self.item(target)["status"], "abandoned")
        self.run_cli("abandon", prerequisite)
        self.assertEqual(self.item(prerequisite)["status"], "rejected")
        self.assertEqual(self.items(), [])
        self.assertEqual(len(self.items("--all")), 2)

    def test_open_item_cannot_gain_discarded_dependency(self):
        discarded = self.create("Discarded")
        self.run_cli("abandon", discarded)
        self.rejected_without_change("create", "New work", "--depends-on", discarded)
        target = self.create("Existing work")
        self.rejected_without_change("update", target, "--depends-on", discarded)

    def test_abandon_todo_normalizes_to_rejected(self):
        item_id = self.create("Never started")
        self.run_cli("update", item_id, "--status", "abandoned")
        self.assertEqual(self.item(item_id)["status"], "rejected")
        self.run_cli("update", item_id, "--status", "in_progress")
        self.rejected_without_change("update", item_id, "--status", "rejected")
        self.run_cli("abandon", item_id)
        self.assertEqual(self.item(item_id)["status"], "abandoned")

    def test_reopening_preserves_completed_dependency_invariant(self):
        prerequisite = self.create("Prerequisite")
        target = self.create("Target", "--depends-on", prerequisite)
        self.run_cli("complete", prerequisite)
        self.run_cli("complete", target)
        self.rejected_without_change("update", prerequisite, "--status", "todo")
        self.run_cli("update", target, "--status", "todo")
        self.run_cli("update", prerequisite, "--status", "todo")

    def test_missing_duplicate_and_cyclic_dependencies_leave_file_unchanged(self):
        first = self.create("First")
        second = self.create("Second", "--depends-on", first)
        self.rejected_without_change("update", first, "--depends-on", "T-00000000")
        self.rejected_without_change("update", second, "--depends-on", first, "--depends-on", first)
        self.rejected_without_change("update", first, "--depends-on", first)
        self.rejected_without_change("update", first, "--depends-on", second)
        self.run_cli("update", second, "--clear-dependencies")
        self.run_cli("update", first, "--depends-on", second)

    def test_filters_and_dependency_traversal(self):
        first = self.create("Session foundation", "--details", "Café setup")
        second = self.create("Refresh", "--depends-on", first)
        third = self.create("Recovery", "--depends-on", second)
        paused = self.create("Later")
        self.run_cli("update", paused, "--status", "paused")
        self.assertEqual([item["id"] for item in self.items("--ready")], [first])
        self.assertEqual({item["id"] for item in self.items("--blocked")}, {second, third})
        self.assertEqual([item["id"] for item in self.items("--search", "CAFÉ")], [first])
        self.assertEqual([item["id"] for item in self.items("--status", "paused")], [paused])
        self.assertEqual([item["id"] for item in self.items("--depends-on", first)], [second])
        direct = json.loads(self.run_cli("deps", third, "--json").stdout)
        ancestors = json.loads(self.run_cli("deps", third, "--transitive", "--json").stdout)
        descendants = json.loads(self.run_cli("dependents", first, "--transitive", "--json").stdout)
        self.assertEqual([item["id"] for item in direct], [second])
        self.assertEqual({item["id"] for item in ancestors}, {first, second})
        self.assertEqual({item["id"] for item in descendants}, {second, third})
        self.run_cli("complete", first)
        self.assertEqual([item["id"] for item in self.items("--ready")], [second])

    def test_manual_edits_are_detected_and_never_blessed_by_mutation(self):
        self.create("First", "--details", "Original details")
        second = self.create("Second")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        for field, replacement in (
            ("title", "Edited title"), ("details", "Edited details"),
            ("status", "paused"), ("depends_on", [second]),
        ):
            with self.subTest(field=field):
                document = copy.deepcopy(original)
                document["items"][0][field] = replacement
                self.path.write_text(json.dumps(document), encoding="utf-8")
                corrupt = self.path.read_bytes()
                self.run_cli("validate", success=False)
                self.run_cli("update", second, "--details", "Unrelated update", success=False)
                self.run_cli("create", "Another item", success=False)
                self.run_cli("init", success=False)
                self.assertEqual(self.path.read_bytes(), corrupt)

    def test_collection_signature_detects_deletion_reordering_and_insertion(self):
        self.create("First")
        self.create("Second")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        deleted = copy.deepcopy(original)
        deleted["items"].pop()
        reordered = copy.deepcopy(original)
        reordered["items"].reverse()
        inserted = copy.deepcopy(original)
        added = copy.deepcopy(inserted["items"][0])
        added["id"] = "T-1234abcd"
        added["signature"] = todo.digest("item", todo.unsigned(added))
        inserted["items"].append(added)
        for document in (deleted, reordered, inserted):
            self.path.write_text(json.dumps(document), encoding="utf-8")
            self.run_cli("validate", success=False)

    def test_json_presentation_is_not_signed_but_duplicate_keys_are_rejected(self):
        item_id = self.create("A title", "--details", "Multiple\nlines\n")
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        self.run_cli("validate")
        self.assertEqual(self.item(item_id)["details"], "Multiple\nlines\n")
        content = self.path.read_text(encoding="utf-8").replace("{", '{"version": 1,', 1)
        self.path.write_text(content, encoding="utf-8")
        self.run_cli("validate", success=False)

    def test_invalid_json_constants_are_rejected(self):
        self.create("Original")
        original = self.path.read_text(encoding="utf-8")
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                self.path.write_text(original.replace('"version": 1', f'"version": {constant}'), encoding="utf-8")
                self.run_cli("validate", success=False)

    def test_invalid_fields_and_noop_updates_are_rejected(self):
        item_id = self.create("Original")
        self.rejected_without_change("update", item_id)
        self.rejected_without_change("update", item_id, "--title", "\n")
        self.rejected_without_change("create", "Duplicate", "--id", item_id)
        self.rejected_without_change("create", "Bad ID", "--id", "T-short")
        self.rejected_without_change("complete", "T-00000000")

    def test_concurrent_creates_do_not_lose_updates(self):
        processes = [subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(SCRIPT), "--file", str(self.path), "create", f"Item {index}", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
        ) for index in range(8)]
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
            self.assertIn("id", json.loads(output))
        items = self.items()
        self.assertEqual(len(items), 8)
        self.assertEqual(len({item["id"] for item in items}), 8)
        self.run_cli("validate")

    def test_failed_atomic_replace_preserves_previous_file(self):
        self.create("Original")
        original = self.path.read_bytes()
        document = todo.load(self.path)
        document["items"][0]["details"] = "Updated"
        with mock.patch.object(todo.os, "replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                todo.save(self.path, document)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".TODO.json.*")), [])

    def test_lock_timeout_preserves_existing_owner_and_file(self):
        directory = self.path.with_name(f".{self.path.name}.lock")
        original = self.path.read_bytes()
        with todo.lock(self.path):
            owner = (directory / "owner.json").read_bytes()
            with self.assertRaises(todo.TodoError):
                with todo.lock(self.path, timeout=0.02):
                    self.fail("a second caller acquired an existing lock")
            self.assertEqual((directory / "owner.json").read_bytes(), owner)
            self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(directory.exists())
        with todo.lock(self.path, timeout=0):
            pass

    def test_exception_releases_lock(self):
        with self.assertRaises(RuntimeError):
            with todo.lock(self.path):
                raise RuntimeError("operation failed")
        with todo.lock(self.path, timeout=0):
            pass

    def test_stale_lock_requires_explicit_recovery(self):
        directory = self.path.with_name(f".{self.path.name}.lock")
        directory.mkdir()
        owner = directory / "owner.json"
        owner.write_text('{"pid": 123456789, "host": "previous-writer"}', encoding="utf-8")
        with self.assertRaises(todo.TodoError):
            with todo.lock(self.path, timeout=0):
                self.fail("an existing lock was stolen")
        self.assertTrue(owner.exists())
        owner.unlink()
        directory.rmdir()
        self.create("Recovered")

    def snapshots(self):
        paths = [self.root / f"{name}.json" for name in ("base", "ours", "theirs")]
        for path in paths:
            shutil.copyfile(self.path, path)
        return paths

    def run_merge(self, paths, success=True):
        arguments = ["merge"]
        for name, path in zip(("base", "ours", "theirs"), paths):
            arguments.extend(("--" + name, str(path)))
        return self.run_cli(*arguments, success=success)

    def test_merge_verified_snapshots_combines_independent_work(self):
        original = self.create("Original")
        paths = self.snapshots()
        left = self.create("Left", path=paths[1])
        right = self.create("Right", path=paths[2])
        self.path.write_text("<<<<<<< ours\ninvalid conflict text\n=======\n>>>>>>> theirs\n", encoding="utf-8")
        self.run_merge(paths)
        self.assertEqual({item["id"] for item in self.items()}, {original, left, right})
        self.run_cli("validate")

    def test_merge_same_item_conflict_and_corrupt_source_preserve_destination(self):
        item_id = self.create("Original")
        paths = self.snapshots()
        self.run_cli("update", item_id, "--title", "Left", path=paths[1])
        self.run_cli("update", item_id, "--title", "Right", path=paths[2])
        original = self.path.read_bytes()
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), original)
        paths[1].write_text(paths[1].read_text(encoding="utf-8").replace("Left", "Manual edit"), encoding="utf-8")
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_merge_rejects_combined_cycle(self):
        first = self.create("First")
        second = self.create("Second")
        paths = self.snapshots()
        self.run_cli("update", first, "--depends-on", second, path=paths[1])
        self.run_cli("update", second, "--depends-on", first, path=paths[2])
        original = self.path.read_bytes()
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), original)


class HookTests(TodoCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(SCRIPT, scripts / "todo.py")

    def git(self, *arguments):
        result = subprocess.run(["git", *arguments], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hook(self, success=True):
        environment = os.environ.copy()
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(HOOK)], cwd=self.root,
            capture_output=True, encoding="utf-8", env=environment,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_hook_validates_staged_snapshot_not_working_copy(self):
        item_id = self.create("Original")
        valid = self.path.read_bytes()
        self.git("add", "TODO.json")
        self.path.write_text(self.path.read_text(encoding="utf-8").replace("Original", "Corrupt working copy"), encoding="utf-8")
        self.hook()
        self.run_cli("validate", success=False)
        self.git("add", "TODO.json")
        self.path.write_bytes(valid)
        self.hook(success=False)
        self.assertEqual(self.item(item_id)["title"], "Original")

    def test_hook_allows_initial_absence_but_rejects_staged_deletion(self):
        self.hook()
        self.git("add", "TODO.json")
        self.git("-c", "user.name=TODO Tests", "-c", "user.email=todo-tests@example.invalid", "commit", "-qm", "Add TODO list")
        self.git("rm", "TODO.json")
        self.hook(success=False)

    def test_hook_rejects_unmerged_index_even_with_valid_working_file(self):
        self.git("add", "TODO.json")
        blob = self.git("hash-object", "TODO.json").stdout.strip()
        self.git("update-index", "--force-remove", "TODO.json")
        lines = "".join(f"100644 {blob} {stage}\tTODO.json\n" for stage in (1, 2, 3))
        result = subprocess.run(
            ["git", "update-index", "--index-info"], input=lines, text=True,
            capture_output=True, cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.run_cli("validate")
        self.hook(success=False)


if __name__ == "__main__":
    unittest.main()
