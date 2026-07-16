#!/usr/bin/env python3
"""Unit tests for the no_guessed_proofs.py PreToolUse guard.

The guard blocks a search-discoverable `by <method>` UNLESS a current
sledgehammer/try0 (within --window) proves it was found rather than guessed. It
reads stdin JSON and exits 0 (allow) or 2 (block, explanation on stderr); it is
driven here as a subprocess -- the real harness contract -- so the test sees the
exact exit code the agent harness (Claude/Codex/OpenCode) acts on. parse_config
is tested in-process.

Run directly: python3 test/no_guessed_proofs.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

from hook_test_support import run_hook as run_hook_process, run_without_package, thy_write

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUESSED = os.path.join(ROOT, "no_guessed_proofs.py")

sys.path.insert(0, ROOT)
import no_guessed_proofs as guessed
from isabelle_hooks.config import DEFAULTS, GuardDefaults
from isabelle_hooks import discovery


def run_hook(payload, args=()):
    args = list(args)
    if "--isabelle-command" not in args and "--searchable" not in args:
        # Policy tests which exercise discovery fallback must not depend on whether
        # the developer machine happens to have Isabelle on PATH.
        args.extend(["--isabelle-command", "/definitely/missing/isabelle"])
    return run_hook_process(GUESSED, payload, args)


def exec_call(source, transcript_path=None):
    payload = {"tool_name": "functions.exec", "tool_input": {"code": source}}
    if transcript_path is not None:
        payload["transcript_path"] = transcript_path
    return payload


def exec_write(content):
    source = (
        "const r = await tools.mcp__iq_dev__write_file({"
        'path: "Foo.thy", command: "str_replace", old_str: "by sorry", '
        "new_str: " + json.dumps(content) + "}); text(r.output);"
    )
    return exec_call(source)


def transcript_with(*tool_names):
    """A JSONL transcript fixture whose lines each carry one tool_use, in order.
    Returns the path; caller removes it."""
    return transcript_with_calls(*[(name, {}) for name in tool_names])


def transcript_with_calls(*calls):
    """Like transcript_with but each entry is a (name, input) pair, so a test can
    put values in the tool input. Returns the path; caller removes it."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for name, inp in calls:
            event = {"message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
            f.write(json.dumps(event) + "\n")
    return path


def use(name, inp=None, kind="tool_use", call_id=None):
    """A tool-call content block (Claude `tool_use`, or a Codex/OpenCode variant)."""
    block = {"type": kind, "name": name, "input": inp or {}}
    if call_id is not None:
        block["id" if kind == "tool_use" else "call_id"] = call_id
    return block


def result(text, kind="tool_result", call_id=None):
    """A tool-result content block carrying `text` (what sledgehammer/try0 printed)."""
    block = {"type": kind, "content": text}
    if call_id is not None:
        block["tool_use_id" if kind == "tool_result" else "call_id"] = call_id
    return block


def transcript_blocks(*blocks, envelope=True):
    """Write `blocks` (from use()/result()) as a JSONL transcript. With envelope,
    wrap each in Claude's {"message":{"content":[...]}}; without, write the bare block
    as its own line -- exercising the tolerant parser on non-Claude shapes. Returns
    the path; caller removes it."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for b in blocks:
            line = {"message": {"content": [b]}} if envelope else b
            f.write(json.dumps(line) + "\n")
    return path


class ParseConfig(unittest.TestCase):
    def test_defaults_have_one_immutable_owner(self):
        self.assertEqual(guessed.DEFAULT_WINDOW, DEFAULTS.window)
        self.assertEqual(guessed.DEFAULT_FOUND_VIA, DEFAULTS.found_via)
        with self.assertRaises(FrozenInstanceError):
            DEFAULTS.window = 99
        self.assertEqual(
            GuardDefaults(isabelle_command="custom").isabelle_command, "custom")

    def test_defaults(self):
        cfg = guessed.parse_config([])
        self.assertEqual(cfg.window, guessed.DEFAULT_WINDOW)
        self.assertEqual(cfg.allowed, set())
        self.assertEqual(cfg.found_via, list(guessed.DEFAULT_FOUND_VIA))
        self.assertEqual(
            cfg.isabelle_command,
            os.environ.get("ISABELLE_HOOKS_ISABELLE", "isabelle"),
        )
        self.assertIsNone(cfg.remediation)

    def test_window_and_allow(self):
        cfg = guessed.parse_config(["--window", "5", "--allow", "auto", "simp"])
        self.assertEqual(cfg.window, 5)
        self.assertEqual(cfg.allowed, {"auto", "simp"})

    def test_bad_window_falls_back(self):
        cfg = guessed.parse_config(["--window", "notanint"])
        self.assertEqual(cfg.window, guessed.DEFAULT_WINDOW)

    def test_nonpositive_window_falls_back(self):
        for value in ("0", "-2"):
            with self.subTest(value=value):
                cfg = guessed.parse_config(["--window", value])
                self.assertEqual(cfg.window, guessed.DEFAULT_WINDOW)

    def test_unknown_args_ignored(self):
        cfg = guessed.parse_config(["--bogus", "x", "--allow", "blast"])
        self.assertEqual(cfg.allowed, {"blast"})

    def test_found_via_parsed(self):
        cfg = guessed.parse_config(["--found-via", "sledgehammer", "try0", "--allow", "auto"])
        self.assertEqual(cfg.found_via, ["sledgehammer", "try0"])
        self.assertEqual(cfg.allowed, {"auto"})

    def test_found_via_empty_falls_back_to_default(self):
        # An explicit but empty --found-via (nothing before the next flag) must not
        # leave the escape hatch dead; it falls back to the built-in default.
        cfg = guessed.parse_config(["--found-via", "--window", "7"])
        self.assertEqual(cfg.found_via, list(guessed.DEFAULT_FOUND_VIA))
        self.assertEqual(cfg.window, 7)

    def test_remediation_is_single_value(self):
        cfg = guessed.parse_config(["--remediation", "Fix: run the hammer, then write it."])
        self.assertEqual(cfg.remediation, "Fix: run the hammer, then write it.")

    def test_discovery_options(self):
        cfg = guessed.parse_config([
            "--isabelle-command", "isabelle_dev", "--searchable", "auto", "metis"])
        self.assertEqual(cfg.isabelle_command, "isabelle_dev")
        self.assertEqual(cfg.searchable_override, {"auto", "metis"})


class MethodDiscovery(unittest.TestCase):
    def test_success_is_cached_and_reused(self):
        with tempfile.TemporaryDirectory() as cache:
            with mock.patch.object(discovery, "_cache_root", return_value=cache), \
                 mock.patch.object(discovery, "_discover_identity",
                                   return_value=("/isabelle", "stable", "fingerprint")), \
                 mock.patch.object(discovery, "_run_discovery",
                                   return_value={"auto", "metis"}) as run:
                first, warning1 = guessed.discover_searchable_methods("isabelle_dev")
                second, warning2 = guessed.discover_searchable_methods("isabelle_dev")
            self.assertEqual(first, {"auto", "metis"})
            self.assertEqual(second, first)
            self.assertIsNone(warning1)
            self.assertIsNone(warning2)
            self.assertEqual(run.call_count, 1)

    def test_changed_fingerprint_refreshes_cache(self):
        with tempfile.TemporaryDirectory() as cache:
            identities = [("/isabelle", "stable", "one"),
                          ("/isabelle", "stable", "two")]
            with mock.patch.object(discovery, "_cache_root", return_value=cache), \
                 mock.patch.object(discovery, "_discover_identity", side_effect=identities), \
                 mock.patch.object(discovery, "_run_discovery",
                                   side_effect=[{"auto"}, {"auto", "smt"}]) as run:
                first, _ = guessed.discover_searchable_methods("isabelle_dev")
                second, _ = guessed.discover_searchable_methods("isabelle_dev")
            self.assertEqual(first, {"auto"})
            self.assertEqual(second, {"auto", "smt"})
            self.assertEqual(run.call_count, 2)

    def test_failure_uses_last_valid_cache(self):
        with tempfile.TemporaryDirectory() as cache:
            with open(os.path.join(cache, "stable-old.json"), "w", encoding="utf-8") as f:
                json.dump({"methods": ["auto", "metis"]}, f)
            with mock.patch.object(discovery, "_cache_root", return_value=cache), \
                 mock.patch.object(discovery, "_discover_identity",
                                   return_value=("/isabelle", "stable", "new")), \
                 mock.patch.object(discovery, "_run_discovery",
                                   side_effect=RuntimeError("boom")):
                methods, warning = guessed.discover_searchable_methods("isabelle_dev")
            self.assertEqual(methods, {"auto", "metis"})
            self.assertIn("previous cache", warning)

    def test_failure_without_cache_returns_none_not_empty_set(self):
        with tempfile.TemporaryDirectory() as cache:
            with mock.patch.object(discovery, "_cache_root", return_value=cache), \
                 mock.patch.object(discovery, "_discover_identity",
                                   return_value=("/isabelle", "stable", "new")), \
                 mock.patch.object(discovery, "_run_discovery",
                                   side_effect=RuntimeError("boom")):
                methods, warning = guessed.discover_searchable_methods("isabelle_dev")
            self.assertIsNone(methods)
            self.assertIn("no cache", warning)

    def test_malformed_discovery_output_is_rejected(self):
        completed = subprocess.CompletedProcess([], 0, "Finished Draft", "")
        with mock.patch.object(guessed.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError):
                guessed._run_discovery("isabelle_dev")


class NoGuessedProofs(unittest.TestCase):
    def test_missing_shared_package_fails_open_cleanly(self):
        code, err = run_without_package(GUESSED, thy_write("lemma x by auto"))
        self.assertEqual(code, 0, err)
        self.assertIn("shared helper unavailable", err)
        self.assertNotIn("Traceback", err)

    def test_multiedit_prior_state_change_exposes_guess(self):
        fd, path = tempfile.mkstemp(suffix=".thy")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("text ‹intro›\ntext ‹PLACEHOLDER›\n")
            payload = {"tool_name": "MultiEdit", "tool_input": {
                "file_path": path, "edits": [
                    {"old_string": "text ‹intro›\ntext ‹",
                     "new_string": "text ‹intro›\n"},
                    {"old_string": "PLACEHOLDER›", "new_string": "by auto"},
                ]}}
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2, err)
        finally:
            os.remove(path)

    def test_multiedit_prior_state_change_makes_guess_prose(self):
        fd, path = tempfile.mkstemp(suffix=".thy")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("text ‹intro›\nPLACEHOLDER\n")
            payload = {"tool_name": "MultiEdit", "tool_input": {
                "file_path": path, "edits": [
                    {"old_string": "text ‹intro›\n",
                     "new_string": "text ‹intro›\ntext ‹"},
                    {"old_string": "PLACEHOLDER\n", "new_string": "by auto›\n"},
                ]}}
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_allow_listed_method_passes(self):
        code, _ = run_hook(thy_write("lemma x by auto"), ["--allow", "auto", "simp"])
        self.assertEqual(code, 0)

    def test_non_listed_method_blocks(self):
        code, err = run_hook(thy_write("lemma x by metis"), ["--allow", "simp"])
        self.assertEqual(code, 2)
        self.assertIn("metis", err)

    def test_no_allowlist_blocks_guess(self):
        code, _ = run_hook(thy_write("lemma x by auto"))
        self.assertEqual(code, 2)

    def test_non_thy_allowed(self):
        payload = {"tool_name": "Write",
                   "tool_input": {"file_path": "notes.md", "content": "by auto"}}
        code, _ = run_hook(payload)
        self.assertEqual(code, 0)

    def test_str_replace_preexisting_anchor_closer_not_blocked(self):
        # Regression for the reported false positive: a str-replace whose anchor
        # reproduces a pre-existing `by auto` line and appends a new lemma closed with an
        # allow-listed method must NOT block on the anchor's `by auto` (it is not new).
        anchor = "lemma toy:\n  unfolding foo_def by auto"
        payload = {
            "tool_name": "mcp__iq-release__write_file",
            "tool_input": {
                "path": "Foo.thy",
                "command": "str_replace",
                "old_str": anchor,
                "new_str": anchor + "\n\nlemma extra:\n  show False by (rule notE)",
            },
        }
        code, err = run_hook(payload, ["--allow", "rule", "simp"])
        self.assertEqual(code, 0, err)

    def test_str_replace_prose_inside_cartouche_not_blocked(self):
        # Regression: editing prose deep inside a `text ‹..›` block (delimiters outside
        # the fragment) where the prose says "checked by a countermodel" must NOT block
        # on a phantom `by a` closer. The edit site is read from the file on disk.
        anchor = "  bare parameters alone carry the fork."
        fd, path = tempfile.mkstemp(suffix=".thy")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("theory S imports Main begin\n\ntext \\<open>\n  Intro.\n"
                    + anchor + "\n\\<close>\n\nlemma r: \"True\" by simp\nend\n")
        try:
            payload = {
                "tool_name": "mcp__iq-release__write_file",
                "tool_input": {
                    "path": path,
                    "command": "str_replace",
                    "old_str": anchor,
                    "new_str": anchor + "\n  this is checked by a countermodel below",
                },
            }
            code, err = run_hook(payload, ["--allow", "simp"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_str_replace_ascii_cartouche_file_unicode_edit_not_blocked(self):
        # Regression for the reported "by construction" block: the file uses the ASCII
        # cartouche `\<open>..\<close>`, the edit is phrased with Unicode `‹..›`. The
        # notation fold must let the edit-site seed find the block so the inserted prose
        # ("some hold only by construction of our encoding") is not read as a `by` closer.
        anchor = "  Every formalized item below is introduced"
        fd, path = tempfile.mkstemp(suffix=".thy")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("theory T imports Main begin\n\ntext \\<open>\n" + anchor
                    + "\n  in place.\n\\<close>\n\nlemma r: \"True\" by simp\nend\n")
        try:
            payload = {
                "tool_name": "mcp__iq-release__write_file",
                "tool_input": {
                    "path": path,
                    "command": "str_replace",
                    "old_str": "begin\n\ntext ‹\n" + anchor,
                    "new_str": "begin\n\ntext ‹\n  some hold only\n"
                               "  by construction of our encoding and are marked so\n"
                               "›\n\ntext ‹\n" + anchor,
                },
            }
            code, err = run_hook(payload, ["--allow", "simp"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_str_replace_new_guessed_closer_still_blocks(self):
        # The complement: a str-replace that appends a genuinely NEW guessed closer is
        # still blocked -- the anchor strip must not become a bypass.
        anchor = "lemma toy:\n  unfolding foo_def by auto"
        payload = {
            "tool_name": "mcp__iq-release__write_file",
            "tool_input": {
                "path": "Foo.thy",
                "command": "str_replace",
                "old_str": anchor,
                "new_str": anchor + "\n\nlemma extra:\n  by metis",
            },
        }
        code, err = run_hook(payload, ["--allow", "rule", "simp"])
        self.assertEqual(code, 2)
        self.assertIn("metis", err)

    def test_pide_edit_prose_inside_cartouche_not_blocked(self):
        # Same prose-in-cartouche false positive, but through the isabelle-pide-mcp
        # `edit` tool (old_text/text/origin) -- parity with the write_file str_replace.
        anchor = "  bare parameters alone carry the fork."
        fd, path = tempfile.mkstemp(suffix=".thy")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("theory S imports Main begin\n\ntext ‹\n  Intro.\n"
                    + anchor + "\n›\n\nlemma r: \"True\" by simp\nend\n")
        try:
            payload = {
                "tool_name": "mcp__isabelle-pide-mcp__edit",
                "tool_input": {
                    "origin": path, "mode": "replace",
                    "old_text": anchor,
                    "text": anchor + "\n  this is checked by a countermodel below",
                },
            }
            code, err = run_hook(payload, ["--allow", "simp"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_pide_edit_new_guessed_closer_still_blocks(self):
        # And a genuinely guessed closer via the PIDE edit tool is still blocked.
        payload = {
            "tool_name": "mcp__isabelle-pide-mcp__edit",
            "tool_input": {
                "origin": "Foo.thy", "mode": "replace",
                "old_text": "lemma x:\n  sorry",
                "text": "lemma x:\n  by metis",
            },
        }
        code, err = run_hook(payload, ["--allow", "simp"])
        self.assertEqual(code, 2)
        self.assertIn("metis", err)

    def test_apply_patch_guessed_method_blocks(self):
        # Codex edits via apply_patch: a guessed `+ by metis` added to a *.thy blocks.
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: Foo.thy\n@@\n lemma x:\n+  by metis\n*** End Patch\n"
        )}}
        code, err = run_hook(payload, ["--allow", "simp"])
        self.assertEqual(code, 2)
        self.assertIn("metis", err)

    def test_apply_patch_allow_listed_passes(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: Foo.thy\n@@\n lemma x:\n+  by simp\n*** End Patch\n"
        )}}
        code, _ = run_hook(payload, ["--allow", "simp"])
        self.assertEqual(code, 0)

    def test_whole_write_method_added_after_existing_by_blocks(self):
        old = "theory T imports Main begin\nlemma x: True\n  by\nend\n"
        fd, path = tempfile.mkstemp(suffix=".thy")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(old)
        try:
            payload = {"tool_name": "Write", "tool_input": {
                "file_path": path, "content": old.replace("end\n", "  auto\nend\n")}}
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_replacement_method_added_after_unchanged_by_blocks(self):
        old_file = "theory T imports Main begin\nlemma x: True\n  by\n  sorry\nend\n"
        fd, path = tempfile.mkstemp(suffix=".thy")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(old_file)
        try:
            payload = {"tool_name": "Edit", "tool_input": {
                "file_path": path,
                "old_string": "  by\n  sorry",
                "new_string": "  by\n  auto",
            }}
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_apply_patch_method_added_after_context_by_blocks(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: Foo.thy\n@@\n lemma x: True\n"
            "  by\n+  auto\n*** End Patch\n"
        )}}
        code, err = run_hook(payload, ["--searchable", "auto"])
        self.assertEqual(code, 2)
        self.assertIn("method `auto`", err)

    def test_apply_patch_move_into_thy_blocks(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: scratch.txt\n*** Move to: Foo.thy\n"
            "@@\n+lemma x by auto\n*** End Patch\n"
        )}}
        code, err = run_hook(payload, ["--searchable", "auto"])
        self.assertEqual(code, 2)
        self.assertIn("method `auto`", err)

    def test_codex_exec_guessed_write_blocks(self):
        code, err = run_hook(exec_write("by auto"), ["--searchable", "auto"])
        self.assertEqual(code, 2)
        self.assertIn("method `auto`", err)

    def test_codex_exec_search_result_authorizes_wrapped_write(self):
        search = (
            "const r = await tools.mcp__iq_dev__repl_sledgehammer({}); "
            "text(r.output);"
        )
        path = transcript_blocks(
            use("functions.exec", {"code": search}, kind="function_call", call_id="search"),
            result("Try this: by auto", kind="function_call_output", call_id="search"),
        )
        try:
            payload = exec_write("by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_codex_exec_json_arguments_authorize_wrapped_write(self):
        search = (
            "const r = await tools.mcp__iq_dev__repl_sledgehammer({}); "
            "text(r.output);"
        )
        call = {
            "type": "function_call",
            "name": "functions.exec",
            "arguments": json.dumps({"code": search}),
            "call_id": "search",
        }
        path = transcript_blocks(
            call,
            result("Try this: by auto", kind="function_call_output", call_id="search"),
        )
        try:
            payload = exec_write("by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_completed_codex_exec_write_consumes_search_evidence(self):
        search = (
            "const r = await tools.mcp__iq_dev__repl_sledgehammer({}); "
            "text(r.output);"
        )
        first_write = exec_write("by auto")["tool_input"]["code"]
        path = transcript_blocks(
            use("functions.exec", {"code": search}, kind="function_call", call_id="search"),
            result("Try this: by auto", kind="function_call_output", call_id="search"),
            use("functions.exec", {"code": first_write}, kind="function_call", call_id="write-1"),
            result("completed", kind="function_call_output", call_id="write-1"),
        )
        try:
            payload = exec_write("by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("NOT found", err)
        finally:
            os.remove(path)

    def test_in_flight_codex_exec_write_does_not_consume_search_evidence(self):
        search = (
            "const r = await tools.mcp__iq_dev__repl_sledgehammer({}); "
            "text(r.output);"
        )
        current_write = exec_write("by auto")["tool_input"]["code"]
        path = transcript_blocks(
            use("functions.exec", {"code": search}, kind="function_call", call_id="search"),
            result("Try this: by auto", kind="function_call_output", call_id="search"),
            use("functions.exec", {"code": current_write}, kind="function_call", call_id="write"),
        )
        try:
            payload = exec_write("by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_recent_sledgehammer_escape_hatch(self):
        # sledgehammer ran AND its result names the method being written -> found.
        path = transcript_blocks(
            use("repl_sledgehammer"),
            result("Proof found. Try this: by (metis foo bar) (0.3 s)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0)  # found, not guessed
        finally:
            os.remove(path)

    def test_iq_explore_sledgehammer_is_escape_hatch(self):
        path = transcript_blocks(
            use("mcp__iq-dev__explore", {
                "query": "sledgehammer", "arguments": "provers = cvc5",
            }, call_id="search"),
            result("Try this: by (metis foo)", call_id="search"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_current_in_flight_edit_does_not_invalidate_search(self):
        edit_calls = (
            ("Write", {"file_path": "Foo.thy", "content": "lemma x by auto"}),
            ("Edit", {"file_path": "Foo.thy", "new_string": "by auto"}),
            ("MultiEdit", {"file_path": "Foo.thy", "edits": [
                {"new_string": "lemma x by auto"},
            ]}),
            ("apply_patch", {"patch": (
                "*** Begin Patch\n*** Add File: Foo.thy\n+lemma x by auto\n*** End Patch\n"
            )}),
            ("Bash", {"command": "printf 'lemma x by auto' > Foo.thy"}),
            ("mcp__iq-dev__write_file", {"path": "Foo.thy", "content": "by auto"}),
            ("mcp__iq-dev__save_file", {"path": "Foo.thy", "content": "by auto"}),
            ("mcp__iq-dev__open_file", {
                "path": "Foo.thy", "create_if_missing": True, "content": "by auto",
            }),
            ("mcp__isabelle-pide-mcp__edit", {"origin": "Foo.thy", "text": "by auto"}),
        )
        for index, (name, inp) in enumerate(edit_calls):
            with self.subTest(tool=name):
                path = transcript_blocks(
                    use("repl_step", {"isar_text": "try0"}, call_id="search"),
                    result("Found proof: by auto", call_id="search"),
                    use(name, inp, call_id="current-%d" % index),
                )
                try:
                    payload = {"tool_name": name, "tool_input": inp}
                    payload["transcript_path"] = path
                    code, err = run_hook(payload, ["--searchable", "auto"])
                    self.assertEqual(code, 0, (name, err))
                finally:
                    os.remove(path)

    def test_current_edit_does_not_consume_minimal_window(self):
        path = transcript_blocks(
            use("repl_step", {"isar_text": "try0"}, call_id="search"),
            result("Found proof: by auto", call_id="search"),
            use("Write", {"file_path": "Foo.thy", "content": "lemma x by auto"},
                call_id="current"),
        )
        try:
            payload = thy_write("lemma x by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--window", "1", "--searchable", "auto"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_found_method_is_consumed_by_first_write(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("Write", {"file_path": "First.thy", "content": "lemma a by auto"},
                call_id="write"),
            result("written", call_id="write"),
        )
        try:
            payload = thy_write("lemma b by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_completed_write_still_invalidates_before_current_write(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("Write", {"file_path": "First.thy", "content": "lemma a by auto"},
                call_id="first-write"),
            result("written", call_id="first-write"),
            use("Write", {"file_path": "Second.thy", "content": "lemma b by auto"},
                call_id="current-write"),
        )
        try:
            payload = thy_write("lemma b by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_completed_save_file_invalidates_search(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("mcp__iq-dev__save_file", {"path": "First.thy", "content": "by auto"},
                call_id="save"),
            result("saved", call_id="save"),
        )
        try:
            payload = thy_write("lemma b by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_completed_open_file_create_invalidates_search(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("mcp__iq-dev__open_file", {
                "path": "First.thy", "create_if_missing": True,
                "content": "lemma a by auto",
            }, call_id="create"),
            result("created", call_id="create"),
        )
        try:
            payload = thy_write("lemma b by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_read_only_open_file_preserves_search_evidence(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("mcp__iq-dev__open_file", {"path": "Existing.thy"}, call_id="open"),
            result("opened", call_id="open"),
        )
        try:
            payload = thy_write("lemma x by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_one_search_result_cannot_authorize_two_closers_in_one_write(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
        )
        try:
            payload = thy_write("lemma a by auto\nlemma b by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_proof_step_after_search_invalidates_result(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("repl_step", {"isar_text": "next"}, call_id="step"),
            result("new goal", call_id="step"),
        )
        try:
            payload = thy_write("lemma b by auto")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_in_flight_proof_step_is_not_mistaken_for_current_edit(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="search"),
            result("Try this: by auto", call_id="search"),
            use("repl_step", {"isar_text": "next"}, call_id="step"),
        )
        try:
            payload = thy_write("lemma b by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto"])
            self.assertEqual(code, 2)
            self.assertIn("method `auto`", err)
        finally:
            os.remove(path)

    def test_try0_in_step_input_is_escape_hatch(self):
        # try0 run as a proof step (`repl_step "try0"`) lives in the tool input's
        # command field, not the name -- the escape hatch must still fire, and the
        # method it reported must match what is written.
        path = transcript_blocks(
            use("repl_step", {"command": "try0"}),
            result("Try this: by metis (12 ms)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0)
        finally:
            os.remove(path)

    def test_try0_in_repl_step_isar_text_is_escape_hatch(self):
        # The iq-dev REPL's repl_step carries its command in `isar_text` (NOT `command`),
        # so a real `repl_step {"isar_text": "try0"}` run must unlock the hatch. Regression
        # for the hook only recognising repl_sledgehammer and missing the try0-as-step run.
        path = transcript_blocks(
            use("repl_step", {"isar_text": "try0"}),
            result("Try this: by metis (12 ms)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0)
        finally:
            os.remove(path)

    def test_sledgehammer_in_repl_step_isar_text_is_escape_hatch(self):
        # sledgehammer can also be invoked as a step (`repl_step {"isar_text":
        # "sledgehammer"}`) rather than via the dedicated tool; that must unlock too.
        path = transcript_blocks(
            use("repl_step", {"isar_text": "sledgehammer"}),
            result("Try this: by (metis foo bar) (0.3 s)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0)
        finally:
            os.remove(path)

    def test_sledgehammer_result_without_method_still_blocks(self):
        # The core soundness fix: a sledgehammer that ran but did NOT find this method
        # (e.g. it timed out, or found it for another goal) must not unlock the guess.
        path = transcript_blocks(
            use("repl_sledgehammer"),
            result("No proof found."),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_found_via_restricts_which_tool_unlocks(self):
        # With --found-via try0 only, a sledgehammer result must NOT unlock the guess;
        # only try0 counts. Proves the trigger set is driven by config, not hard-coded.
        path = transcript_blocks(
            use("repl_sledgehammer"),
            result("Try this: by (metis foo)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20", "--found-via", "try0"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_custom_found_via_tool_unlocks(self):
        # A non-default trigger name (e.g. a hypothetical `quickcheck`) supplied via
        # --found-via is honoured end-to-end.
        path = transcript_blocks(
            use("repl_quickcheck"),
            result("Found: by (metis foo)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20", "--found-via", "quickcheck"])
            self.assertEqual(code, 0)
        finally:
            os.remove(path)

    def test_block_message_uses_found_via_and_remediation(self):
        # The block message is generated from --found-via and --remediation, carrying
        # no hard-coded proof-tool names or MCP verbs.
        code, err = run_hook(
            thy_write("lemma x by metis"),
            ["--allow", "simp", "--found-via", "sledgehammer", "try0",
             "--remediation", "Fix: poke the oracle."],
        )
        self.assertEqual(code, 2)
        self.assertIn("sledgehammer/try0", err)
        self.assertIn("Fix: poke the oracle.", err)
        self.assertNotIn("repl_sledgehammer", err)

    def test_pide_in_theory_search_get_state_find_unlocks(self):
        # A PIDE-style MCP has no search tool: sledgehammer runs by being inserted
        # into the theory via `edit`, and its find is read back from `get_state`.
        # That pair must count as evidence, or the hatch is dead on such servers.
        path = transcript_blocks(
            use("mcp__isabelle-pide-mcp__edit",
                {"origin": "Scratch.thy", "text": "sledgehammer", "mode": "append"},
                call_id="e1"),
            result("ok", call_id="e1"),
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q1"),
            result("sledgehammer: Try this: by (metis foo)", call_id="q1"),
        )
        try:
            payload = thy_write("lemma x by (metis foo)")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_get_state_find_without_in_theory_search_still_blocks(self):
        # A get_state result mentioning the method is not evidence by itself: without
        # an arming in-theory search the output is just file state, not a find.
        path = transcript_blocks(
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q1"),
            result("lemma foo ... by metis ...", call_id="q1"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_in_theory_search_inside_comment_does_not_arm(self):
        # `sledgehammer` written inside a comment is prose, not a run -- a later
        # get_state result naming the method must not unlock.
        path = transcript_blocks(
            use("mcp__isabelle-pide-mcp__edit",
                {"origin": "Scratch.thy", "text": "(* sledgehammer next *)"},
                call_id="e1"),
            result("ok", call_id="e1"),
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q1"),
            result("... by metis ...", call_id="q1"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_edit_after_in_theory_search_invalidates_its_find(self):
        # Another completed edit between the in-theory search and the query result
        # moved the file state, so the find no longer describes it.
        path = transcript_blocks(
            use("mcp__isabelle-pide-mcp__edit",
                {"origin": "Scratch.thy", "text": "sledgehammer"}, call_id="e1"),
            result("ok", call_id="e1"),
            use("mcp__isabelle-pide-mcp__edit",
                {"origin": "Scratch.thy", "text": "definition d where d = x"},
                call_id="e2"),
            result("ok", call_id="e2"),
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q1"),
            result("Try this: by (metis foo)", call_id="q1"),
        )
        try:
            payload = thy_write("lemma x by (metis foo)")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_removing_in_theory_search_command_does_not_rearm(self):
        # The cleanup str-replace that DELETES the inserted `sledgehammer` reproduces
        # it only in old_text; the changed text is the replacement. It must not arm a
        # fresh search, or every cleanup would mint evidence from stale state output.
        path = transcript_blocks(
            use("mcp__isabelle-pide-mcp__edit",
                {"origin": "Scratch.thy", "old_text": "sledgehammer\nqed",
                 "text": "qed"},
                call_id="e1"),
            result("ok", call_id="e1"),
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q1"),
            result("... by metis ...", call_id="q1"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_repeated_get_state_polls_are_one_find_not_two(self):
        # Polling the same in-theory find twice must not mint two evidence keys:
        # one search authorizes one closer, however often its output is re-read.
        path = transcript_blocks(
            use("mcp__isabelle-pide-mcp__edit",
                {"origin": "Scratch.thy", "text": "sledgehammer"}, call_id="e1"),
            result("ok", call_id="e1"),
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q1"),
            result("Try this: by (metis foo)", call_id="q1"),
            use("mcp__isabelle-pide-mcp__get_state", {"origin": "Scratch.thy"},
                call_id="q2"),
            result("Try this: by (metis foo)", call_id="q2"),
        )
        try:
            payload = thy_write(
                "lemma a: A by (metis foo)\nlemma b: B by (metis foo)")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_second_search_does_not_invalidate_first_find(self):
        # Searches are read-only: a later sledgehammer run (even a failed one) must
        # not erase evidence for a method an earlier run genuinely found.
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="s1"),
            result("Try this: by (metis foo)", call_id="s1"),
            use("repl_sledgehammer", call_id="s2"),
            result("No proof found.", call_id="s2"),
        )
        try:
            payload = thy_write("lemma x by (metis foo)")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_two_searches_authorize_two_closers_in_one_write(self):
        # Two finds from two searches coexist, so one write may add both closers --
        # while a single find still cannot authorize two closers (see
        # test_one_search_result_cannot_authorize_two_closers_in_one_write).
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="s1"),
            result("Try this: by (metis foo)", call_id="s1"),
            use("repl_sledgehammer", call_id="s2"),
            result("Try this: by auto", call_id="s2"),
        )
        try:
            payload = thy_write("lemma a: A by (metis foo)\nlemma b: B by auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_method_in_later_unrelated_result_still_blocks(self):
        # The hammer's OWN result found nothing; a later, unrelated result (e.g. a Read
        # of another theory) merely mentions the method. That must not rescue the guess
        # -- the method must appear in the hammer's own result (#W2).
        path = transcript_blocks(
            use("repl_sledgehammer"),
            result("No proof found."),
            use("Read", {"file_path": "Other.thy"}),
            result("lemma foo ... by metis ..."),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_sledgehammer_found_other_method_still_blocks(self):
        # sledgehammer found `smt`, but the write guesses `metis` -> still blocked.
        path = transcript_blocks(
            use("repl_sledgehammer"),
            result("Try this: by (smt (z3) foo)"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_try0_as_prose_in_edit_does_not_escape(self):
        # try0 appearing only in an Edit's *content* (prose), with no actual try0 or
        # sledgehammer run, must not unlock a guess -- even if some result names the
        # method. Guards against the whole-input substring scan (#5).
        path = transcript_blocks(
            use("Edit", {"new_string": "we should try0 this goal"}),
            result("Try this: by metis"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_codex_function_call_shape_escape_hatch(self):
        # A non-Claude transcript (Codex/OpenCode function-call blocks, bare per line)
        # must still be understood, or the hatch would be dead there and a legitimately
        # found method would always be over-blocked (#4).
        path = transcript_blocks(
            use("repl_sledgehammer", kind="function_call"),
            result("Try this: by (metis foo)", kind="function_call_output"),
            envelope=False,
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0)
        finally:
            os.remove(path)

    def test_incidental_try0_substring_does_not_escape(self):
        # A path like try0_results.thy mentions "try0" but is not a try0 run; the
        # word boundary must keep it from tripping the escape hatch.
        path = transcript_with_calls(("Edit", {"file_path": "experiments/try0_results.thy"}))
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_incidental_try0_tool_name_suffix_does_not_escape(self):
        path = transcript_blocks(
            use("try0_results", call_id="not_search"),
            result("Try this: by metis", call_id="not_search"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_stale_sledgehammer_still_blocks(self):
        # sledgehammer is 3 calls back, but the window only sees the last 1.
        path = transcript_with("repl_sledgehammer", "Edit", "Edit")
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "1"])
            self.assertEqual(code, 2)
        finally:
            os.remove(path)

    def test_first_non_allowed_method_after_allowed_blocks(self):
        # The by_method_names loop must SKIP an allow-listed closer and still
        # block a later non-allowed one in the same write -- not just look at the
        # first `by`. Every existing case has a single method; this exercises the loop.
        code, err = run_hook(thy_write("lemma a by auto\nlemma b by metis"),
                             ["--allow", "auto", "simp"])
        self.assertEqual(code, 2)
        self.assertIn("metis", err)

    def test_non_searchable_package_methods_pass(self):
        args = ["--searchable", "auto", "metis", "simp"]
        for method in ("pat_completeness", "lexicographic_order"):
            code, err = run_hook(thy_write("lemma x by " + method), args)
            self.assertEqual(code, 0, (method, err))

    def test_package_prefix_does_not_exempt_searchable_auto(self):
        code, err = run_hook(
            thy_write("lemma x by pat_completeness auto"),
            ["--searchable", "auto", "metis", "simp"],
        )
        self.assertEqual(code, 2)
        self.assertIn("method `auto`", err)

    def test_package_prefix_with_found_auto_passes(self):
        path = transcript_blocks(
            use("repl_step", {"isar_text": "try0"}, call_id="search-1"),
            result("Try this: by auto", call_id="search-1"),
        )
        try:
            payload = thy_write("lemma x by pat_completeness auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "auto", "metis", "simp"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_interleaved_id_pairs_keep_hammer_result(self):
        path = transcript_blocks(
            use("repl_sledgehammer", call_id="hammer"),
            use("Read", {"file_path": "Other.thy"}, call_id="read"),
            result("Try this: by (metis foo)", call_id="hammer"),
            result("unrelated", call_id="read"),
        )
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--searchable", "metis"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_diagnostic_reports_path_line_and_source(self):
        code, err = run_hook(
            thy_write("lemma a by pat_completeness\nlemma b by auto"),
            ["--searchable", "auto"],
        )
        self.assertEqual(code, 2)
        self.assertIn("Location: Foo.thy:2", err)
        self.assertIn("Source: lemma b by auto", err)

    def test_compound_closer_blocks_on_terminal_method(self):
        # The reported hole: `by unfold_locales auto` runs `auto` (a blind guess) after
        # the allow-listed `unfold_locales`. The terminal method must be checked too, so
        # this blocks on `auto` even though the first token is allow-listed.
        code, err = run_hook(thy_write("lemma x by unfold_locales auto"),
                             ["--allow", "unfold_locales", "rule"])
        self.assertEqual(code, 2)
        self.assertIn("auto", err)

    def test_compound_closer_both_allowed_passes(self):
        # A compound closer whose BOTH methods are allow-listed is fine.
        code, err = run_hook(thy_write("lemma x by unfold_locales standard"),
                             ["--allow", "unfold_locales", "standard"])
        self.assertEqual(code, 0, err)

    def test_paren_combinator_compound_blocks(self):
        # The parenthesised form of the same smuggle -- `(unfold_locales, auto)` -- must
        # also be mined for every method, blocking on `auto`.
        code, err = run_hook(thy_write("lemma x by (unfold_locales, auto)"),
                             ["--allow", "unfold_locales"])
        self.assertEqual(code, 2)
        self.assertIn("auto", err)

    def test_paren_combinator_method_after_arg_bearing_method_blocks(self):
        # `by (rule exI, auto)`: `rule` takes the argument `exI`, then the `,` starts a
        # second method `auto` -- which must be flagged. The argument `exI` sitting
        # between the method name and the combinator must not hide the `auto`.
        code, err = run_hook(thy_write("lemma x by(rule exI, auto)"),
                             ["--allow", "rule", "intro"])
        self.assertEqual(code, 2)
        self.assertIn("auto", err)
        self.assertNotIn("method `exI`", err)

    def test_all_paren_combinators_start_a_new_method(self):
        # Each method combinator -- `,` (sequential), `;` (structured), `|` (alternative)
        # -- starts a fresh method-name position, so the method after it is checked. All
        # three forms of `rule exI <comb> auto` must block on `auto`.
        for comb in (",", ";", "|"):
            code, err = run_hook(thy_write("lemma x by(rule exI %s auto)" % comb),
                                 ["--allow", "rule", "intro"])
            self.assertEqual(code, 2, "combinator %r should block" % comb)
            self.assertIn("auto", err)

    def test_method_argument_is_not_flagged(self):
        # A method's ARGUMENTS are not methods: `by (rule foo)` applies `rule` (allowed);
        # `foo` is a fact argument and must not be read as a guessed method.
        code, err = run_hook(thy_write("lemma x by (rule foo)"), ["--allow", "rule"])
        self.assertEqual(code, 0, err)

    def test_simp_modifier_argument_not_flagged(self):
        # `by (auto simp: foo)` applies only `auto`; `simp`/`foo` are modifier+fact args.
        # It blocks on `auto` (not allow-listed) -- but never on `foo` (would be a false
        # positive: `foo` is not a method).
        code, err = run_hook(thy_write("lemma x by (auto simp: foo)"), ["--allow", "rule"])
        self.assertEqual(code, 2)
        self.assertIn("auto", err)
        self.assertNotIn("`by foo`", err)

    def test_paren_argument_group_not_flagged(self):
        # A parenthesised method argument (`metis (full_types)`) is an argument group,
        # not a nested method -- `full_types` must not be mined as a method name.
        code, err = run_hook(thy_write("lemma x by (metis (full_types) foo)"),
                             ["--allow", "rule"])
        self.assertEqual(code, 2)
        self.assertIn("metis", err)
        self.assertNotIn("method `full_types`", err)

    def test_bracket_argument_not_flagged(self):
        # Bracketed instantiation args (`rule exI[of _ x]`) are arguments; `exI`/`of`/`x`
        # are not methods. `rule` is allow-listed -> passes with no false positive.
        code, err = run_hook(thy_write("lemma x by (rule exI[of _ x])"), ["--allow", "rule"])
        self.assertEqual(code, 0, err)

    def test_next_command_after_allowed_by_not_flagged(self):
        # After an allow-listed single-method closer, the NEXT command on the following
        # line must not be read as a second method (`lemma`/`theorem`/... are keywords,
        # and the scan is bounded to method1's own line anyway).
        code, err = run_hook(thy_write("lemma a by rule\nlemma b: True by rule"),
                             ["--allow", "rule"])
        self.assertEqual(code, 0, err)

    def test_same_line_keyword_after_by_not_flagged(self):
        # A proof-flow keyword sharing the line with a completed `by` (`by simp then
        # show ...`) must not be mis-read as method2.
        code, err = run_hook(thy_write("have p: \"x\" by simp then show ?thesis by rule"),
                             ["--allow", "simp", "rule"])
        self.assertEqual(code, 0, err)

    def test_compound_closer_escape_hatch_on_terminal(self):
        # The escape hatch applies to the terminal method of a compound closer: if
        # sledgehammer found `auto`, then `by unfold_locales auto` is not a guess.
        path = transcript_blocks(
            use("repl_sledgehammer"),
            result("Try this: by auto (0.2 s)"),
        )
        try:
            payload = thy_write("lemma x by unfold_locales auto")
            payload["transcript_path"] = path
            code, err = run_hook(payload, ["--window", "20", "--allow", "unfold_locales"])
            self.assertEqual(code, 0, err)
        finally:
            os.remove(path)

    def test_by_in_unicode_cartouche_prose_passes(self):
        # Prose written as `text ‹ … ›` (the project convention) may contain an
        # English "by"; it is not a proof closer and must not block.
        code, _ = run_hook(thy_write("text ‹discharged by reflexivity›\nlemma x by auto"),
                           ["--allow", "auto"])
        self.assertEqual(code, 0)

    def test_thy_extension_case_insensitive_blocks(self):
        # A guessed closer in Foo.THY (== Foo.thy on macOS) must still block.
        code, _ = run_hook({"tool_name": "Write",
                            "tool_input": {"file_path": "Foo.THY", "content": "by metis"}},
                           ["--allow", "simp"])
        self.assertEqual(code, 2)

    def test_nonexistent_transcript_still_blocks(self):
        # A missing transcript path can't prove a recent sledgehammer, so a guess
        # is still blocked (the os.path.exists guard returns False -> no escape).
        payload = thy_write("lemma x by metis")
        payload["transcript_path"] = "/no/such/transcript.jsonl"
        code, _ = run_hook(payload, ["--window", "20"])
        self.assertEqual(code, 2)

    def test_bare_event_transcript_escape_hatch(self):
        # Some transcripts store blocks under a top-level "content" without a
        # {"message": ...} envelope; the parser must still find the tool_use/result.
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"content": [
                {"type": "tool_use", "name": "repl_sledgehammer", "input": {}},
                {"type": "tool_result", "content": "Try this: by (metis foo)"},
            ]}) + "\n")
        try:
            payload = thy_write("lemma x by metis")
            payload["transcript_path"] = path
            code, _ = run_hook(payload, ["--window", "20"])
            self.assertEqual(code, 0)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
