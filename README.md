# Isabelle agent hooks

Experimental, best-effort PreToolUse enforcement for two Isabelle proof-discipline
rules:

- use structured Isar instead of `apply` scripts;
- use search-discoverable proof methods only with proof-search provenance.

The single `isabelle_guards.py` entry point normalizes an edit once and applies both
policies. It supports standard coding-agent writes, `apply_patch`, literal nested calls
inside Codex `functions.exec`, AutoCorrode I/Q writes, and `isabelle-pide-mcp` edits.
Unsupported or malformed calls fail open rather than blocking the agent.

## Proof provenance

A search-discoverable closer is accepted from either source:

1. a recent `sledgehammer`/`try0` result that names the method;
2. an existing proof unit relocated within the same atomic edit.

Search integrations can bind evidence to a goal by including this line in their result:

```text
ISABELLE_HOOK_EVIDENCE {"method":"metis","goal":"<goal-fingerprint>"}
```

The marker must occur in the paired result of a recognized search call. Goal-bound
evidence is preferred; natural-language result matching remains the fallback for search
tools that do not yet emit markers. Each search result is single-use, and a completed
state-changing operation invalidates outstanding evidence.

OpenCode search tools may instead return
`{"isabelleHookEvidence":{"method":"metis","goal":"..."}}`; the adapter converts
that structured field to the canonical marker before recording the result.

Relocations are matched one-for-one by file, lexical context, statement, and closer.
Reordering an existing proof unit is allowed, while copying it or moving only its method
to a different statement is not. If static matching is inconclusive, normal search
provenance is required.

### Semantic refactor provider

Renames and other semantically unchanged refactors can be verified by an external PIDE
bridge. Configure its command with `ISABELLE_HOOKS_SEMANTIC_FINGERPRINT_COMMAND` or
`--semantic-fingerprint-command`. The command receives JSON on standard input:

```json
{
  "version": 1,
  "path": "Example.thy",
  "before": {"source": "...", "units": ["..."]},
  "after": {"source": "...", "units": ["..."]}
}
```

It returns parallel proposition/context fingerprint lists:

```json
{"before": ["fingerprint"], "after": ["fingerprint"]}
```

Null entries fall back to structural matching. Provider failure also falls back safely;
it never authorizes a refactor on its own.

## Searchable-method registry

The write hook never launches Isabelle. Prepare or refresh its registry during setup or
after changing Isabelle:

```sh
python3 refresh_searchable_methods.py [isabelle-command]
```

The prepared manifest is keyed from the configured command, launcher identity, query
version, and query source. `ISABELLE_HOOKS_IDENTITY` can supply a deployment-owned
identity when a stable wrapper path does not reflect Isabelle upgrades. A missing or
stale manifest uses the conservative fallback immediately rather than making a write
wait for `isabelle process_theories`.

## Hook contract

The entry point reads one JSON object from standard input:

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

Install these together:

- `isabelle_guards.py`
- `refresh_searchable_methods.py`
- `Hook_Searchable_Methods.thy`
- the complete `isabelle_hooks/` package
- `skills/isabelle-proof-hooks/SKILL.md` in the agent's project skill directory

`isabelle_guards.py` accepts:

- `--policies apply-script guessed-proof` (both by default; select either one alone)
- `--window N`
- `--allow M1 M2 ...` for the conservative no-registry fallback
- `--found-via T1 T2 ...`
- `--remediation TEXT`
- `--isabelle-command CMD`
- `--searchable M1 M2 ...` for diagnostics/tests
- `--semantic-fingerprint-command CMD`

## Installation

For Claude Code, install the bundle under `.claude/hooks/` and configure one
PreToolUse command hook:

```json
{"type":"command","command":"python3 .claude/hooks/isabelle_guards.py"}
```

Install only one policy by adding, for example,
`--policies guessed-proof` or `--policies apply-script`. Policy selection changes
evaluation only; edit extraction remains the same shared, single-pass implementation.
Install the bundled skill as `.claude/skills/isabelle-proof-hooks/SKILL.md`.

For Codex, install it under `.codex/hooks/` and configure the same single command in
`.codex/hooks.json` or the adjacent `config.toml`. If Codex may start below the project
root, resolve the root before invoking the script. Project hooks must be approved in
Codex settings. Install the skill as `.agents/skills/isabelle-proof-hooks/SKILL.md`.

For OpenCode, install the Python bundle and `guards.json` under `.opencode/hooks/`, and
copy `opencode-guard.ts` to `.opencode/plugins/`. The plugin maintains a per-worktree
transcript and invokes the configured guard once per matching call. Install the skill as
`.opencode/skills/isabelle-proof-hooks/SKILL.md`.

[`guards.json`](guards.json) is the OpenCode configuration and a matcher reference for
other agents. Its matcher covers recognized write, edit, shell, MCP, patch, and optional
`functions.exec` calls. Only configure tools whose mutation payload is understood.

## Code layout

`isabelle_hooks/guard.py` owns policy evaluation and configuration. `edits.py` produces
the shared old/new mutation view, `relocations.py` owns proof-unit provenance and the
semantic-provider protocol, `transcript.py` owns search evidence, and `discovery.py`
owns explicit registry refresh plus hot-path manifest loading. Contract and adapter
tests live in `test/`.
