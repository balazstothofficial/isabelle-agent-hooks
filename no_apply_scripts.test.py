#!/usr/bin/env python3
"""Unit tests for the no_apply_scripts.py PreToolUse guard.

The guard unconditionally blocks any apply-script in an Isabelle theory write
(there is no escape hatch). It reads stdin JSON and exits 0 (allow) or 2 (block,
explanation on stderr); it is driven here as a subprocess -- the real harness
contract -- so the test sees the exact exit code the agent harness
(Claude/Codex/OpenCode) acts on.

Run directly: python3 no_apply_scripts.test.py
"""
import os
import unittest

from hook_test_support import run_hook as run_hook_process, run_without_package, thy_write

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
APPLY = os.path.join(HOOKS_DIR, "no_apply_scripts.py")


def run_hook(payload):
    return run_hook_process(APPLY, payload)


class NoApplyScripts(unittest.TestCase):
    def test_missing_shared_package_fails_open_cleanly(self):
        code, err = run_without_package(APPLY, thy_write("apply auto"))
        self.assertEqual(code, 0, err)
        self.assertIn("shared helper unavailable", err)
        self.assertNotIn("Traceback", err)

    def test_apply_paren_blocks(self):
        code, err = run_hook(thy_write("lemma x\n  apply (rule foo)"))
        self.assertEqual(code, 2)
        self.assertIn("apply", err)

    def test_leading_apply_blocks(self):
        code, _ = run_hook(thy_write("lemma x\napply auto"))
        self.assertEqual(code, 2)

    def test_midline_apply_blocks(self):
        # `apply` need not start the line: a bare method after other tokens on the
        # same line is still an apply-script and must be blocked.
        code, _ = run_hook(thy_write("lemma x apply auto"))
        self.assertEqual(code, 2)

    def test_applyfoo_is_not_apply(self):
        code, _ = run_hook(thy_write("definition applyfoo where x"))
        self.assertEqual(code, 0)

    def test_apply_inside_identifiers_not_blocked(self):
        # `map_apply` / `apply_cong` embed "apply" but are not the apply command;
        # the word boundaries must keep them out.
        code, _ = run_hook(thy_write("lemma map_apply: foo using apply_cong by simp"))
        self.assertEqual(code, 0)

    def test_primed_apply_identifiers_not_blocked(self):
        for name in ("apply'", "apply''"):
            code, err = run_hook(thy_write("fixes %s :: nat" % name))
            self.assertEqual(code, 0, (name, err))

    def test_apply_tac_identifier_not_blocked(self):
        code, err = run_hook(thy_write("definition applyTac where x"))
        self.assertEqual(code, 0, err)

    def test_apply_in_inner_syntax_ignored(self):
        samples = [
            'assumes "apply a f = a"',
            'notes "apply"',
            r'text \<open>apply auto\<close>',
            'text ‹apply auto›',
        ]
        for sample in samples:
            code, err = run_hook(thy_write(sample))
            self.assertEqual(code, 0, (sample, err))

    def test_apply_in_comment_ignored(self):
        code, _ = run_hook(thy_write("lemma x (* apply (rule foo) *) by simp"))
        self.assertEqual(code, 0)

    def test_non_thy_allowed(self):
        payload = {"tool_name": "Write",
                   "tool_input": {"file_path": "notes.md", "content": "apply (rule foo)"}}
        code, _ = run_hook(payload)
        self.assertEqual(code, 0)

    def test_thy_extension_case_insensitive_blocks(self):
        # Foo.THY is the same file as Foo.thy on a case-insensitive filesystem; an
        # apply-script written there must still be blocked (no case-variant bypass).
        code, _ = run_hook({"tool_name": "Write",
                            "tool_input": {"file_path": "Foo.THY", "content": "apply auto"}})
        self.assertEqual(code, 2)

    def test_apply_patch_blocks(self):
        # Codex edits via apply_patch: a `+ apply` added to a *.thy is blocked too.
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: Foo.thy\n@@\n lemma x:\n+  apply auto\n*** End Patch\n"
        )}}
        code, err = run_hook(payload)
        self.assertEqual(code, 2)
        self.assertIn("apply", err)

    def test_apply_patch_non_thy_allowed(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: notes.md\n+apply (rule foo)\n*** End Patch\n"
        )}}
        code, _ = run_hook(payload)
        self.assertEqual(code, 0)

    def test_apply_patch_move_into_thy_blocks(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: scratch.txt\n*** Move to: Foo.thy\n"
            "@@\n+apply auto\n*** End Patch\n"
        )}}
        code, err = run_hook(payload)
        self.assertEqual(code, 2)
        self.assertIn("apply", err)

    def test_codex_functions_exec_write_file_blocks(self):
        payload = {
            "tool_name": "functions.exec",
            "tool_input": (
                'const r = await tools.mcp__iq_dev__write_file({'
                'path:"Foo.thy",command:"str_replace",old_str:"by simp",'
                'new_str:"apply (rule TrueI) done"}); text(r);'
            ),
        }
        code, err = run_hook(payload)
        self.assertEqual(code, 2)
        self.assertIn("apply", err)

    def test_codex_functions_exec_control_allowed(self):
        payload = {
            "tool_name": "functions.exec",
            "tool_input": (
                'const r = await tools.mcp__iq_dev__write_file({'
                'path:"Foo.thy",command:"str_replace",old_str:"by (rule TrueI)",'
                'new_str:"by simp"}); text(r);'
            ),
        }
        code, err = run_hook(payload)
        self.assertEqual(code, 0, err)

    def test_iq_open_file_create_blocks(self):
        payload = {
            "tool_name": "mcp__iq-dev__open_file",
            "tool_input": {
                "path": "New.thy", "create_if_missing": True,
                "content": "lemma x: True\n  apply auto",
            },
        }
        code, err = run_hook(payload)
        self.assertEqual(code, 2)
        self.assertIn("apply", err)

    def test_codex_functions_exec_can_remove_existing_apply(self):
        payload = {
            "tool_name": "functions.exec",
            "tool_input": (
                'await tools.mcp__iq_dev__write_file({'
                'path:"Foo.thy",command:"str_replace",old_str:"apply auto done",'
                'new_str:"by simp"});'
            ),
        }
        code, err = run_hook(payload)
        self.assertEqual(code, 0, err)

    def test_direct_codex_and_claude_paths_still_block(self):
        payloads = [
            {
                "tool_name": "mcp__iq_dev__write_file",
                "tool_input": {
                    "path": "Foo.thy", "command": "str_replace",
                    "old_str": "by simp", "new_str": "apply auto",
                },
            },
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "Foo.thy", "old_string": "by simp",
                    "new_string": "apply auto",
                },
            },
        ]
        for payload in payloads:
            code, err = run_hook(payload)
            self.assertEqual(code, 2, (payload["tool_name"], err))


if __name__ == "__main__":
    unittest.main()
