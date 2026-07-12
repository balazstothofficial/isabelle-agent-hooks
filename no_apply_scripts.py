#!/usr/bin/env python3
"""PreToolUse guard: forbid apply-style proof scripts in Isabelle theories.

Enforces the isabelle.md proof-discipline rule "No apply scripts -- structured
Isar only" (rendered as CLAUDE.md/AGENTS.md per agent) at the harness level.
Unconditional: there is no escape hatch -- `apply` is never
allowed (unlike a guessed `by`, an apply-script is not made acceptable by a
preceding sledgehammer/try0).

Blocks a write to an Isabelle theory when the ADDED text contains an apply-script
(`apply ...`). Covers Write / Edit / MultiEdit / Bash, Codex's apply_patch and
functions.exec, the iq-dev MCP write_file/save_file, and the isabelle-pide-mcp
`edit` tool. Non-theory writes are never blocked. Fails OPEN on internal errors.
"""
import sys, re

try:
    from isabelle_hooks.protocol import BLOCK_EXIT
    from isabelle_hooks.edits import extract_thy_edits
except Exception as _e:
    # Fail OPEN, but say so: a missing/broken helper silently disables the guard.
    # Non-blocking (still exit 0) so it can never brick the workflow.
    sys.stderr.write(
        "[no-apply-scripts hook] shared helper unavailable (%s); guard disabled.\n" % _e
    )
    sys.exit(0)

# `apply` as a standalone word, wherever it sits on the line: line-start
# (`apply auto`), `apply (`, or mid-line after other tokens (`lemma x apply auto`).
# `\b...\b` keeps identifiers like `apply_cong`, `map_apply` or `applyTac` from
# matching (the surrounding `_`/letters are word chars, so there is no boundary).
# The negative lookahead excludes primed Isabelle identifiers such as the `apply'`
# locale parameter in Abstract_Substitution; the apostrophe is not a Python word
# character, so a trailing `\b` by itself would misclassify it as the outer command.
APPLY = re.compile(r"\bapply\b(?!')")


def main():
    fragments, _ = extract_thy_edits()
    if fragments is None:
        sys.exit(0)
    if not any(APPLY.search(fragment.text) for fragment in fragments):
        sys.exit(0)

    msg = (
        "\n[no-apply-scripts hook] BLOCKED write to an Isabelle theory.\n"
        "It contains an `apply` script.\n\n"
        "PROOF DISCIPLINE (see the project instructions): no `apply` at all -- use structured Isar "
        "(`proof ... qed`, `by`, `using ... by`, `unfolding ... by`, "
        "`proof (rule ...)`).\n"
    )
    sys.stderr.write(msg)
    sys.exit(BLOCK_EXIT)  # stderr is fed back to the model


if __name__ == "__main__":
    main()
