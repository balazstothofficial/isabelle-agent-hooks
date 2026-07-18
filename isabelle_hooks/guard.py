"""Shared policy evaluation for the single Isabelle theory guard."""
import re
from dataclasses import dataclass

from .config import DEFAULTS
from .discovery import load_searchable_methods
from .methods import fragment_method_hits
from .relocations import candidate_goal_digest, relocation_allowances
from .transcript import recent_method_evidence_map


APPLY = re.compile(r"\bapply\b(?!')")
POLICY_APPLY = "apply-script"
POLICY_GUESSED = "guessed-proof"
POLICIES = (POLICY_APPLY, POLICY_GUESSED)


@dataclass(frozen=True)
class GuardConfig:
    policies: frozenset
    window: int
    allowed: frozenset
    found_via: tuple
    remediation: str | None
    isabelle_command: str
    searchable_override: frozenset | None
    semantic_fingerprint_command: str | None


@dataclass(frozen=True)
class Violation:
    message: str
    kind: str


def default_config():
    return GuardConfig(
        frozenset(POLICIES), DEFAULTS.window, frozenset(DEFAULTS.allowed), tuple(DEFAULTS.found_via),
        None, DEFAULTS.isabelle_command, None,
        DEFAULTS.semantic_fingerprint_command)


def parse_config(argv):
    defaults = default_config()
    window = defaults.window
    allowed = set(defaults.allowed)
    found_via = []
    remediation = None
    isabelle_command = defaults.isabelle_command
    searchable_override = None
    semantic_command = defaults.semantic_fingerprint_command
    policies = set(defaults.policies)
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--policies":
            selected = set()
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                if argv[i] in POLICIES:
                    selected.add(argv[i])
                i += 1
            if selected:
                policies = selected
        elif arg == "--window" and i + 1 < len(argv):
            try:
                value = int(argv[i + 1])
                if value > 0:
                    window = value
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
        elif arg == "--semantic-fingerprint-command" and i + 1 < len(argv):
            semantic_command = argv[i + 1]
            i += 2
        else:
            i += 1
    return GuardConfig(
        frozenset(policies), window, frozenset(allowed), tuple(found_via or defaults.found_via),
        remediation, isabelle_command,
        frozenset(searchable_override) if searchable_override is not None else None,
        semantic_command)


def _apply_violation(fragments):
    for fragment in fragments:
        if APPLY.search(fragment.text):
            return Violation(
                "\n[isabelle-theory-guard] BLOCKED write to an Isabelle theory.\n"
                "It introduces an `apply` script.\n\n"
                "Use structured Isar (`proof ... qed`, `by`, `using ... by`, or "
                "`unfolding ... by`).\n",
                "apply-script",
            )
    return None


def _candidate_id(fragment, hit):
    return id(fragment), hit.by_offset, hit.offset, hit.name


def _guessed_violation(fragments, transcript, cfg):
    candidates = []
    for fragment in fragments:
        scan_text, hits = fragment_method_hits(fragment)
        candidates.extend((fragment, scan_text, hit) for hit in hits)
    if not candidates:
        return None

    relocation_ids = relocation_allowances(
        candidates, cfg.semantic_fingerprint_command)
    if cfg.searchable_override is not None:
        searchable, registry_warning = set(cfg.searchable_override), None
    else:
        searchable, registry_warning = load_searchable_methods(cfg.isabelle_command)

    requiring = [
        candidate for candidate in candidates
        if _candidate_id(candidate[0], candidate[2]) not in relocation_ids
        and ((candidate[2].name in searchable) if searchable is not None
             else candidate[2].name not in cfg.allowed)
    ]
    if not requiring:
        return None

    evidence_by_method = recent_method_evidence_map(
        transcript, cfg.window, [item[2].name for item in requiring], cfg.found_via)
    used_evidence = set()
    blocked = None
    mismatch = None
    for fragment, scan_text, hit in requiring:
        goal = candidate_goal_digest(
            fragment, hit, cfg.semantic_fingerprint_command)
        records = evidence_by_method.get(hit.name.lower(), [])
        matching = [
            record for record in records
            if record.key not in used_evidence
            and (record.goal is None or (goal is not None and record.goal == goal))
        ]
        # Prefer goal-bound capabilities; natural-language transcript evidence remains
        # the fallback until every supported search integration emits the marker.
        matching.sort(key=lambda record: not record.explicit)
        if matching:
            used_evidence.add(matching[0].key)
            continue
        blocked = fragment, scan_text, hit
        explicit_goals = sorted({record.goal for record in records if record.goal})
        if explicit_goals and goal not in explicit_goals:
            mismatch = "Search evidence exists for a different goal."
        break
    if blocked is None:
        return None

    fragment, scan_text, hit = blocked
    source = fragment.context_source or fragment.source
    base_line = fragment.context_line or fragment.line
    relative_line = scan_text[:hit.by_offset].count("\n")
    source_lines = source.splitlines() or [source]
    source_line = source_lines[min(relative_line, len(source_lines) - 1)].strip()
    location = "%s:%d" % ((fragment.path or "<unknown theory>"),
                          base_line + relative_line)
    found_names = "/".join(cfg.found_via) or "a proof-search tool"
    remediation = cfg.remediation or DEFAULTS.remediation_template.format(
        found_names=found_names)
    registry_note = ((registry_warning + ". Conservative fallback requires evidence "
                      "for methods not explicitly allowed.\n")
                     if registry_warning else "")
    mismatch_note = (mismatch + "\n") if mismatch else ""
    return Violation(
        "\n[isabelle-theory-guard] BLOCKED write to an Isabelle theory.\n"
        "It introduces method `%s` without matching proof provenance.\n\n"
        "Location: %s\nSource: %s\n\n%s%s"
        "New closers must be found by %s. Exact proof-unit relocations within one "
        "atomic edit are allowed automatically.\n%s\n"
        % (hit.name, location, source_line, registry_note, mismatch_note,
           found_names, remediation),
        "guessed-proof",
    )


def evaluate(fragments, transcript, cfg):
    """Return the first policy violation after one shared edit extraction."""
    if POLICY_APPLY in cfg.policies:
        violation = _apply_violation(fragments)
        if violation is not None:
            return violation
    if POLICY_GUESSED in cfg.policies:
        return _guessed_violation(fragments, transcript, cfg)
    return None
