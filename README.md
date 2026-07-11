# Isabelle agent hooks

Agent-neutral guards for Isabelle theory edits. The guards reject apply-style proof
scripts and search-discoverable proof methods that were guessed instead of obtained
from recent proof search. They are configured by the caller; repository-specific
policy does not live here.

## Hook contract

Each hook is invoked as a command before an edit tool runs. It reads one JSON object
from standard input:

```json
{
  "tool_name": "Write",
  "tool_input": {"file_path": "Example.thy", "content": "lemma x: True by simp"},
  "transcript_path": "/path/to/session.jsonl"
}
```

The scripts use the Claude/Codex PreToolUse convention:

- exit `0` to allow the tool call;
- exit `2` to block it, with the explanation written to standard error;
- fail open on malformed input or internal errors so a broken guard cannot disable
  editing altogether.

`no_apply_scripts.py` needs no arguments. `no_guessed_proofs.py` accepts:

- `--window N`: inspect the last `N` transcript tool calls for proof-search evidence;
- `--allow M1 M2 ...`: conservative fallback methods, used only when discovery from
  pinned Isabelle and all matching caches are unavailable;
- `--found-via T1 T2 ...`: proof-search tool names whose results count as evidence that
  a method was found;
- `--remediation TEXT`: replacement text for the block message's `Fix:` line.

It also supports `--isabelle-command CMD` to select the pinned Isabelle launcher and
`--searchable M1 M2 ...` as a diagnostic/test override for the searchable-method
registry. Unknown or malformed arguments are ignored.

The two scripts must be installed beside `isabelle_hook_common.py`; the guessed-proof
guard also needs `Hook_Searchable_Methods.thy` beside it.

## Wiring

The matcher must cover every editing tool whose payload the shared parser understands.
For example:

```text
Write|Edit|MultiEdit|Bash|apply_patch|.*write_file|.*save_file|mcp__isabelle[-_]pide[-_]mcp__edit
```

Claude Code's `.claude/settings.local.json` can invoke the guards like this (replace
`HOOK_DIR` and choose your policy values):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash|apply_patch|.*write_file|.*save_file|mcp__isabelle[-_]pide[-_]mcp__edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 HOOK_DIR/no_guessed_proofs.py --window 30 --allow assumption rule intro --found-via sledgehammer try0 --remediation 'Fix: run proof search, then write the method it returns.'"
          },
          {
            "type": "command",
            "command": "python3 HOOK_DIR/no_apply_scripts.py"
          }
        ]
      }
    ]
  }
}
```

Codex accepts the same PreToolUse shape in `.codex/hooks.json`. Keep the command and
matcher identical, changing only the installed hook directory if necessary.

OpenCode does not expose that subprocess contract directly. Its plugin adapter should:

1. handle `tool.execute.before`;
2. map the OpenCode tool name and arguments to `tool_name` and `tool_input`;
3. write that JSON object to each Python process on stdin;
4. treat process exit `2` as a rejected tool call and surface stderr to the model;
5. allow exit `0`, and fail open for other adapter/process failures.

This keeps all three agents on the same parser and policy arguments even though
OpenCode needs a small TypeScript-to-Python bridge.

## Matcher/parser synchronization contract

The caller owns the matcher and this repository owns payload extraction. They must be
updated together across that boundary:

- `isabelle_hook_common.py`'s `PATH_KEYS` lists payload fields that can identify the
  edited file;
- `CONTENT_KEYS` lists fields that can carry added text;
- replace operations additionally use `NEW_KEYS`/`OLD_KEYS`;
- `BASH_TOOL`, `PATCH_TOOL_SUBSTR`, and `MCP_WRITE_SUBSTRS` select payload-specific
  extraction branches.

Every tool admitted by the matcher must either provide a recognized path/content pair
or have a matching name-specific branch. Otherwise a content-bearing non-Bash edit
without a recognized path deliberately takes the conservative path gate and may be
over-blocked. When adding a matcher alternative, first add or verify a representative
payload test in `isabelle_hook_common.test.py`, then update both sides together.

## Development

Run one suite directly for a fast edit/test loop:

```bash
python3 no_guessed_proofs.test.py
python3 no_apply_scripts.test.py
python3 isabelle_hook_common.test.py
```

Run Ruff and all suites:

```bash
ruff check --select F .
python3 isabelle_hook_common.test.py
python3 no_guessed_proofs.test.py
python3 no_apply_scripts.test.py
```

See `transcript-schema.md` for transcript-parser verification against real agent
sessions.
