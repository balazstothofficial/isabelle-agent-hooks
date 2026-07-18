#!/usr/bin/env python3
"""Single PreToolUse guard for Isabelle proof discipline."""
import sys

try:
    from isabelle_hooks.edits import extract_thy_edits
    from isabelle_hooks.guard import evaluate, parse_config
    from isabelle_hooks.protocol import BLOCK_EXIT
except Exception as exc:
    sys.stderr.write(
        "[isabelle-theory-guard] shared helper unavailable (%s); guard disabled.\n"
        % exc)
    sys.exit(0)


def main():
    config = parse_config(sys.argv[1:])
    fragments, transcript = extract_thy_edits()
    if fragments is None:
        return 0
    violation = evaluate(fragments, transcript, config)
    if violation is None:
        return 0
    sys.stderr.write(violation.message)
    return BLOCK_EXIT


if __name__ == "__main__":
    sys.exit(main())
