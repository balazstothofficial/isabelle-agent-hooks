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

Use this matcher for both guards:

```text
Write|Edit|MultiEdit|Bash|apply_patch|.*write_file|.*save_file|functions[.]exec
```

The extra alternative is inert in clients that do not expose `functions.exec`, so
the same matchers are safe in Claude, direct-tool Codex, and OpenCode. When a Codex
version wraps a write in `functions.exec`, both guards unwrap literal nested
`write_file`, `apply_patch`, and shell-write calls and check them through the same
direct parser. The guessed-proof guard also normalizes nested proof-search and write
calls in the transcript so evidence remains current and single-use. The shipped
`guards.json` already carries these matchers.

OpenCode has no JSON hook config, so this repo ships a ready-to-use plugin,
`opencode-guard.ts`, that does the `tool.execute.before` bridging for you (it maps
OpenCode's tool name and arguments to `tool_name`/`tool_input`, sends the JSON to
each Python guard, rejects exit `2` with stderr, allows exit `0`, and logs a paired
tool call + result transcript so the guessed-proof escape hatch keeps working).

To install it in a project:

1. Copy these four files into `.opencode/hooks/`: `no_apply_scripts.py`,
   `no_guessed_proofs.py`, `isabelle_hook_common.py`, and
   `Hook_Searchable_Methods.thy`.
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

## Which tools the guards check

For each tool call, the guards look inside it to find the file being edited and the
text being written, then check that text for `apply` scripts and guessed proofs.

You choose which tools they run on with the `matcher` in your hook config (for
example `Write|Edit|MultiEdit|Bash`). The guards already understand the standard
edit tools — `Write`, `Edit`, `MultiEdit`, `Bash`, and common MCP write tools — so
in most setups this works out of the box and you don't need to do anything.

One thing to watch: **the guards can only check a tool whose contents they can
read.** If you point the matcher at an unusual edit tool they don't recognize, they
won't find any text to inspect and will let the edit through without checking it —
they never block a call just because they couldn't read it. So only add a tool to
the matcher if you've confirmed the guards actually catch edits made with it (write
a proof with an `apply` script through that tool and check that it gets blocked). If
a tool you rely on isn't being caught, open an issue.
