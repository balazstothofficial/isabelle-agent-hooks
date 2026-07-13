# Isabelle agent hooks

Experimental, best-effort PreToolUse guards for two Isabelle proof-discipline rules:

- use structured Isar instead of `apply` scripts;
- use search-discoverable proof methods only after recent `sledgehammer` or `try0`
  evidence.

The guards inspect only text added to `.thy` files. They are guardrails, not a complete
enforcement boundary: unsupported or malformed calls fail open rather than blocking
the agent, and coding-agent hook and transcript interfaces may change over time.

Both AutoCorrode's I/Q MCP server and `isabelle-pide-mcp` are supported, alongside
the coding agents' standard file read/write/edit tools.

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

## Installation by coding agent

### Claude Code

Copy the Python files, `Hook_Searchable_Methods.thy`, and the `isabelle_hooks/`
package into `<repo>/.claude/hooks/`. Add both guards as `PreToolUse` command hooks
in one of these files:

- `<repo>/.claude/settings.local.json` for a local, uncommitted setup;
- `<repo>/.claude/settings.json` for configuration shared with the project.

A command hook entry looks like this:

```json
{
  "type": "command",
  "command": "python3 .claude/hooks/no_guessed_proofs.py"
}
```

Use `defaultMatcher` from [`guards.json`](guards.json) as the matcher for both hook
entries.

### Codex

Copy the same hook bundle into `<repo>/.codex/hooks/`. Add both `PreToolUse` command
hooks to `<repo>/.codex/hooks.json`; Codex also supports inline hooks in the adjacent
`<repo>/.codex/config.toml`. For example, a command entry in `hooks.json` is:

```json
{
  "type": "command",
  "command": "root=\"$(hg root 2>/dev/null || git rev-parse --show-toplevel 2>/dev/null || pwd)\"; python3 \"$root/.codex/hooks/no_guessed_proofs.py\""
}
```

This resolves the repository root from Mercurial or Git before falling back to the
current directory, so the hook still finds its script when Codex starts in a
subdirectory. Use the same prefix for `no_apply_scripts.py`.

Project hooks load only for trusted projects. In the Codex desktop app, open
**Settings → Hooks**, review the discovered hooks, and approve them; unapproved hooks
do not run.

For both Claude Code and Codex, [`guards.json`](guards.json) is only a reference file:
neither agent loads it automatically. Copy its `defaultMatcher` value into the
matcher field of each configured hook. It routes these tool calls to the guards:

- standard write, edit, and shell tools;
- `apply_patch`;
- AutoCorrode I/Q's `write_file`, `save_file`, and `open_file` tools;
- `isabelle-pide-mcp`'s `edit` tool;
- optionally, `functions.exec` orchestration.

After a call matches, the Python code determines whether it actually changes a
`.thy` file; for example, a read-only I/Q `open_file` call is allowed. For
`functions.exec`, nested writes and proof searches are recognized when their tool
names and arguments are literal values in the JavaScript source.

Only configure tools whose write payload the guards understand. An unrecognized
payload is allowed because the hooks never block text they could not inspect.

### OpenCode

OpenCode uses the included `opencode-guard.ts` plugin to bridge
`tool.execute.before` and `tool.execute.after` to the Python contract. The plugin also
maintains a per-worktree transcript so proof-search evidence remains current and
single-use.

To install it:

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
Contract and adapter tests live in `test/`.
