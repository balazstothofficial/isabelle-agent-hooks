"""Proof-unit matching for verified refactoring moves.

The no-guessed-proof rule concerns provenance, not byte offsets.  A proof unit that
already existed may therefore move within one atomic edit without fresh search
evidence.  Matching is deliberately stricter than method-name matching: the enclosing
statement, closer, lexical context, and file must agree, and each old occurrence is
consumed at most once.

An optional external semantic provider can replace the conservative textual goal
fingerprint.  It is configured by ``ISABELLE_HOOKS_SEMANTIC_FINGERPRINT_COMMAND`` and
receives one JSON request on stdin; see ``semantic_fingerprints`` below.
"""
import hashlib
import json
import re
import shlex
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass

from .config import DEFAULTS
from .methods import by_method_hits
from .syntax import _strip


_STATEMENT = re.compile(
    r"(?m)^\s*(?:lemma|theorem|corollary|proposition|schematic_goal|have|show|thus|"
    r"hence|interpretation|sublocale|instance)\b")
_CONTEXT_OPEN = re.compile(
    r"(?m)^[ \t]*((?:context|locale|class|instantiation|notepad)\b(?:"
    r"[^\n]*\bbegin\b[^\n]*|"
    r"[^\n]*\n(?:[ \t]+[^\n]*\n)*[ \t]*begin\b[^\n]*))")
_CONTEXT_END = re.compile(r"(?m)^\s*end\b")
_THEORY = re.compile(r"(?ms)^\s*(theory\b.*?\bbegin\b)")


def _compact(text):
    return " ".join(text.split())


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _context_at(text, offset):
    """Conservative lexical context signature at ``offset``.

    This is not an Isabelle parser.  Uncertain constructs merely prevent a relocation
    exemption; they never authorize a new closer.  A configured semantic provider is
    the path for context-sensitive renames and other elaborated equivalences.
    """
    prefix = text[:offset]
    theory = _THEORY.search(prefix)
    stack = []
    events = [(m.start(), "open", _compact(m.group(1)))
              for m in _CONTEXT_OPEN.finditer(prefix)]
    events += [(m.start(), "end", "") for m in _CONTEXT_END.finditer(prefix)]
    for _position, kind, value in sorted(events):
        if kind == "open":
            stack.append(value)
        elif stack:
            stack.pop()
    return (_compact(theory.group(1)) if theory else "<partial>", tuple(stack))


@dataclass(frozen=True)
class ProofUnit:
    ordinal: int
    by_offset: int
    method_offset: int
    method: str
    statement: str
    closer: str
    context: tuple
    structural_goal: str
    content_key: str
    structural_key: str


def proof_units(source):
    """Extract conservative statement/closer units from one theory snapshot."""
    text = _strip(source)
    starts = [m.start() for m in _STATEMENT.finditer(text)]
    units = []
    for hit in by_method_hits(text):
        preceding = [start for start in starts if start <= hit.by_offset]
        if not preceding:
            continue
        start = preceding[-1]
        # Include the complete closer line.  Parenthesized method expressions normally
        # remain on this line; multiline expressions stay conservative and will not
        # match unless the external semantic provider identifies them.
        end = text.find("\n", hit.offset + len(hit.name))
        if end < 0:
            end = len(text)
        statement = _compact(text[start:hit.by_offset])
        closer = _compact(text[hit.by_offset:end])
        context = _context_at(text, start)
        goal = _digest(repr((context, statement)))
        content_key = _digest(repr((statement, closer)))
        key = _digest(repr((context, statement, closer)))
        units.append(ProofUnit(
            len(units), hit.by_offset, hit.offset, hit.name, statement, closer,
            context, goal, content_key, key))
    return units


def semantic_fingerprints(command, path, before, after, before_units, after_units):
    """Ask an optional PIDE/semantic bridge for elaborated goal fingerprints.

    The command reads JSON with ``path``, both source snapshots, and serializable unit
    descriptions.  It returns ``{"before": [...], "after": [...]}``, with one string
    or null per unit.  Fingerprints should identify the elaborated proposition and
    relevant Isabelle context; this module combines them with the unchanged closer.
    Any protocol or process failure falls back to structural matching.
    """
    if not command:
        return None, None
    request = {
        "version": 1,
        "path": path,
        "before": {"source": before, "units": [u.__dict__ for u in before_units]},
        "after": {"source": after, "units": [u.__dict__ for u in after_units]},
    }
    try:
        proc = subprocess.run(
            shlex.split(command), input=json.dumps(request), text=True,
            capture_output=True,
            timeout=DEFAULTS.semantic_fingerprint_timeout_seconds,
        )
        if proc.returncode != 0:
            return None, None
        response = json.loads(proc.stdout)
        old = response.get("before")
        new = response.get("after")
        if not isinstance(old, list) or not isinstance(new, list):
            return None, None
        if len(old) != len(before_units) or len(new) != len(after_units):
            return None, None
        if not all(value is None or isinstance(value, str) for value in old + new):
            return None, None
        return old, new
    except Exception:
        return None, None


def _unit_keys(units, semantic):
    if semantic is None:
        return [unit.structural_key for unit in units]
    return [
        _digest(repr((fingerprint, unit.closer))) if fingerprint else unit.structural_key
        for unit, fingerprint in zip(units, semantic)
    ]


def relocation_allowances(candidates, semantic_command=None):
    """Return candidate identities authorized by old proof units in the same edit."""
    grouped = defaultdict(list)
    for fragment, scan_text, hit in candidates:
        if fragment.path and fragment.before_source is not None and fragment.after_source is not None:
            grouped[fragment.path].append((fragment, scan_text, hit))

    allowed = set()
    for path, path_candidates in grouped.items():
        before = path_candidates[0][0].before_source
        after = path_candidates[0][0].after_source
        if before is None or after is None:
            continue
        old_units, new_units = proof_units(before), proof_units(after)
        old_sem, new_sem = semantic_fingerprints(
            semantic_command, path, before, after, old_units, new_units)
        old_keys = _unit_keys(old_units, old_sem)
        new_keys = _unit_keys(new_units, new_sem)
        old_counts, new_counts = Counter(old_keys), Counter(new_keys)

        # Candidate offsets normally refer to the final snapshot.  Associate them by
        # exact by/method offsets first and by structural unit as a conservative fallback
        # for ordered MultiEdit snapshots.
        units_by_offsets = {
            (unit.by_offset, unit.method_offset, unit.method): (unit, new_keys[index])
            for index, unit in enumerate(new_units)
        }
        units_by_content = defaultdict(list)
        for index, unit in enumerate(new_units):
            units_by_content[unit.content_key].append((unit, new_keys[index]))

        keyed_candidates = defaultdict(list)
        for fragment, scan_text, hit in path_candidates:
            match = (units_by_offsets.get((hit.by_offset, hit.offset, hit.name))
                     if fragment.context_source == after else None)
            if match is None:
                local = next((unit for unit in proof_units(
                    fragment.context_source or fragment.source)
                    if (unit.by_offset, unit.method_offset, unit.method)
                    == (hit.by_offset, hit.offset, hit.name)), None)
                choices = units_by_content.get(local.content_key, []) if local else []
                match = choices[0] if len(choices) == 1 else None
            if match is not None:
                keyed_candidates[match[1]].append((fragment, hit))

        for key, items in keyed_candidates.items():
            # No net increase: old proof units are consumed one-for-one.  An extra copy
            # makes new_counts > old_counts and receives no relocation allowance.
            if new_counts[key] <= old_counts[key]:
                for fragment, hit in items[:old_counts[key]]:
                    allowed.add((id(fragment), hit.by_offset, hit.offset, hit.name))
    return allowed


def candidate_goal_digest(fragment, hit, semantic_command=None):
    """Return the structural goal digest for one candidate, when recognizable."""
    local_source = fragment.context_source or fragment.source
    local = next((unit for unit in proof_units(local_source)
                  if (unit.by_offset, unit.method_offset, unit.method) == (
                      hit.by_offset, hit.offset, hit.name)), None)
    final_source = fragment.after_source or local_source
    final_units = proof_units(final_source)
    if final_source == local_source:
        choices = [unit for unit in final_units if unit == local]
    else:
        choices = [unit for unit in final_units
                   if local and unit.content_key == local.content_key]
    if len(choices) == 1:
        if semantic_command:
            _before_semantic, after_semantic = semantic_fingerprints(
                semantic_command, fragment.path, final_source, final_source,
                final_units, final_units)
            if after_semantic is not None:
                value = after_semantic[choices[0].ordinal]
                if value:
                    return value
        return choices[0].structural_goal
    return None
