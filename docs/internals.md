# Internals

Reference material for people integrating with the hook or reading the code. For
what the hook does and how to install it, see the [README](../README.md).

## Hook contract

`isabelle_guards.py` reads one JSON object from standard input:

```json
{
  "tool_name": "Write",
  "tool_input": {"file_path": "Example.thy", "content": "lemma x: True by simp"},
  "transcript_path": "/path/to/session.jsonl"
}
```

- Exit `0`: allow the call.
- Exit `2`: block it; the reason is printed on standard error.
- Malformed input, unrecognized tools, missing dependencies, and internal errors
  all exit `0` (fail open).

Every supported tool shape (plain writes, edits, `apply_patch`, shell heredocs,
Codex `functions.exec` literals,
[AutoCorrode](https://github.com/awslabs/AutoCorrode) I/Q writes,
[isabelle-pide-mcp](https://github.com/kappelmann/isabelle-pide-mcp)
edits) is normalized into one shared old/new mutation view before either policy
runs, so `--policies` changes evaluation only, never what counts as an edit.

## Evidence markers for search integrations

By default, search evidence is matched from the natural-language output of
`sledgehammer`/`try0` results in the transcript. A search integration can bind its
evidence to a specific goal instead by including this line in its result:

```text
ISABELLE_HOOK_EVIDENCE {"method":"metis","goal":"<goal-fingerprint>"}
```

The marker must occur in the paired result of a recognized search call. Goal-bound
evidence is preferred over natural-language matching when both exist. Each search
result is single-use, and completing a state-changing operation invalidates
outstanding evidence.

OpenCode search tools may instead return

```json
{"isabelleHookEvidence": {"method": "metis", "goal": "..."}}
```

and the adapter converts that structured field to the canonical marker before
recording the result.

## Semantic fingerprint provider

Renames and other semantically unchanged refactors can be verified by an external
PIDE bridge, configured with `ISABELLE_HOOKS_SEMANTIC_FINGERPRINT_COMMAND` or
`--semantic-fingerprint-command`. The command receives JSON on standard input:

```json
{
  "version": 1,
  "path": "Example.thy",
  "before": {"source": "...", "units": ["..."]},
  "after": {"source": "...", "units": ["..."]}
}
```

and returns parallel proposition/context fingerprint lists:

```json
{"before": ["fingerprint"], "after": ["fingerprint"]}
```

Null entries fall back to structural matching. Provider failure also falls back to
structural matching; the provider can only confirm equivalence, never authorize a
refactor on its own.

## Searchable-method registry

`refresh_searchable_methods.py [isabelle-command]` runs
`isabelle process_theories` over `Hook_Searchable_Methods.thy` and caches the set
of methods sledgehammer/try0 can return. The cache key covers the configured
command, the launcher's identity, the query version, and hashes of the relevant
Isabelle search sources — so changing or upgrading Isabelle invalidates it.
`ISABELLE_HOOKS_IDENTITY` can supply a deployment-owned identity when a stable
wrapper path would otherwise hide Isabelle upgrades.

The write hook only ever reads this cache; a missing or stale registry switches to
the conservative fallback (evidence required for every method not in `--allow`)
instead of making a write wait for discovery.

## Code layout

[`isabelle_hooks/guard.py`](../isabelle_hooks/guard.py) is the policy-evaluation
entry point; start there when reading the code. Edit normalization is in
`edits.py`, relocation matching and the fingerprint-provider protocol in
`relocations.py`, transcript evidence in `transcript.py`, and registry refresh
plus hot-path loading in `discovery.py`.
