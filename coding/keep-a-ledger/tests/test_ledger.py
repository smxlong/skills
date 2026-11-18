"""Behavioral tests for ledger operations, three-way merges, and Git hooks."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "ledger.py"
HOOK = SKILL / "scripts" / "pre-commit"
SPEC = importlib.util.spec_from_file_location("ledger", SCRIPT)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)
STAMP = "2026-11-17T12:00:00Z"


def entry(item_id="L-a7c91e2f", title="A decision", timestamp=STAMP, body="Useful context."):
    return f"# {item_id} {title} {timestamp}\n\n{body}\n"


class HeaderTests(unittest.TestCase):
    def test_valid_headers_allow_legacy_ids_and_equal_timestamps(self):
        content = entry("L-0001") + "\n" + entry("L-12345") + "\n" + entry("L-abcdef12")
        parsed = ledger.parse_ledger(content)
        self.assertEqual([item["id"] for item in parsed], ["L-0001", "L-12345", "L-abcdef12"])
        self.assertEqual(ledger.parse_ledger(""), [])

    def test_malformed_headers_are_rejected(self):
        for heading in (
            "# Ledger",
            "#L-abcdef12 Missing space " + STAMP,
            " # L-abcdef12 Indented " + STAMP,
            "# L-abc Short ID " + STAMP,
            "# L-ABCDEF12 Uppercase " + STAMP,
            "# L-abcdef12  " + STAMP,
            "# L-abcdef12 Title  " + STAMP,
            "# L-abcdef12 Title\twith tab " + STAMP,
            "# L-abcdef12 Title " + STAMP + " ",
            "# L-abcdef12 Offset 2026-11-17T12:00:00+00:00",
            "# L-abcdef12 Fraction 2026-11-17T12:00:00.001Z",
            "# L-abcdef12 No zero padding 2026-1-17T12:00:00Z",
        ):
            with self.subTest(heading=heading):
                with self.assertRaises(ledger.LedgerError):
                    ledger.parse_ledger(heading + "\n\nContext.\n")

    def test_invalid_calendar_times_are_rejected(self):
        for timestamp in ("2026-02-29T12:00:00Z", "2026-13-01T12:00:00Z", "2026-11-17T24:00:00Z"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ledger.LedgerError):
                    ledger.parse_ledger(entry(timestamp=timestamp))
        ledger.parse_ledger(entry(timestamp="2024-02-29T12:00:00Z"))

    def test_duplicate_ids_report_both_locations(self):
        with self.assertRaises(ledger.LedgerError) as raised:
            ledger.parse_ledger(entry() + "\n" + entry(title="Different title"))
        self.assertIn(":5:", str(raised.exception))
        self.assertIn("line 1", str(raised.exception))

    def test_decreasing_timestamps_are_rejected(self):
        content = entry(timestamp="2026-11-17T12:00:01Z") + "\n" + entry("L-0002")
        with self.assertRaises(ledger.LedgerError):
            ledger.parse_ledger(content)

    def test_fenced_examples_and_subheadings_are_not_entries(self):
        body = (
            "## Cross references\n\n- L-0001: depends-on.\n\n"
            "````markdown\n# Malformed example\n```\n"
            "# L-a7c91e2f Duplicate example 2000-01-01T00:00:00Z\n`````\n\n"
            "~~~text\n# Another example\n~~~\n\n"
            "    # Indented code\n> # Quoted text"
        )
        self.assertEqual(len(ledger.parse_ledger(entry(body=body))), 1)

    def test_unclosed_fences_and_nonempty_files_without_entries_are_rejected(self):
        for content in ("Unstructured notes\n", entry(body="```text\nunclosed")):
            with self.assertRaises(ledger.LedgerError):
                ledger.parse_ledger(content)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ledger tests ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "LEDGER.md"
        self.run_cli("init")

    def run_cli(self, *arguments, success=True, path=None, input=None):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(SCRIPT), "--file", str(path or self.path), *arguments],
            capture_output=True, encoding="utf-8", input=input, cwd=self.root, timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def append(self, title="Decision", body="Useful context.", *, item_id=None, timestamp=STAMP, path=None):
        arguments = ["append", title, "--body", body, "--timestamp", timestamp, "--json"]
        if item_id is not None:
            arguments.extend(("--id", item_id))
        return json.loads(self.run_cli(*arguments, path=path).stdout)

    def items(self, *arguments):
        return json.loads(self.run_cli("list", *arguments, "--json").stdout)

    def snapshots(self):
        paths = [self.root / f"{side}.md" for side in ("base", "ours", "theirs")]
        for path in paths:
            shutil.copyfile(self.path, path)
        return paths

    def run_merge(self, paths, *, success=True):
        arguments = ["merge"]
        for side, path in zip(("base", "ours", "theirs"), paths):
            arguments.extend(("--" + side, str(path)))
        return self.run_cli(*arguments, success=success)


class LedgerTests(LedgerCase):
    def test_append_read_show_and_full_text_search(self):
        first = self.append("First decision", "The café discussion established our scope.")
        before = self.path.read_bytes()
        second = self.append("Correction", f"This corrects {first['id']}; the original scope was too wide.")
        self.assertTrue(self.path.read_bytes().startswith(before))
        read = self.run_cli("read").stdout
        self.assertIn(first["text"], read)
        self.assertIn(second["text"], read)
        shown = json.loads(self.run_cli("show", first["id"], "--json").stdout)
        self.assertEqual(shown["text"], first["text"])
        matches = json.loads(self.run_cli("search", first["id"], "--json").stdout)
        self.assertEqual([item["id"] for item in matches], [first["id"], second["id"]])
        self.assertEqual([item["id"] for item in self.items("--search", "CAFÉ")], [first["id"]])
        self.assertEqual([item["id"] for item in self.items("--recent", "1")], [second["id"]])

    def test_stdin_body_and_generated_id_and_timestamp(self):
        body = "First paragraph.\n\n```markdown\n# An example heading\n```\n"
        result = self.run_cli("append", "A discovery", "--body-file", "-", "--json", input=body)
        appended = json.loads(result.stdout)
        self.assertRegex(appended["id"], r"^L-[0-9a-f]{8}$")
        self.assertRegex(appended["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn(body.rstrip(), appended["text"])

    def test_append_preserves_existing_crlf_bytes(self):
        original = entry().replace("\n", "\r\n").encode("utf-8")
        self.path.write_bytes(original)
        self.append("Second")
        self.assertTrue(self.path.read_bytes().startswith(original))

    def test_invalid_appends_leave_history_unchanged(self):
        first = self.append()
        original = self.path.read_bytes()
        cases = (
            ("append", "Duplicate", "--body", "Context", "--id", first["id"], "--timestamp", STAMP),
            ("append", "Earlier", "--body", "Context", "--timestamp", "2026-11-17T11:59:59Z"),
            ("append", "Empty", "--body", "  "),
            ("append", "Injected", "--body", entry("L-0002"), "--timestamp", STAMP),
            ("append", "Unclosed", "--body", "```text\nExample", "--timestamp", STAMP),
            ("append", " Newline\nTitle", "--body", "Context"),
            ("init",),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.run_cli(*arguments, success=False)
                self.assertEqual(self.path.read_bytes(), original)
        self.run_cli("show", "L-0000", success=False)

    def test_existing_invalid_ledger_cannot_be_blessed_by_append(self):
        self.path.write_text(entry() + "\n" + entry(), encoding="utf-8")
        original = self.path.read_bytes()
        self.run_cli("append", "New entry", "--body", "Context", success=False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_appends_keep_all_entries(self):
        processes = [subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(SCRIPT), "--file", str(self.path),
             "append", f"Entry {index}", "--body", f"Context {index}", "--timestamp", STAMP, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
        ) for index in range(8)]
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
            self.assertIn("id", json.loads(output))
        entries = self.items()
        self.assertEqual(len(entries), 8)
        self.assertEqual(len({item["id"] for item in entries}), 8)
        self.run_cli("validate")

    def test_failed_atomic_write_preserves_history_and_cleans_temporary_file(self):
        self.append()
        original = self.path.read_bytes()
        with mock.patch.object(ledger.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                ledger.append(self.path, title="Second", body="Context", timestamp=STAMP)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".LEDGER.md.*")), [])

    def test_lock_timeout_does_not_remove_existing_owner(self):
        with ledger.lock(self.path):
            with self.assertRaises(ledger.LedgerError):
                with ledger.lock(self.path, timeout=0.01):
                    self.fail("lock was stolen")
            self.assertTrue((self.root / ".LEDGER.md.lock" / "owner.json").exists())
        with ledger.lock(self.path, timeout=0):
            pass


class MergeTests(LedgerCase):
    def test_merge_sorts_whole_entries_and_keeps_shared_history_once(self):
        first = self.append("Original", item_id="L-0001")
        paths = self.snapshots()
        left = self.append("Left", timestamp="2026-11-17T12:00:02Z", path=paths[1])
        right = self.append("Right", timestamp="2026-11-17T12:00:01Z", path=paths[2])
        self.path.write_text("<<<<<<< ours\nconflict\n=======\n>>>>>>> theirs\n", encoding="utf-8")
        self.run_merge(paths)
        self.assertEqual([item["id"] for item in self.items()], [first["id"], right["id"], left["id"]])
        for old in (first, left, right):
            shown = json.loads(self.run_cli("show", old["id"], "--json").stdout)
            self.assertEqual(shown["text"], old["text"])
        before = self.path.read_bytes()
        self.run_merge(paths)
        self.assertEqual(self.path.read_bytes(), before)

    def test_equal_timestamp_ties_keep_target_branch_first(self):
        paths = self.snapshots()
        left = self.append("Left", path=paths[1])
        right = self.append("Right", path=paths[2])
        self.run_merge(paths)
        self.assertEqual([item["id"] for item in self.items()], [left["id"], right["id"]])

    def test_incoming_id_collision_updates_only_incoming_references(self):
        paths = self.snapshots()
        left = self.append("Local", "Local reference to L-deadbeef.", item_id="L-deadbeef", path=paths[1])
        self.append("Incoming", "Incoming reference to L-deadbeef.", item_id="L-deadbeef", path=paths[2])
        self.append("Follow-up", "Depends on L-deadbeef. Keep L-deadbeef-extra and L-deadbeef0 intact.", path=paths[2])
        self.run_merge(paths)
        items = self.items()
        renamed = next(item["id"] for item in items if item["title"] == "Incoming")
        self.assertNotEqual(renamed, "L-deadbeef")
        self.assertRegex(renamed, r"^L-[0-9a-f]{8}$")
        local = self.run_cli("show", "L-deadbeef").stdout
        self.assertEqual(local.rstrip("\n"), left["text"])
        follow_up = next(item for item in items if item["title"] == "Follow-up")
        text = self.run_cli("show", follow_up["id"]).stdout
        self.assertIn(f"Depends on {renamed}.", text)
        self.assertIn("L-deadbeef-extra and L-deadbeef0", text)

    def test_modified_or_removed_historical_entries_stop_merge(self):
        self.append("Original")
        paths = self.snapshots()
        before = self.path.read_bytes()
        paths[1].write_text(paths[1].read_text(encoding="utf-8").replace("Useful context.", "Changed explanation."), encoding="utf-8")
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), before)
        paths[1].write_text("", encoding="utf-8")
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_ambiguous_shared_new_reference_stops_merge(self):
        paths = self.snapshots()
        for index, title in ((1, "Local"), (2, "Incoming")):
            self.append(title, item_id="L-deadbeef", path=paths[index])
            self.append("Shared", "Refers to L-deadbeef.", item_id="L-cafefeed", path=paths[index])
        before = self.path.read_bytes()
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_snapshots_or_conflicting_preambles_leave_destination_unchanged(self):
        self.append()
        paths = self.snapshots()
        before = self.path.read_bytes()
        paths[1].write_text("Local context.\n\n" + paths[1].read_text(encoding="utf-8"), encoding="utf-8")
        paths[2].write_text("Incoming context.\n\n" + paths[2].read_text(encoding="utf-8"), encoding="utf-8")
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), before)
        paths[2].write_text("# Invalid snapshot\n", encoding="utf-8")
        self.run_merge(paths, success=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.run_cli("merge", success=False)
        self.assertEqual(self.path.read_bytes(), before)


class GitTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(SCRIPT, scripts / "ledger.py")

    def git(self, *arguments, success=True, input=None):
        result = subprocess.run(
            ["git", *arguments], cwd=self.root, capture_output=True, encoding="utf-8", input=input, timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def commit(self, message):
        self.git("-c", "user.name=Ledger Tests", "-c", "user.email=ledger-tests@example.invalid", "commit", "-qm", message)

    def hook(self, success=True):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(HOOK)], cwd=self.root,
            capture_output=True, encoding="utf-8", timeout=30,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_hook_validates_index_instead_of_working_copy(self):
        self.append()
        original = self.path.read_bytes()
        self.git("add", "LEDGER.md")
        self.path.write_text("# Invalid working file\n", encoding="utf-8")
        self.hook()
        self.git("add", "LEDGER.md")
        self.path.write_bytes(original)
        self.hook(success=False)
        self.run_cli("validate")

    def test_hook_allows_initial_absence_but_rejects_staged_deletion(self):
        self.hook()
        self.git("add", "LEDGER.md")
        self.commit("Adopt ledger")
        self.git("rm", "LEDGER.md")
        self.hook(success=False)

    def test_hook_rejects_duplicate_ids_and_decreasing_timestamps(self):
        for content in (
            entry() + "\n" + entry(),
            entry(timestamp="2026-11-17T12:00:01Z") + "\n" + entry("L-0002"),
        ):
            self.path.write_text(content, encoding="utf-8")
            self.git("add", "LEDGER.md")
            self.hook(success=False)

    def test_real_git_conflict_is_resolved_from_index_and_requires_restaging(self):
        first = self.append("Base")
        self.git("add", "LEDGER.md")
        self.commit("Base ledger")
        self.git("switch", "-qc", "incoming")
        incoming = self.append("Incoming", timestamp="2026-11-17T12:00:01Z")
        self.git("add", "LEDGER.md")
        self.commit("Incoming ledger entry")
        self.git("switch", "-qc", "local", "HEAD~1")
        local = self.append("Local", timestamp="2026-11-17T12:00:02Z")
        self.git("add", "LEDGER.md")
        self.commit("Local ledger entry")
        self.git("-c", "user.name=Ledger Tests", "-c", "user.email=ledger-tests@example.invalid", "merge", "--no-commit", "incoming", success=False)
        self.assertIn("<<<<<<<", self.path.read_text(encoding="utf-8"))
        self.hook(success=False)
        self.run_cli("merge", "--staged")
        self.assertEqual([item["id"] for item in self.items()], [first["id"], incoming["id"], local["id"]])
        self.hook(success=False)
        self.git("add", "LEDGER.md")
        self.hook()

    def test_add_add_conflict_without_a_base_file_is_resolved(self):
        sources = [entry("L-0001", title="Local"), entry("L-0002", title="Incoming")]
        blobs = [self.git("hash-object", "-w", "--stdin", input=content).stdout.strip() for content in sources]
        stages = "".join(f"100644 {blob} {stage}\tLEDGER.md\n" for stage, blob in zip((2, 3), blobs))
        self.git("update-index", "--index-info", input=stages)
        self.path.write_text("<<<<<<< conflict\n", encoding="utf-8")
        self.run_cli("merge", "--staged")
        self.assertEqual([item["id"] for item in self.items()], ["L-0001", "L-0002"])
        self.git("add", "LEDGER.md")
        self.hook()

    def test_merge_staged_without_conflict_preserves_destination(self):
        self.append()
        self.git("add", "LEDGER.md")
        original = self.path.read_bytes()
        self.run_cli("merge", "--staged", success=False)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
