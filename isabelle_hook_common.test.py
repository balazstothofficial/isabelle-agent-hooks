#!/usr/bin/env python3
"""Unit tests for the shared Isabelle hook parser (isabelle_hook_common.py).

extract_thy_edit() is the single parsing surface behind both proof guards
(no_apply_scripts.py, no_guessed_proofs.py), so a bug here mis-fires *both*. It
reads the hook's stdin JSON and returns (check_text, transcript) where check_text
is the added theory text with comments/cartouches/strings stripped, or None when
the call does not target a *.thy file.

Run directly: python3 isabelle_hook_common.test.py
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isabelle_hook_common as hook
from isabelle_hooks import protocol


def run(payload):
    """Feed `payload` (a dict) as stdin JSON and return extract_thy_edit()."""
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return hook.extract_thy_edit()
    finally:
        sys.stdin = saved


def run_fragments(payload):
    """Feed a payload and return the structured target-aware extraction result."""
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return hook.extract_thy_edits()
    finally:
        sys.stdin = saved


def thy_file(content):
    """Write `content` to a temp *.thy file; return its path (caller removes it)."""
    fd, path = tempfile.mkstemp(suffix=".thy")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class ProtocolRecognition(unittest.TestCase):
    def test_namespaced_tool_aliases_match(self):
        self.assertTrue(protocol.is_mcp_write_tool("mcp__iq_dev__write_file"))
        self.assertTrue(protocol.is_edit_tool_name("namespace.apply_patch"))
        self.assertTrue(protocol.is_exec_tool("functions.exec"))

    def test_incidental_tool_name_substrings_do_not_match(self):
        self.assertFalse(protocol.is_edit_tool_name("credit_report"))
        self.assertFalse(protocol.is_mcp_write_tool("overwrite_filename"))
        self.assertFalse(protocol.tool_name_ends_with("try0_results", "try0"))


class ToolCoverage(unittest.TestCase):
    def test_write_thy(self):
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "Foo.thy", "content": "lemma x by auto"},
        })
        self.assertIsNotNone(check)
        self.assertIn("by auto", check)

    def test_whole_buffer_existing_file_checks_only_added_lines(self):
        old = 'theory T imports Main begin\nlemma old: "True" by auto\nend\n'
        path = thy_file(old)
        try:
            new = old.replace("\nend\n", '\nlemma new: "True" by metis\nend\n')
            fragments, _ = run_fragments({
                "tool_name": "Write", "tool_input": {"file_path": path, "content": new},
            })
            joined = "\n".join(f.text for f in fragments)
            self.assertIn("by metis", joined)
            self.assertNotIn("by auto", joined)
            self.assertEqual(fragments[0].path, path)
        finally:
            os.remove(path)

    def test_whole_buffer_added_method_keeps_preceding_by_as_context(self):
        old = "theory T imports Main begin\nlemma x: True\n  by\nend\n"
        path = thy_file(old)
        try:
            new = old.replace("end\n", "  auto\nend\n")
            fragments, _ = run_fragments({
                "tool_name": "Write", "tool_input": {"file_path": path, "content": new},
            })
            self.assertEqual(fragments[0].text.strip(), "auto")
            self.assertIn("by\n  auto", fragments[0].context_text)
            self.assertIsNotNone(fragments[0].changed_ranges)
        finally:
            os.remove(path)

    def test_whole_buffer_new_file_checks_complete_content(self):
        path = os.path.join(tempfile.gettempdir(), "nonexistent-hook-new-file.thy")
        if os.path.exists(path):
            os.remove(path)
        fragments, _ = run_fragments({
            "tool_name": "Write",
            "tool_input": {"file_path": path, "content": "lemma a by auto\nlemma b by metis"},
        })
        joined = "\n".join(f.text for f in fragments)
        self.assertIn("by auto", joined)
        self.assertIn("by metis", joined)

    def test_replace_all_seeds_each_prose_and_code_occurrence(self):
        path = thy_file("theory T imports Main begin\ntext ‹\nanchor\n›\nanchor\nend\n")
        try:
            fragments, _ = run_fragments({
                "tool_name": "Edit",
                "tool_input": {"file_path": path, "old_string": "anchor",
                               "new_string": "anchor\nby auto", "replace_all": True},
            })
            self.assertEqual(len(fragments), 2)
            self.assertNotIn("by auto", fragments[0].text)
            self.assertIn("by auto", fragments[1].text)
        finally:
            os.remove(path)

    def test_multiedit_uses_prior_deletion_to_expose_later_code(self):
        path = thy_file("text ‹intro›\ntext ‹PLACEHOLDER›\n")
        try:
            fragments, _ = run_fragments({
                "tool_name": "MultiEdit",
                "tool_input": {"file_path": path, "edits": [
                    {"old_string": "text ‹intro›\ntext ‹",
                     "new_string": "text ‹intro›\n"},
                    {"old_string": "PLACEHOLDER›", "new_string": "by auto"},
                ]},
            })
            self.assertIn("by auto", "\n".join(f.text for f in fragments))
        finally:
            os.remove(path)

    def test_multiedit_uses_prior_insertion_to_hide_later_prose(self):
        path = thy_file("text ‹intro›\nPLACEHOLDER\n")
        try:
            fragments, _ = run_fragments({
                "tool_name": "MultiEdit",
                "tool_input": {"file_path": path, "edits": [
                    {"old_string": "text ‹intro›\n",
                     "new_string": "text ‹intro›\ntext ‹"},
                    {"old_string": "PLACEHOLDER\n", "new_string": "by auto›\n"},
                ]},
            })
            self.assertNotIn("by auto", "\n".join(f.text for f in fragments))
        finally:
            os.remove(path)

    def test_functions_exec_nested_writes_share_ordered_snapshot(self):
        path = thy_file("text ‹intro›\ntext ‹PLACEHOLDER›\n")
        try:
            calls = [
                {"path": path, "old_str": "text ‹intro›\ntext ‹",
                 "new_str": "text ‹intro›\n"},
                {"path": path, "old_str": "PLACEHOLDER›", "new_str": "by auto"},
            ]
            source = "\n".join(
                "await tools.mcp__iq_dev__write_file(" + json.dumps(call) + ");"
                for call in calls
            )
            fragments, _ = run_fragments({
                "tool_name": "functions.exec", "tool_input": {"code": source},
            })
            self.assertIn("by auto", "\n".join(f.text for f in fragments))
        finally:
            os.remove(path)

    def test_functions_exec_unresolved_replace_keeps_fragment_fallback(self):
        path = thy_file("theory T imports Main begin\nend\n")
        try:
            call = {"path": path, "old_str": "missing", "new_str": "by auto"}
            source = "await tools.mcp__iq_dev__write_file(" + json.dumps(call) + ");"
            fragments, _ = run_fragments({
                "tool_name": "functions.exec", "tool_input": {"code": source},
            })
            self.assertIn("by auto", "\n".join(f.text for f in fragments))
        finally:
            os.remove(path)

    def test_apply_patch_fragments_keep_their_paths(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"input": (
            "*** Begin Patch\n*** Update File: One.thy\n@@\n+lemma a by auto\n"
            "*** Update File: Two.thy\n@@\n+lemma b by metis\n*** End Patch\n"
        )}}
        fragments, _ = run_fragments(payload)
        self.assertEqual([(f.path, f.source) for f in fragments],
                         [("One.thy", "lemma a by auto"), ("Two.thy", "lemma b by metis")])

    def test_edit_new_string(self):
        check, _ = run({
            "tool_name": "Edit",
            "tool_input": {"file_path": "Foo.thy", "new_string": "by simp"},
        })
        self.assertIn("by simp", check)

    def test_edit_replace_excludes_unchanged_anchor(self):
        # A str-replace that reproduces a pre-existing `by auto` anchor line and appends
        # new content: only the appended lines are ADDED; the anchor's closer must not
        # leak into check_text (else it is re-flagged as a fresh guess).
        anchor = "lemma old:\n  unfolding foo_def by auto"
        check, _ = run({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "Foo.thy",
                "old_string": anchor,
                "new_string": anchor + "\n\nlemma new:\n  by (rule refl)",
            },
        })
        self.assertNotIn("by auto", check)
        self.assertIn("by (rule refl)", check)

    def test_edit_replace_keeps_changed_closer(self):
        # The changed region itself introduces a new closer -> it IS checked. Guards
        # against the anchor-strip swallowing the genuinely new line.
        check, _ = run({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "Foo.thy",
                "old_string": "lemma x:\n  sorry",
                "new_string": "lemma x:\n  by metis",
            },
        })
        self.assertIn("by metis", check)

    def test_edit_replace_anchor_between_old_and_new_stripped(self):
        # Anchor lines shared as a TRAILING block are stripped too, not just leading.
        check, _ = run({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "Foo.thy",
                "old_string": "lemma tail:\n  by auto",
                "new_string": "lemma head:\n  by metis\n\nlemma tail:\n  by auto",
            },
        })
        self.assertIn("by metis", check)
        self.assertNotIn("by auto", check)

    def test_mcp_str_replace_excludes_anchor(self):
        # The MCP write_file str_replace command (old_str/new_str) gets the same anchor
        # treatment -- this is the exact tool/shape from the reported false positive.
        anchor = "lemma toy:\n  unfolding foo_def by auto"
        check, _ = run({
            "tool_name": "mcp__iq-release__write_file",
            "tool_input": {
                "path": "Foo.thy",
                "command": "str_replace",
                "old_str": anchor,
                "new_str": anchor + "\n\nlemma extra:\n  by (rule notE)",
            },
        })
        self.assertNotIn("by auto", check)
        self.assertIn("by (rule notE)", check)

    def test_multiedit_per_edit_anchor_stripped(self):
        # MultiEdit's per-edit old/new pairs are reduced individually.
        check, _ = run({
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "Foo.thy",
                "edits": [
                    {"old_string": "lemma a:\n  by auto",
                     "new_string": "lemma a:\n  by auto\nlemma b:\n  by blast"},
                ],
            },
        })
        self.assertIn("by blast", check)
        self.assertNotIn("by auto", check)

    def test_insert_without_old_checks_all(self):
        # An insert (new_str with no old_str) has no anchor to subtract: all of it is new.
        check, _ = run({
            "tool_name": "mcp__iq-release__write_file",
            "tool_input": {"path": "Foo.thy", "command": "insert",
                           "insert_line": 5, "new_str": "  by auto"},
        })
        self.assertIn("by auto", check)

    def test_str_replace_inside_cartouche_is_prose(self):
        # The reported false positive: a str-replace whose changed block lands inside a
        # `text \<open>..\<close>` block -- whose delimiters are OUTSIDE the fragment --
        # must be read as PROSE. "checked by a countermodel" must NOT surface as a `by`
        # closer. The edit site is located by reading the file on disk.
        anchor = "  until then the bare parameters alone carry the fork."
        path = thy_file(
            "theory Soziale imports Main begin\n\n"
            "text \\<open>\n  Intro prose about forks.\n" + anchor + "\n\\<close>\n\n"
            "lemma real: \"True\" by simp\nend\n"
        )
        try:
            check, _ = run({
                "tool_name": "mcp__iq-release__write_file",
                "tool_input": {
                    "path": path,
                    "command": "str_replace",
                    "old_str": anchor,
                    "new_str": anchor + "\n  the fork is checked by a countermodel here",
                },
            })
            self.assertNotIn("by a", check)
        finally:
            os.remove(path)

    def test_str_replace_inside_unicode_cartouche_is_prose(self):
        # Same, with the Unicode cartouche notation ‹ .. › the project actually uses.
        anchor = "  bare parameters alone carry the fork."
        path = thy_file(
            "theory Soziale imports Main begin\n\n"
            "text ‹\n  Intro prose.\n" + anchor + "\n›\n\nlemma r: \"True\" by simp\nend\n"
        )
        try:
            check, _ = run({
                "tool_name": "mcp__iq-release__write_file",
                "tool_input": {
                    "path": path,
                    "command": "str_replace",
                    "old_str": anchor,
                    "new_str": anchor + "\n  as shown by a countermodel below",
                },
            })
            self.assertNotIn("by a", check)
        finally:
            os.remove(path)

    def test_str_replace_in_code_still_checked_with_file(self):
        # Seeding must NOT suppress a real edit: a str-replace on a CODE line (outside any
        # prose) in a real file still yields the closer for checking.
        path = thy_file(
            "theory Foo imports Main begin\n\nlemma x: \"True\"\n  sorry\nend\n"
        )
        try:
            check, _ = run({
                "tool_name": "mcp__iq-release__write_file",
                "tool_input": {
                    "path": path,
                    "command": "str_replace",
                    "old_str": "lemma x: \"True\"\n  sorry",
                    "new_str": "lemma x: \"True\"\n  by metis",
                },
            })
            self.assertIn("by metis", check)
        finally:
            os.remove(path)

    def test_str_replace_cartouche_notation_mismatch_still_seeds(self):
        # The file on disk uses the ASCII cartouche `\<open>..\<close>`, but the edit is
        # phrased with the Unicode `‹..›` (Isabelle treats them as the same symbols and
        # the MCP matches modulo that). The edit-site seed must still locate the site, so
        # prose "by construction" deep inside the block is not misread as a `by` closer.
        anchor = "  Every formalized item below is introduced"
        path = thy_file(
            "theory T imports Main begin\n\ntext \\<open>\n" + anchor
            + "\n  in place.\n\\<close>\n\nlemma r: \"True\" by simp\nend\n"
        )
        try:
            check, _ = run({
                "tool_name": "mcp__iq-release__write_file",
                "tool_input": {
                    "path": path,
                    "command": "str_replace",
                    # edit uses the Unicode notation, not matching the file's ASCII form
                    "old_str": "begin\n\ntext ‹\n" + anchor,
                    "new_str": "begin\n\ntext ‹\n  some claims hold only\n"
                               "  by construction of our encoding\n›\n\ntext ‹\n" + anchor,
                },
            })
            self.assertNotIn("by construction", check)
        finally:
            os.remove(path)

    def test_edit_site_seed_tolerates_trailing_whitespace(self):
        # The file carries trailing whitespace on the anchor lines (and the PIDE `edit`
        # tool matches old_text "modulo trailing whitespace"); the edit-site lookup must
        # still locate the block so prose "by construction" inside it stays prose.
        anchor = "  Every formalized item below is introduced"
        path = thy_file(
            "theory T imports Main begin\n\ntext \\<open>   \n" + anchor + "  \n"
            "  in place.\n\\<close>\n\nlemma r: \"True\" by simp\nend\n"
        )
        try:
            check, _ = run({
                "tool_name": "mcp__isabelle-pide-mcp__edit",
                "tool_input": {
                    "origin": path, "mode": "replace",
                    # no trailing whitespace here; file has it
                    "old_text": "begin\n\ntext ‹\n" + anchor,
                    "text": "begin\n\ntext ‹\n  some hold only\n"
                            "  by construction of our encoding\n›\n\ntext ‹\n" + anchor,
                },
            })
            self.assertNotIn("by construction", check)
        finally:
            os.remove(path)

    def test_str_replace_missing_file_falls_back_to_fragment(self):
        # If the file can't be read/located, seeding yields None and the fragment is
        # scanned as-is (fail toward the prior behaviour, never an exception).
        check, _ = run({
            "tool_name": "mcp__iq-release__write_file",
            "tool_input": {
                "path": "/no/such/Theory.thy",
                "command": "str_replace",
                "old_str": "lemma x:\n  sorry",
                "new_str": "lemma x:\n  by metis",
            },
        })
        self.assertIn("by metis", check)

    def test_multiedit_collects_all_edits(self):
        check, _ = run({
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "Foo.thy",
                "edits": [
                    {"new_string": "lemma a"},
                    {"new_string": "by blast"},
                ],
            },
        })
        self.assertIn("by blast", check)

    def test_bash_requires_thy_substring(self):
        # A Bash command touching a .thy passes through (content is checked).
        check, _ = run({
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'by auto' > Foo.thy"},
        })
        self.assertIsNotNone(check)
        # A Bash command with no .thy anywhere is not our concern.
        check, _ = run({
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'by auto' > notes.md"},
        })
        self.assertIsNone(check)

    def test_mcp_write_file_unknown_param(self):
        # MCP write tools may use an unrecognised param name; the fallback scans
        # all string values once the path gate is satisfied by a .thy origin.
        check, _ = run({
            "tool_name": "mcp__iq-dev__write_file",
            "tool_input": {"origin": "Foo.thy", "weird_body_key": "by metis"},
        })
        self.assertIn("by metis", check)

    def test_codex_functions_exec_unwraps_mcp_write_file(self):
        check, transcript = run({
            "tool_name": "functions.exec",
            "tool_input": (
                'const r = await tools.mcp__iq_dev__write_file({'
                'path:"Foo.thy",command:"str_replace",old_str:"by simp",'
                'new_str:"apply (rule TrueI) done"}); text(r);'
            ),
            "transcript_path": "/tmp/codex.jsonl",
        })
        self.assertIn("apply (rule TrueI) done", check)
        self.assertNotIn("by simp", check)
        self.assertEqual(transcript, "/tmp/codex.jsonl")

    def test_codex_functions_exec_dict_envelope(self):
        check, _ = run({
            "tool_name": "functions.exec",
            "tool_input": {"code": (
                'await tools.mcp__iq_dev__write_file({'
                'path:"Foo.thy",command:"insert",new_str:"apply auto"});'
            )},
        })
        self.assertIn("apply auto", check)

    def test_codex_functions_exec_ignores_tool_text_in_a_string(self):
        check, _ = run({
            "tool_name": "functions.exec",
            "tool_input": (
                'text("tools.mcp__iq_dev__write_file({'
                'path:\\"Foo.thy\\",new_str:\\"apply auto\\"})");'
            ),
        })
        self.assertIsNone(check)

    def test_pide_mcp_edit_origin(self):
        check, _ = run({
            "tool_name": "mcp__isabelle-pide-mcp__edit",
            "tool_input": {"origin": "Scratch.thy", "text": "by force"},
        })
        self.assertIn("by force", check)

    def test_pide_mcp_edit_replace_excludes_anchor(self):
        # The PIDE `edit` tool (mode=replace) pairs old_text (current lines) with text
        # (new lines) -- the same anchor treatment as str_replace: a `by auto` carried
        # over unchanged in text must not be re-flagged.
        old = "lemma toy:\n  unfolding foo_def by auto"
        check, _ = run({
            "tool_name": "mcp__isabelle-pide-mcp__edit",
            "tool_input": {
                "origin": "Foo.thy", "mode": "replace",
                "old_text": old,
                "text": old + "\n\nlemma extra:\n  by (rule notE)",
            },
        })
        self.assertNotIn("by auto", check)
        self.assertIn("by (rule notE)", check)

    def test_pide_mcp_edit_replace_keeps_new_closer(self):
        # A genuinely new closer in a PIDE replace is still surfaced for checking.
        check, _ = run({
            "tool_name": "mcp__isabelle-pide-mcp__edit",
            "tool_input": {
                "origin": "Foo.thy", "mode": "replace",
                "old_text": "lemma x:\n  sorry",
                "text": "lemma x:\n  by metis",
            },
        })
        self.assertIn("by metis", check)

    def test_pide_mcp_edit_replace_inside_cartouche_is_prose(self):
        # PIDE replace on prose deep inside a `text ‹..›` block: the edit-site seed
        # (read from the file via old_text) makes "checked by a countermodel" prose.
        anchor = "  bare parameters alone carry the fork."
        path = thy_file(
            "theory S imports Main begin\n\ntext ‹\n  Intro.\n"
            + anchor + "\n›\n\nlemma r: \"True\" by simp\nend\n"
        )
        try:
            check, _ = run({
                "tool_name": "mcp__isabelle-pide-mcp__edit",
                "tool_input": {
                    "origin": path, "mode": "replace",
                    "old_text": anchor,
                    "text": anchor + "\n  as shown by a countermodel below",
                },
            })
            self.assertNotIn("by a", check)
        finally:
            os.remove(path)

    def test_pide_mcp_edit_session_origin_not_checked(self):
        # A session-qualified origin (e.g. "HOL.Nat") is not a *.thy path: not a file we
        # guard (base session files can't be edited), so it must return None.
        check, _ = run({
            "tool_name": "mcp__isabelle-pide-mcp__edit",
            "tool_input": {"origin": "HOL.Nat", "mode": "replace",
                           "old_text": "x", "text": "by auto"},
        })
        self.assertIsNone(check)


class ApplyPatch(unittest.TestCase):
    """Codex's apply_patch envelope (also reachable via a Bash heredoc)."""

    def _patch(self, body):
        return "*** Begin Patch\n" + body + "*** End Patch\n"

    def test_update_thy_added_lines(self):
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: Foo.thy\n@@\n lemma x:\n-  sorry\n+  by simp\n"
            )},
        })
        self.assertIsNotNone(check)
        self.assertIn("by simp", check)
        # Removed/context lines are not added text.
        self.assertNotIn("sorry", check)

    def test_add_file_thy(self):
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Add File: New.thy\n+theory New\n+  apply auto\n"
            )},
        })
        self.assertIn("apply auto", check)

    def test_non_thy_section_ignored(self):
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: notes.md\n+by auto\n"
            )},
        })
        self.assertIsNone(check)

    def test_only_thy_section_of_multi_file_patch(self):
        # A '+ apply' in a non-theory section must not be attributed to the theory.
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: README.md\n+apply makes no sense here\n"
                "*** Update File: Foo.thy\n+by blast\n"
            )},
        })
        self.assertIn("by blast", check)
        self.assertNotIn("makes no sense", check)

    def test_patch_via_bash_heredoc(self):
        # An apply_patch heredoc inside a Bash command is detected by the marker and
        # parsed per-file (better than dumping the whole command).
        check, _ = run({
            "tool_name": "Bash",
            "tool_input": {"command": (
                "apply_patch <<'EOF'\n"
                "*** Begin Patch\n*** Update File: Foo.thy\n+by metis\n*** End Patch\nEOF\n"
            )},
        })
        self.assertIn("by metis", check)

    def test_patch_with_no_added_lines_yields_none(self):
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: Foo.thy\n@@\n-  old\n"
            )},
        })
        self.assertIsNone(check)

    def test_thy_file_header_is_case_insensitive(self):
        # The per-file *.thy attribution must also be case-insensitive.
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: Foo.THY\n+  apply auto\n"
            )},
        })
        self.assertIsNotNone(check)
        self.assertIn("apply auto", check)

    def test_move_into_thy_uses_destination_path(self):
        fragments, _ = run_fragments({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: scratch.txt\n"
                "*** Move to: Foo.thy\n"
                "@@\n+lemma x by auto\n"
            )},
        })
        self.assertEqual([f.path for f in fragments], ["Foo.thy"])
        self.assertIn("by auto", fragments[0].text)

    def test_patch_hunk_retains_context_for_cross_boundary_syntax(self):
        fragments, _ = run_fragments({
            "tool_name": "apply_patch",
            "tool_input": {"input": self._patch(
                "*** Update File: Foo.thy\n@@\n lemma x: True\n  by\n+  auto\n"
            )},
        })
        self.assertEqual(fragments[0].text.strip(), "auto")
        self.assertIn("by\n  auto", fragments[0].context_text)


class PatchMarkerScoping(unittest.TestCase):
    """A "*** Begin Patch" marker is only an apply_patch envelope for the apply_patch
    tool or a Bash heredoc. In any other tool it is incidental content and must not
    hijack the edit -- neither to suppress the check nor to mis-target it."""

    def test_marker_in_write_content_does_not_bypass(self):
        # A Write to a *.thy whose content merely starts with the marker (no *.thy
        # patch section) must STILL be checked -- otherwise it is a trivial bypass.
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "Foo.thy",
                           "content": "*** Begin Patch\nlemma x by metis"},
        })
        self.assertIsNotNone(check)
        self.assertIn("by metis", check)

    def test_patch_shaped_content_to_non_thy_is_not_checked(self):
        # A Write to a NON-theory file whose content happens to contain a patch that
        # names a *.thy must be gated by the real target (notes.md), not the embedded
        # header -- i.e. not over-blocked.
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "notes.md",
                           "content": "*** Begin Patch\n*** Update File: Foo.thy\n+by metis\n"},
        })
        self.assertIsNone(check)

    def test_real_apply_patch_tool_still_parsed(self):
        # Regression guard: the genuine apply_patch tool is unaffected by the scoping.
        check, _ = run({
            "tool_name": "apply_patch",
            "tool_input": {"input":
                "*** Begin Patch\n*** Update File: Foo.thy\n+by metis\n*** End Patch\n"},
        })
        self.assertIn("by metis", check)


class ThyGating(unittest.TestCase):
    def test_non_thy_path_ignored(self):
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "settings.json", "content": "by auto"},
        })
        self.assertIsNone(check)

    def test_thy_extension_is_case_insensitive(self):
        # On a case-insensitive filesystem (macOS) Foo.THY *is* Foo.thy, so the gate
        # must recognise the case variant -- otherwise it is a trivial bypass. Both
        # the path form (Write) and the Bash-substring form must catch it.
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "Foo.THY", "content": "lemma x by auto"},
        })
        self.assertIsNotNone(check)
        self.assertIn("by auto", check)
        check, _ = run({
            "tool_name": "Bash",
            "tool_input": {"command": "printf 'by auto' > Foo.THY"},
        })
        self.assertIsNotNone(check)

    def test_session_qualified_name_is_not_a_thy_path(self):
        # A session-qualified theory name (HOL.Nat) is not a *.thy path.
        check, _ = run({
            "tool_name": "mcp__isabelle-pide-mcp__edit",
            "tool_input": {"origin": "HOL.Nat", "text": "by auto"},
        })
        self.assertIsNone(check)


class BashWriteTarget(unittest.TestCase):
    """A Bash command counts as a theory edit only when a *.thy is an actual write
    target -- not when it is merely mentioned (comment, grep pattern, read-only arg)."""

    def _bash(self, command):
        return run({"tool_name": "Bash", "tool_input": {"command": command}})[0]

    def test_redirect_write_is_checked(self):
        self.assertIsNotNone(self._bash("echo 'by auto' > Foo.thy"))
        self.assertIsNotNone(self._bash("echo 'by auto' >> Foo.thy"))

    def test_redirect_case_insensitive(self):
        self.assertIsNotNone(self._bash("printf 'by auto' > Foo.THY"))

    def test_heredoc_write_is_checked(self):
        self.assertIsNotNone(self._bash(
            "cat > Foo.thy <<'EOF'\nby auto\nEOF\n"
        ))

    def test_tee_and_copy_move_are_checked(self):
        self.assertIsNotNone(self._bash("echo x | tee Foo.thy"))
        self.assertIsNotNone(self._bash("tee -a Foo.thy < in"))
        self.assertIsNotNone(self._bash("cp scratch Foo.thy"))
        self.assertIsNotNone(self._bash("mv a.thy Foo.thy"))

    def test_incidental_mention_does_not_fire(self):
        # The reported regression: a prose "by" in a command that writes to a NON-thy
        # file, while only mentioning a .thy in a comment, must not be flagged.
        self.assertIsNone(self._bash("echo 'proved by hand' >> notes.md  # see Foo.thy"))

    def test_read_only_mention_does_not_fire(self):
        self.assertIsNone(self._bash("grep 'by' Foo.thy"))
        self.assertIsNone(self._bash("cat Foo.thy | head"))

    def test_interpolated_path_with_literal_thy_is_checked(self):
        # A redirect target that is variable-interpolated but still carries the literal
        # `.thy` (a common shape) must arm the guard (#3).
        self.assertIsNotNone(self._bash('echo lemma > "$dir/Foo.thy"'))
        self.assertIsNotNone(self._bash("echo lemma > ${OUT}/Bar.thy"))

    def test_thy_as_source_of_copy_does_not_fire(self):
        # A theory used as the SOURCE of cp/mv (destination is elsewhere) is a read,
        # not an edit, so an unrelated "by" on the line must not be flagged (#6).
        self.assertIsNone(self._bash("cp Foo.thy backup/  # kept by me"))
        self.assertIsNone(self._bash("mv Foo.thy /tmp/archive/"))
        # But a theory as the DESTINATION is a real write.
        self.assertIsNotNone(self._bash("cp template Foo.thy"))
        self.assertIsNotNone(self._bash("mv draft.txt Real.thy"))

    def test_operand_does_not_span_newlines(self):
        # A cp on one line must not reach a .thy mentioned on a LATER line (e.g. in a
        # following comment) -- its operands live on the same line (#W4).
        self.assertIsNone(self._bash("cp scratch dest\necho done  # ref Foo.thy"))


class Stripping(unittest.TestCase):
    def test_strips_line_and_block_comments(self):
        check = hook._strip("hello (* by auto *) world")
        self.assertNotIn("by auto", check)

    def test_strips_cartouches(self):
        check = hook._strip(r"text \<open>see by auto here\<close> done")
        self.assertNotIn("by auto", check)

    def test_strips_nested_cartouches(self):
        # Cartouches nest: an inner \<open>..\<close> must not end the outer span early
        # and leak its tail. A non-greedy regex stopped at the inner close and
        # exposed "by reflexivity" in the comment tail.
        check = hook._strip(
            r"\<comment>\<open>unified \<open>across\<close> merges, "
            r"discharge by reflexivity\<close>" + "\nlocale foo"
        )
        self.assertNotIn("by reflexivity", check)
        self.assertIn("locale foo", check)

    def test_strips_unicode_cartouches(self):
        # The project convention writes prose as `text ‹ … ›` with the Unicode
        # cartouche glyphs (U+2039/U+203A), not the ASCII \<open>…\<close> form. An
        # English "by" inside such prose must not leak to the `by <method>` matcher.
        check = hook._strip("text ‹see by auto here› done")
        self.assertNotIn("by auto", check)
        self.assertIn("done", check)

    def test_strips_nested_unicode_cartouches(self):
        # Unicode cartouches nest just like the ASCII form: an inner ‹…› must not end
        # the outer span early and leak its tail.
        check = hook._strip("text ‹unified ‹across› merges, discharge by reflexivity›\nlocale foo")
        self.assertNotIn("by reflexivity", check)
        self.assertIn("locale foo", check)

    def test_strips_mixed_ascii_and_unicode_cartouches(self):
        # A block may mix forms; both are stripped independently.
        check = hook._strip(r"\<open>outer by simp\<close> and ‹inner by blast›")
        self.assertNotIn("by simp", check)
        self.assertNotIn("by blast", check)

    def test_cartouche_marker_inside_string_does_not_swallow_code(self):
        # A \<open> (or ‹) inside a "string literal" is verbatim text there -- it must
        # NOT open a cartouche span that eats a following real `by <method>`. With the
        # old pass-based strip this leaked: the string char armed a bogus cartouche and
        # `by metis` vanished, so the guard silently did not fire.
        check = hook._strip(r'lemma "\<open>" by metis')
        self.assertIn("by metis", check)
        check = hook._strip('lemma "‹" by metis')
        self.assertIn("by metis", check)

    def test_literal_close_of_other_notation_does_not_end_cartouche(self):
        # A \<close> written as prose inside a ‹..› cartouche is interior text -- it must
        # not close the cartouche early and leak the following prose `by metis`.
        check = hook._strip(r"text ‹ discuss the \<close> symbol, then by metis ›")
        self.assertNotIn("by metis", check)
        # Symmetric: a Unicode › inside a \<open>..\<close> cartouche is interior text.
        check = hook._strip(r"text \<open> the › glyph, then by simp \<close>")
        self.assertNotIn("by simp", check)

    def test_quote_inside_comment_is_not_a_string_start(self):
        # A `"` inside a comment is text; it must not start a string that then swallows
        # real code after the comment closes.
        check = hook._strip('(* a " b *) lemma y by auto')
        self.assertIn("by auto", check)
        self.assertNotIn("by metis", check)

    def test_strips_nested_ml_comments(self):
        # ML comments nest too: (* a (* b *) by simp *) must not leak "by simp".
        check = hook._strip("lemma x (* note (* inner *) by simp *) end")
        self.assertNotIn("by simp", check)
        self.assertIn("lemma x", check)
        self.assertIn("end", check)

    def test_keeps_code_after_nested_comment(self):
        # A real closer following a nested comment is still seen.
        check = hook._strip("(* skip (* me *) by blast *)\nlemma y by auto")
        self.assertNotIn("by blast", check)
        self.assertIn("by auto", check)

    def test_strips_string_literals(self):
        check = hook._strip('note "by auto" end')
        self.assertNotIn("by auto", check)

    def test_keeps_real_code(self):
        check = hook._strip("lemma foo by auto")
        self.assertIn("by auto", check)

    def test_extract_strips_comment_in_real_payload(self):
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "Foo.thy", "content": "(* by auto *) lemma x"},
        })
        # Real code present (so not None), but the commented method is stripped.
        self.assertNotIn("by auto", check)


class FailOpen(unittest.TestCase):
    def test_unparseable_stdin(self):
        saved = sys.stdin
        sys.stdin = io.StringIO("{not json")
        try:
            check, transcript = hook.extract_thy_edit()
        finally:
            sys.stdin = saved
        self.assertIsNone(check)
        self.assertEqual(transcript, "")

    def test_non_dict_input(self):
        check, _ = run([1, 2, 3])
        self.assertIsNone(check)

    def test_non_dict_tool_input(self):
        check, transcript = run({
            "tool_name": "Write",
            "tool_input": "oops",
            "transcript_path": "/tmp/t.jsonl",
        })
        self.assertIsNone(check)
        self.assertEqual(transcript, "/tmp/t.jsonl")

    def test_empty_text_yields_none(self):
        check, _ = run({
            "tool_name": "Write",
            "tool_input": {"file_path": "Foo.thy", "content": "   "},
        })
        self.assertIsNone(check)


if __name__ == "__main__":
    unittest.main()
