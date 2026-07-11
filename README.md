# Isabelle agent hooks

PreToolUse guards that reject Isabelle `apply` scripts and proof methods guessed
without recent proof-search evidence.

## Contract

Each script reads one JSON object from standard input:

```json
{
  "tool_name": "Write",
  "tool_input": {"file_path": "Example.thy", "content": "lemma x: True by simp"},
  "transcript_path": "/path/to/session.jsonl"
}
```

- Exit `0`: allow the tool call.
- Exit `2`: block it and return the explanation on standard error.
- Malformed input and internal errors fail open.

Install `no_apply_scripts.py` and `no_guessed_proofs.py` beside
`isabelle_hook_common.py`. The guessed-proof guard also requires
`Hook_Searchable_Methods.thy`.

## Options

`no_apply_scripts.py` takes no arguments. `no_guessed_proofs.py` accepts:

- `--window N`: number of recent transcript tool calls to inspect.
- `--allow M1 M2 ...`: fallback methods if Isabelle discovery and caches fail.
- `--found-via T1 T2 ...`: proof-search tools whose results count as evidence.
- `--remediation TEXT`: replacement `Fix:` text in the block message.
- `--isabelle-command CMD`: pinned Isabelle launcher used for discovery.
- `--searchable M1 M2 ...`: explicit searchable-method override for diagnostics.

## Agent wiring

Claude Code uses `.claude/settings.local.json`; Codex uses the same PreToolUse shape
in `.codex/hooks.json`. A command entry is:

```json
{
  "type": "command",
  "command": "python3 HOOK_DIR/no_guessed_proofs.py --window 30 --found-via sledgehammer try0"
}
```

OpenCode needs a `tool.execute.before` adapter that maps its tool name and arguments
to `tool_name` and `tool_input`, sends the JSON to each Python process, rejects exit
`2` with stderr, and allows exit `0`.

## Matcher contract

The caller's edit-tool matcher must stay synchronized with
`isabelle_hook_common.py`:

- `PATH_KEYS` identifies edited-file fields.
- `CONTENT_KEYS` identifies added-text fields.
- `NEW_KEYS` and `OLD_KEYS` describe replacements.
- `BASH_TOOL`, `PATCH_TOOL_SUBSTR`, and `MCP_WRITE_SUBSTRS` select specialized
  extraction branches.

Every matched tool must expose recognized path/content fields or have a specialized
branch. Add a representative payload test before extending the matcher.
