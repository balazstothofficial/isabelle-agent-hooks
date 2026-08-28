# Isabelle agent hooks

Coding agents are bad at Isabelle in a specific way: they don't want to run
sledgehammer. Ask Claude Code or Codex to close a goal and it will type `by (metis
foo bar)` from pattern memory, watch it fail, mutate the lemma list, and try again,
when a single sledgehammer call would have settled it. The other habit is falling
back to unstructured `apply` scripts. After watching both loops enough times on my
own developments, I wrote this hook to veto those two kinds of edits before they
land. Once guessing stops working, running sledgehammer is the easiest path left.

It works as an agent hook: Claude Code, Codex, and OpenCode can run a command
before every file write, and that command can veto the write and print an
explanation the agent sees. This repository contains that command, specialized for
`.thy` files, plus a skill file that tells the agent how to respond when it gets
blocked.

I've been running it since July 2026 on Isabelle2025-2 and the Isabelle development
version, with the agent driving the prover through an MCP server (see
[Prover MCP servers](#prover-mcp-servers-iq-and-pide)). It is experimental and
best-effort; read [Limitations](#limitations) before relying on it.

## What this is not

This is not a verification tool, and "guard" is not a soundness claim. It constrains
the *edits an LLM agent makes*, nothing more:

- It never talks to the Isabelle kernel and never checks whether a proof is valid.
  Only Isabelle can do that.
- It inspects text. A proof that passes the hook can still fail; approval means
  the edit plausibly came from the right workflow, nothing stronger.
- It assumes a cooperative agent; it won't stop an adversarial one.

## What it looks like

The agent tries to write a guessed proof:

```isabelle
lemma rev_rev: "rev (rev xs) = xs"
  by metis
```

The write is rejected and the agent sees:

```text
[isabelle-theory-guard] BLOCKED write to an Isabelle theory.
It introduces method `metis` without a matching proof-search result.

Location: Scratch.thy:2
Source: by metis

New closers must be found by sledgehammer/try0. Exact proof-unit relocations within one atomic edit are allowed automatically.
Fix: run sledgehammer/try0 on this goal, then write the method it returns. If nothing is found, the step is too big -- break it into smaller `have`s.
```

The agent then actually runs sledgehammer, gets `by (metis rev_rev_ident)` back,
and that write goes through because the method now appears in a recent search
result in the session transcript.

## Installing for Claude Code

You need Python ≥ 3.10 (the hook itself is standard library only) and, for the
registry step below, an `isabelle` launcher on `PATH` with a built HOL session.
From your Isabelle project root:

```bash
git clone https://github.com/balazstothofficial/isabelle-agent-hooks /tmp/isabelle-agent-hooks
mkdir -p .claude/hooks .claude/skills
cp -R /tmp/isabelle-agent-hooks/isabelle_guards.py \
      /tmp/isabelle-agent-hooks/refresh_searchable_methods.py \
      /tmp/isabelle-agent-hooks/Hook_Searchable_Methods.thy \
      /tmp/isabelle-agent-hooks/isabelle_hooks \
      .claude/hooks/
cp -R /tmp/isabelle-agent-hooks/skills/isabelle-proof-hooks .claude/skills/
```

Register the hook in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash",
        "hooks": [
          {"type": "command", "command": "python3 .claude/hooks/isabelle_guards.py"}
        ]
      }
    ]
  }
}
```

Finally, build the registry of methods that sledgehammer and try0 can return for
your Isabelle installation (this launches Isabelle over HOL; the hook itself never
does, so this runs once, outside the write path):

```bash
python3 .claude/hooks/refresh_searchable_methods.py
```

The registry is keyed to the launcher: if you configure the hook with
`--isabelle-command` or `ISABELLE_HOOKS_ISABELLE`, pass that same command as this
script's argument. Rerun it after upgrading Isabelle. If the registry is missing
or stale, the hook doesn't stall your writes; it falls back to a stricter mode
that demands search evidence for every new proof method except ones you pass
with `--allow`. [docs/internals.md](docs/internals.md) covers how the cache
invalidates itself, including upgrades hidden behind a stable wrapper script.

Codex and OpenCode installs use the same bundle; see
[Other agents](#other-agents-codex-opencode) below.

## What gets blocked

Two independent policies, both on by default; `--policies` restricts enforcement
to one of them (e.g. `--policies apply-script`). Both see the same parsed edit;
the flag changes only which rules are evaluated.

**`apply-script`** blocks any edit that introduces an `apply` step, and tells the
agent to write structured Isar (`proof ... qed`, `by`, `using ... by`) instead.

**`guessed-proof`** blocks an edit that introduces a *search-discoverable* closer:
a terminal proof method that sledgehammer or try0 could have produced, like
`metis`, `smt`, or `meson`. Such a method is only accepted with one of two kinds of evidence that
it wasn't guessed:

1. **A real search result.** A recent sledgehammer/try0 call in the session
   transcript whose output names that method. Each result authorizes exactly one
   written proof, and completing any other state-changing operation first
   invalidates it, so the agent can't run one search and coast on it.
2. **A relocation.** The same lemma together with its proof, moved within a single
   atomic edit. Old and new proofs are matched one-for-one by file, surrounding
   context, statement, and method, so reordering lemmas is fine. Blocked as
   non-relocations:
   - copying a proof without removing the original;
   - moving just the method onto a different statement;
   - splitting the deletion and the insertion across separate tool calls.

   When static matching can't decide, the edit needs search evidence like any
   other.

Renames and other formatting-level refactors can additionally be cleared by an
external PIDE-based fingerprint provider; see
[docs/internals.md](docs/internals.md).

## Prover MCP servers (I/Q and PIDE)

The hook doesn't care how the agent talks to Isabelle: any transcript in which a
sledgehammer/try0 result appears works as evidence. But it has built-in support
for two MCP servers that give the agent an interactive PIDE session, and they are
how I actually run it.

**[AutoCorrode](https://github.com/awslabs/AutoCorrode)'s I/Q server** (I use the
development port in my
[AutoCorrode-Dev fork](https://github.com/balazstothofficial/AutoCorrode-Dev))
exposes an interactive Isar REPL plus file tools. The guard understands both
sides of it:

- Its file tools are guarded edits: `write_file` (including its `str_replace`
  form), `save_file`, and `open_file` when called with content to create a file
  (read-only `open_file` uses pass untouched).
- Its searches count as evidence: a `repl_sledgehammer` call, or a `repl_step` whose
  Isar command is `sledgehammer`/`try0`, counts as a search, and the method named
  in that call's own result authorizes writing it.
- Its REPL state changes (`repl_undo`, `repl_reset`, `repl_load`, and any
  `repl_step` that isn't itself a search) invalidate outstanding evidence, just
  like a file edit.

**[isabelle-pide-mcp](https://github.com/kappelmann/isabelle-pide-mcp)** is also
supported, though I've tested it less. It has no search tool at all; searching
means editing the theory, and the guard tracks that flow directly. An `edit` whose
*added* text contains `sledgehammer` or `try0` is allowed and arms an in-theory
search, and a later `get_state` result naming a method is that search's evidence
for writing the method. Repeated polls of the same find reuse the same evidence
rather than minting more. The `edit` tool is guarded when its `origin` is a
`*.thy` path.

To use either server, extend the hook matcher with its tool names, e.g. for
Claude Code:

```text
Write|Edit|MultiEdit|Bash|.*write_file|.*save_file|.*open_file|mcp__isabelle[-_]pide[-_]mcp__edit
```

A server can additionally bind its search results to specific goals with
`ISABELLE_HOOK_EVIDENCE` markers, and a PIDE session is the natural backend for
the semantic fingerprint provider; both protocols are specified in
[docs/internals.md](docs/internals.md).

## Tuning

`isabelle_guards.py` flags, appended to the hook command in your agent config,
e.g. `"command": "python3 .claude/hooks/isabelle_guards.py --isabelle-command
/opt/Isabelle2025-2/bin/isabelle"` (for OpenCode, use the hook entry's `args`
array in `guards.json`). Environment-variable equivalents in parentheses:

- `--policies apply-script guessed-proof` — which rules to enforce.
- `--window N` — how far back in the transcript to look for search evidence
  (default 30 entries).
- `--allow M1 M2 ...` — methods exempt from evidence in the no-registry fallback.
- `--found-via T1 T2 ...` — which tool results count as searches (default
  `sledgehammer try0`).
- `--remediation TEXT` — replace the "Fix: ..." line in block messages.
- `--isabelle-command CMD` (`ISABELLE_HOOKS_ISABELLE`) — the Isabelle launcher the
  registry is keyed to.
- `--semantic-fingerprint-command CMD`
  (`ISABELLE_HOOKS_SEMANTIC_FINGERPRINT_COMMAND`) — external refactor verifier.
- `--searchable M1 M2 ...` — override the registry, for diagnostics and tests.

## Limitations

Beyond the ground rules in [What this is not](#what-this-is-not):

An agent that wants around the hook gets around it: it could fabricate an
evidence marker or route the edit through an unrecognized tool. The bundled skill
instructs agents not to, and in practice they comply, but nothing enforces that.

It fails open. Malformed input, unrecognized tool calls, missing dependencies,
and internal errors all allow the write rather than blocking the agent. A
provider or registry failure likewise falls back to the conservative path and
never authorizes anything on its own.

It can also false-positive: evidence matching is heuristic, and a legitimate
refactor the matcher can't follow will be asked for search evidence it shouldn't
strictly need.

## Other agents (Codex, OpenCode)

**Codex:** install the same bundle under `.codex/hooks/` and add a `PreToolUse`
command hook to `.codex/hooks.json` (or inline in the adjacent `config.toml`),
with the matcher taken from [`guards.json`](guards.json)'s `defaultMatcher`:

```json
{"type": "command", "command": "python3 .codex/hooks/isabelle_guards.py"}
```

If Codex may start below the project root, resolve the root before invoking the
script; project hooks must be approved in Codex settings. Install the skill
directory as `.agents/skills/isabelle-proof-hooks/` — its `agents/openai.yaml`
carries the Codex-facing interface metadata.

**OpenCode:** install the Python bundle and [`guards.json`](guards.json) under
`.opencode/hooks/`, and copy `opencode-guard.ts` to `.opencode/plugins/`. The
plugin maintains a per-worktree transcript and invokes the guard once per matching
call; an optional `"interpreter"` key in `guards.json` overrides the `python3` it
runs the guard with. Install the skill as
`.opencode/skills/isabelle-proof-hooks/SKILL.md`.

### Matcher contract

`guards.json` also serves as a matcher reference for wiring up other agents: the
guard understands plain writes and edits, `apply_patch`, shell heredocs, literal
nested calls inside Codex `functions.exec`,
[AutoCorrode](https://github.com/awslabs/AutoCorrode) I/Q file writes, and
[isabelle-pide-mcp](https://github.com/kappelmann/isabelle-pide-mcp) edits (see
[Prover MCP servers](#prover-mcp-servers-iq-and-pide)). Only route tools whose
mutation payload it understands; anything else fails open.


## Internals

The implementer-facing details are in [docs/internals.md](docs/internals.md):
the hook's stdin/exit-code contract, the `ISABELLE_HOOK_EVIDENCE` marker protocol
for search integrations, the semantic fingerprint provider protocol, and how the
method registry is cached and invalidated. The code entry point for policy
evaluation is [`isabelle_hooks/guard.py`](isabelle_hooks/guard.py).
