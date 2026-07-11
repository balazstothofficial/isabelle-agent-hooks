# Verifying the escape-hatch transcript parsing (Codex / OpenCode)

`no_guessed_proofs.py` asks the repository's pinned Isabelle which methods Try0 and
Sledgehammer can emit. It blocks those methods unless a recent proof-search result
named the same method. Package-specific methods outside the discovered registry are
allowed directly. The registry is obtained lazily through
`Hook_Searchable_Methods.thy` and cached by Isabelle/source identity; `--allow` is only
the conservative fallback if discovery and all matching caches fail.

To match found methods, the
hook reads the **agent's own transcript** (the `transcript_path` the harness passes on
stdin) and parses out the tool calls and their results.

The parsing lives in `no_guessed_proofs.py` in three constants:

- `_USE_TYPES` — content-block `type` values that mean "a tool was called".
- `_RESULT_TYPES` — content-block `type` values that mean "a tool returned a result".
- `_CMD_KEYS` — input fields whose value names the command that ran (for detecting a
  `repl_step "try0"`-style invocation).

plus `_result_text()`, which flattens a result block's `content` / `output` / `text`.
Call IDs (`id` / `call_id`) are paired with result IDs (`tool_use_id` / `call_id`) so
parallel calls cannot steal each other's results. Adjacency is used only for schemas
that expose no IDs. A later proof/file-state-changing call invalidates the result, so
the first theory write effectively consumes it; read-only calls may remain interleaved.
The recency window is supplied by the caller through `--window`.

**These are VERIFIED for Claude** (`tool_use` / `tool_result` inside
`{"message":{"content":[…]}}`). The Codex / OpenCode variants (`function_call`,
`function_call_output`, `tool_call`, `tool_output`, …) are a **best-effort guess**.

**Failure mode if the guess is wrong:** the hook finds no calls/results in that agent's
transcript, so the escape hatch never fires — a method you *did* just find via
sledgehammer is still **blocked**. It fails safe (never a bypass), but it's an annoying
over-block. That's what this doc helps you confirm or refute.

## Method A — behavioural (definitive)

Run the prompt below in an **Isabelle repo configured with these hooks and an MCP**
(e.g. one whose preset sets `mcp = "autocorrode"` or `"pide"`, so `sledgehammer` is
available). The prompt makes the agent:

1. write `by metis` on a trivial goal **without** a prior hammer → expect **BLOCK**;
2. run `sledgehammer` (which reports `metis`), then write `by metis` → expect **ALLOW**.

If step 2 is still blocked, the hook could not read that agent's transcript → the
constants above need the agent's block-type/key names.

## Method B — schema dump (fallback, when you can't get a provable goal)

Point the parser at a **real** Codex/OpenCode session transcript and see what it
extracts. From this directory:

```bash
python3 - "$TRANSCRIPT" <<'PY'
import sys, no_guessed_proofs as g
ev = g._read_events(sys.argv[1], 10_000)
print(f"parsed {len(ev)} events; "
      f"{sum(e.kind=='call' for e in ev)} calls, {sum(e.kind=='result' for e in ev)} results")
for e in ev[-8:]:
    print(e.kind, e.call_id, repr(e.text[:80]))
PY
```

`0 events` (when the file clearly contains tool activity) means the shapes aren't
recognised. Inspect a few raw lines of `$TRANSCRIPT` to find that agent's block `type`
strings and result key, then add them to `_USE_TYPES` / `_RESULT_TYPES` / `_CMD_KEYS` /
`_result_text` and re-run — until calls and results both show up.

Locating `$TRANSCRIPT`: Codex keeps session logs under `~/.codex/` and OpenCode under
`~/.local/share/opencode/` (or `~/.local/state/…`); pick the most recently modified
`*.jsonl` from the session you just ran.

## After any change

Re-run the unit suites and the flake check:

```bash
python3 no_guessed_proofs.test.py && python3 isabelle_hook_common.test.py
nix flake check
```

---

## Prompt — paste into **Codex** (running in a hook-configured Isabelle repo)

> I want to verify that a PreToolUse guard can read your transcript. In this repo,
> editing a `.thy` file to add a `by <method>` closer is blocked unless a
> `sledgehammer`/`try0` was run just before and reported that method.
>
> Do exactly this and report the raw outcome of each step:
> 1. In a scratch theory (create `Scratch.thy` with a trivially true lemma, e.g.
>    `lemma "True" `), add the closer `by metis` **without** running any proof search.
>    Try to save the file. I expect a hook to BLOCK it — paste the exact block message.
> 2. Now run `sledgehammer` (or `try0`) on that goal through the Isabelle MCP and wait
>    for it to report a method. Tell me what it reported.
> 3. Immediately edit the theory to close the goal with the method it reported. Try to
>    save. Tell me whether the save was ALLOWED or BLOCKED.
>
> If step 3 was BLOCKED even though step 2 found the method, the hook could not parse
> your transcript. In that case: find your current session's transcript file (look for
> the most recently modified `*.jsonl` under `~/.codex/`), and paste ~6 lines around the
> sledgehammer call and its result so I can see the JSON shape. Do not summarise — paste
> the raw JSON lines.

## Prompt — paste into **OpenCode** (running in a hook-configured Isabelle repo)

> I want to verify that a PreToolUse guard can read your transcript. In this repo,
> editing a `.thy` file to add a `by <method>` closer is blocked unless a
> `sledgehammer`/`try0` was run just before and reported that method.
>
> Do exactly this and report the raw outcome of each step:
> 1. In a scratch theory (create `Scratch.thy` with a trivially true lemma, e.g.
>    `lemma "True" `), add the closer `by metis` **without** running any proof search.
>    Try to save the file. I expect a hook to BLOCK it — paste the exact block message.
> 2. Now run `sledgehammer` (or `try0`) on that goal through the Isabelle MCP and wait
>    for it to report a method. Tell me what it reported.
> 3. Immediately edit the theory to close the goal with the method it reported. Try to
>    save. Tell me whether the save was ALLOWED or BLOCKED.
>
> If step 3 was BLOCKED even though step 2 found the method, the hook could not parse
> your transcript. In that case: find your current session's transcript file (look for
> the most recently modified `*.jsonl` under `~/.local/share/opencode/` or
> `~/.local/state/opencode/`), and paste ~6 lines around the sledgehammer call and its
> result so I can see the JSON shape. Do not summarise — paste the raw JSON lines.
