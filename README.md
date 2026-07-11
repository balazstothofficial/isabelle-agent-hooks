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

OpenCode has no JSON hook config, so this repo ships a ready-to-use plugin,
`opencode-guard.ts`, that does the `tool.execute.before` bridging for you (it maps
OpenCode's tool name and arguments to `tool_name`/`tool_input`, sends the JSON to
each Python guard, rejects exit `2` with stderr, allows exit `0`, and logs a paired
tool call + result transcript so the guessed-proof escape hatch keeps working).

To install it in a project:

1. Copy the guards beside `isabelle_hook_common.py` into `.opencode/hooks/`
   (`no_apply_scripts.py`, `no_guessed_proofs.py`, `isabelle_hook_common.py`,
   `Hook_Searchable_Methods.thy`).
2. Copy `guards.json` into `.opencode/hooks/` too, and edit its hook list/args to
   taste.
3. Copy `opencode-guard.ts` into `.opencode/plugins/`.

`guards.json` configures the plugin (read once at load from the hooks directory):

```json
{
  "interpreter": "python3",
  "hooks": [
    { "script": "no_guessed_proofs.py", "matcher": "Write|Edit|MultiEdit|Bash",
      "args": ["--window", "30", "--found-via", "sledgehammer", "try0"] },
    { "script": "no_apply_scripts.py", "matcher": "Write|Edit|MultiEdit|Bash" }
  ]
}
```

- `interpreter` is optional; it defaults to `$ISABELLE_HOOKS_PYTHON`, then `python3`
  on `PATH`. Point it at a specific interpreter when the guards need pinned deps.
- Each `matcher` is a JavaScript regex tested against both OpenCode's tool id
  (`bash`, `edit`, …) and its mapped Claude name (`Bash`, `Edit`, …).
- A missing or invalid `guards.json` fails open (no guards run) rather than blocking
  every tool call.

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
