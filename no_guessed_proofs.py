#!/usr/bin/env python3
"""PreToolUse guard: block guessed Isabelle terminal proof methods.

Enforces the isabelle.md proof-discipline rule "Closers are found, not guessed"
at the harness level, so it cannot be talked around. (The apply-script rule is a
separate hook, no_apply_scripts.py.)

Blocks a write when ADDED text introduces a method that the pinned Isabelle reports
as discoverable by try0/sledgehammer, unless that method was reported by a recent
search result. Searches are recognised as dedicated tool calls (repl_sledgehammer),
trigger words in proof-command fields (repl_step "try0", a Bash command), or -- for
PIDE-style MCPs without a search tool -- a theory edit that inserts the search
command, paired with a later get_state result carrying the find.
Package/structural methods absent from that registry may be written
directly. If discovery is unavailable, `--allow` supplies the conservative legacy
fallback policy.
A compound closer is fully covered: `by unfold_locales auto` is blocked on `auto`
when Isabelle reports `auto` as searchable, even though `unfold_locales` is not.

Config (all from argv, so policy stays with the caller):
  --window N        how many recent tool calls to scan for sledgehammer/try0
  --allow M1 M2 ..  fallback whitelist used only if Isabelle discovery fails
  --isabelle-command CMD  pinned Isabelle launcher used for discovery
  --searchable M1 ...     explicit registry override (tests/manual diagnostics)

Covers Write / Edit / MultiEdit / Bash, Codex's apply_patch and functions.exec,
the I/Q MCP write_file/save_file/open_file creation paths, and the
isabelle-pide-mcp `edit` tool.
Non-theory writes are never blocked.
Fails OPEN on internal errors so it can never brick the workflow.
"""
import subprocess as subprocess  # compatibility export for diagnostics/tests
import sys
from collections import namedtuple

try:
    from isabelle_hooks.config import DEFAULTS
    from isabelle_hooks.protocol import BLOCK_EXIT
    from isabelle_hooks.edits import extract_thy_edits
    from isabelle_hooks.discovery import (
        QUERY_MARKER as QUERY_MARKER,
        QUERY_VERSION as QUERY_VERSION,
        QUERY_THEORY as QUERY_THEORY,
        _cache_root as _cache_root,
        _discover_identity as _discover_identity,
        _run_discovery as _run_discovery,
        discover_searchable_methods as discover_searchable_methods,
    )
    from isabelle_hooks.methods import (
        MethodHit as MethodHit,
        by_method_hits as by_method_hits,
        by_method_names as by_method_names,
        fragment_method_hits,
    )
    from isabelle_hooks.transcript import (
        TranscriptEvent as TranscriptEvent,
        recent_method_evidence,
        method_found_recently as method_found_recently,
    )
except Exception as _e:
    sys.stderr.write(
        "[no-guessed-proofs hook] shared helper unavailable (%s); guard disabled.\n" % _e
    )
    sys.exit(0)


# Application defaults live here; hook configurations pass only intentional overrides.
DEFAULT_WINDOW = DEFAULTS.window
DEFAULT_FOUND_VIA = DEFAULTS.found_via
DEFAULT_ISABELLE_COMMAND = DEFAULTS.isabelle_command

# Parsed argv. Explicit command-line values override the application defaults above.
Config = namedtuple(
    "Config", "window allowed found_via remediation isabelle_command searchable_override")


def parse_config(argv):
    """Parse the guard's argv into a Config. Recognised:
      --window N            recent tool calls to scan for a found proof
      --allow M1 M2 ...     legacy fallback allowances if discovery fails
      --found-via T1 T2 ... proof-search tools whose result proves "found"
      --remediation TEXT    the block message's "Fix:" line (single value)
      --isabelle-command C  pinned Isabelle launcher
      --searchable M1 ...   explicit searchable-method override
    Tolerant: unknown args are ignored and bad values fall back to defaults
    (fail-open). Empty --found-via falls back to DEFAULT_FOUND_VIA so the escape
    hatch still works on a bare run."""
    window = DEFAULT_WINDOW
    allowed = set(DEFAULTS.allowed)
    found_via = []
    remediation = None
    isabelle_command = DEFAULT_ISABELLE_COMMAND
    searchable_override = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--window" and i + 1 < len(argv):
            try:
                parsed_window = int(argv[i + 1])
                if parsed_window > 0:
                    window = parsed_window
            except ValueError:
                pass
            i += 2
        elif arg == "--allow":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                allowed.add(argv[i])
                i += 1
        elif arg == "--found-via":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                found_via.append(argv[i])
                i += 1
        elif arg == "--remediation" and i + 1 < len(argv):
            remediation = argv[i + 1]
            i += 2
        elif arg == "--isabelle-command" and i + 1 < len(argv):
            isabelle_command = argv[i + 1]
            i += 2
        elif arg == "--searchable":
            searchable_override = set()
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                searchable_override.add(argv[i])
                i += 1
        else:
            i += 1
    return Config(window, allowed, found_via or list(DEFAULT_FOUND_VIA), remediation,
                  isabelle_command, searchable_override)


def main():
    cfg = parse_config(sys.argv[1:])

    fragments, transcript = extract_thy_edits()
    if fragments is None:
        sys.exit(0)

    candidates = []
    for fragment in fragments:
        scan_text, hits = fragment_method_hits(fragment)
        candidates.extend((fragment, scan_text, hit) for hit in hits)
    if not candidates:
        sys.exit(0)

    if cfg.searchable_override is not None:
        searchable, warning = cfg.searchable_override, None
    else:
        searchable, warning = discover_searchable_methods(cfg.isabelle_command)
    if warning:
        sys.stderr.write("[no-guessed-proofs hook] WARNING: " + warning + "\n")

    blocked = None
    used_evidence = set()
    for fragment, scan_text, hit in candidates:
        # Normal policy is inverted: only Isabelle-discoverable methods need proof.
        # If discovery failed completely, preserve the conservative legacy policy.
        requires_found = (hit.name in searchable) if searchable is not None else (
            hit.name not in cfg.allowed)
        if not requires_found:
            continue
        # One evidence key per written closer: several coexisting finds can authorize
        # several closers in one write, but a single find never authorizes two.
        evidence = next(
            (key for key in recent_method_evidence(
                transcript, cfg.window, hit.name, cfg.found_via)
             if key not in used_evidence),
            None)
        if evidence is None:
            blocked = fragment, scan_text, hit
            break
        used_evidence.add(evidence)
    if blocked is None:
        sys.exit(0)
    fragment, scan_text, hit = blocked

    found_names = "/".join(cfg.found_via) or "a proof-search tool"
    remediation = cfg.remediation or DEFAULTS.remediation_template.format(
        found_names=found_names)
    if searchable is None:
        policy_note = (
            "Discovery fallback: methods allowed without " + found_names + ": "
            + (", ".join(sorted(cfg.allowed)) if cfg.allowed else "(none)") + ".\n")
    else:
        policy_note = (
            "Methods registered by pinned Isabelle as search-discoverable require evidence; "
            "other individual methods are allowed directly.\n")
    diagnostic_source = (fragment.context_source if fragment.context_source is not None
                         else fragment.source)
    diagnostic_line = (fragment.context_line if fragment.context_line is not None
                       else fragment.line)
    relative_line = scan_text[:hit.by_offset].count("\n")
    source_lines = diagnostic_source.splitlines() or [diagnostic_source]
    source_line = source_lines[min(relative_line, len(source_lines) - 1)].strip()
    location = ((fragment.path or "<unknown theory>") + ":"
                + str(diagnostic_line + relative_line))
    msg = (
        "\n[no-guessed-proofs hook] BLOCKED write to an Isabelle theory.\n"
        "It introduces method `" + hit.name + "` in a `by` closer that was NOT found via "
        + found_names + ".\n\n"
        "Location: " + location + "\n"
        "Source: " + source_line + "\n\n"
        "PROOF DISCIPLINE (see the project instructions): the closer of each step must be FOUND, not "
        "guessed.\n" + policy_note + remediation + "\n"
    )
    sys.stderr.write(msg)
    sys.exit(BLOCK_EXIT)  # stderr is fed back to the model


if __name__ == "__main__":
    main()
