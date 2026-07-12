# Isabelle agent hooks

PreToolUse guards that enforce two Isabelle proof-discipline rules:

- use structured Isar instead of `apply` scripts;
- use search-discoverable proof methods only after recent `sledgehammer` or `try0`
  evidence.

The guards inspect only text added to `.thy` files. Unsupported or malformed calls
fail open rather than blocking the agent.

## Hook contract

Each Python entry point reads one JSON object from standard input:

```json
{
  "tool_name": "Write",
  "tool_input": {"file_path": "Example.thy", "content": "lemma x: True by simp"},
  "transcript_path": "/path/to/session.jsonl"
}
```

- Exit `0`: allow the call.
- Exit `2`: block it and explain why on standard error.
- Malformed input, missing dependencies, and internal errors fail open.

Install these files together:

- `no_apply_scripts.py`
- `no_guessed_proofs.py`
- `isabelle_hook_common.py`
- `Hook_Searchable_Methods.thy`
- the complete `isabelle_hooks/` package

## Configuration

`no_apply_scripts.py` takes no arguments. `no_guessed_proofs.py` accepts:

- `--window N`: recent transcript calls to inspect;
- `--allow M1 M2 ...`: fallback methods when discovery and caches fail;
- `--found-via T1 T2 ...`: proof-search tools that may provide evidence;
- `--remediation TEXT`: replacement `Fix:` text in block messages;
- `--isabelle-command CMD`: Isabelle launcher, defaulting to
  `$ISABELLE_HOOKS_ISABELLE` and then `isabelle` on `PATH`;
- `--searchable M1 M2 ...`: explicit method registry for diagnostics and tests.

Claude Code and Codex can invoke a guard directly from their PreToolUse config:

```json
{
  "type": "command",
  "command": "python3 HOOK_DIR/no_guessed_proofs.py"
}
```

Use `defaultMatcher` from [`guards.json`](guards.json) for both guards. It covers the
standard write/edit tools, common MCP file writers, `apply_patch`, Bash, and optional
`functions.exec` orchestration. Nested literal writes and proof-search calls inside
`functions.exec` are normalized through the same parsers as direct calls.

Only configure tools whose write payload the guards understand. An unrecognized
payload is allowed because the hooks never block text they could not inspect.

## OpenCode

OpenCode uses the included `opencode-guard.ts` plugin to bridge
`tool.execute.before` and `tool.execute.after` to the Python contract. The plugin also
maintains a per-worktree transcript so proof-search evidence remains current and
single-use.

To install it in a project:

1. Copy the Python files, `Hook_Searchable_Methods.thy`, `guards.json`, and the
   `isabelle_hooks/` package into `.opencode/hooks/`.
2. Copy `opencode-guard.ts` into `.opencode/plugins/`.
3. Adjust the hook list or arguments in `.opencode/hooks/guards.json` if needed.

The plugin reads `guards.json` once at startup:

```json
{
  "interpreter": "python3",
  "defaultMatcher": "...",
  "hooks": [
    {"script": "no_guessed_proofs.py"},
    {"script": "no_apply_scripts.py"}
  ]
}
```

`interpreter` is optional and falls back to `$ISABELLE_HOOKS_PYTHON`, then `python3`
on `PATH`. Hooks may override `defaultMatcher` with their own `matcher`. Missing or
invalid configuration fails open and runs no guards.

## Code layout

The two entry points own guard-specific policy, arguments, diagnostics, and control
flow. `isabelle_hooks/config.py` owns built-in defaults; the rest of the package owns
shared protocol, edit, syntax, discovery, method, and transcript mechanisms.
`isabelle_hook_common.py` preserves the historical shared import surface.
