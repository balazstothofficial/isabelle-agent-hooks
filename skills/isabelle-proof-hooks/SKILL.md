---
name: isabelle-proof-hooks
description: Work correctly and efficiently with the Isabelle proof-discipline PreToolUse guard. Use when editing Isabelle theory files in a guarded project, responding to blocked theory writes, moving or refactoring existing proofs, using try0/sledgehammer evidence, or diagnosing a missing searchable-method registry.
---

# Isabelle Proof Hooks

Treat a blocked write as workflow feedback. Do not retry the same content through a
different write tool, disguise proof text, or weaken the hook configuration.

The installation may enable either policy or both:

- `apply-script`: write structured Isar; never add an `apply` chain.
- `guessed-proof`: search-discoverable closers need search or relocation provenance.

## Add a new closer

1. Inspect the exact current goal.
2. Run `try0`; if it finds nothing useful, run `sledgehammer` with a finite timeout.
3. Use a method only when it appears in that search call's own result.
4. Before writing the closer, avoid file edits, shell commands, ordinary proof steps,
   undo/reset calls, or other state-changing operations. Read-only inspection and
   further proof searches are safe.
5. Write exactly the returned closer promptly. One search result authorizes one closer;
   several new closers need separate finds.
6. Confirm the written command finishes in the real theory buffer. Search output is
   provenance, not final validation.

Goal-bound evidence may be emitted by the proof-search integration. Never fabricate or
manually copy its marker; the integration owns it. If a diagnostic says evidence belongs
to another goal, search again on the current goal.

## Refactor an existing proof

Move proof-bearing text in one atomic edit so the guard can compare the original and
final theory snapshots. Prefer one whole-buffer write, `MultiEdit`, `apply_patch`, or an
equivalent supported MCP mutation containing both deletion and insertion.

The guard consumes old proof units one-for-one. It permits a statement and closer to
move within the same file and context, but does not treat these as refactors:

- copying a proof unit without removing its old occurrence;
- moving only a method name to a different statement;
- splitting deletion and insertion across separate tool calls;
- moving across a changed locale/context that cannot be verified.

Formatting-only changes and renames can use the configured semantic fingerprint
provider. If no provider is configured or equivalence is uncertain, the safe fallback is
to obtain fresh proof-search evidence—not to work around the guard.

## Respond to diagnostics

- `apply script`: rewrite the proof as `proof ... qed`, `by`, `using ... by`, or smaller
  structured `have` steps.
- `without matching proof provenance`: search the current goal and write the returned
  method before another mutation.
- `different goal`: rerun search on this goal.
- `registry is not prepared/stale`: run the installed
  `refresh_searchable_methods.py` once outside the write path if setup changes are in
  scope; otherwise tell the user. Do not launch method discovery before every write.
- A genuinely found closer still blocked: report the exact search call/result and the
  intervening operations. Do not guess variants or retry blindly.
