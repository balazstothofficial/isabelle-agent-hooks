#!/usr/bin/env python3
"""PreToolUse guard: block guessed Isabelle terminal proof methods.

Enforces the isabelle.md proof-discipline rule "Closers are found, not guessed"
at the harness level, so it cannot be talked around. (The apply-script rule is a
separate hook, no_apply_scripts.py.)

Blocks a write when ADDED text introduces a method that the pinned Isabelle reports
as discoverable by try0/sledgehammer, unless that method was reported by a recent
search result. Package/structural methods absent from that registry may be written
directly. If discovery is unavailable, `--allow` supplies the conservative legacy
fallback policy.
A compound closer is fully covered: `by unfold_locales auto` is blocked on `auto`
when Isabelle reports `auto` as searchable, even though `unfold_locales` is not.

Config (all from argv, so policy stays with the caller):
  --window N        how many recent tool calls to scan for sledgehammer/try0
  --allow M1 M2 ..  fallback whitelist used only if Isabelle discovery fails
  --isabelle-command CMD  pinned Isabelle launcher used for discovery
  --searchable M1 ...     explicit registry override (tests/manual diagnostics)

Covers Write / Edit / MultiEdit / Bash, Codex's apply_patch, the iq-dev MCP
write_file/save_file, and the isabelle-pide-mcp `edit` tool. Non-theory writes are
never blocked.
Fails OPEN on internal errors so it can never brick the workflow.
"""
import sys, json, re, os, hashlib, subprocess, tempfile, time
from collections import namedtuple, deque
try:
    import fcntl
except ImportError:  # non-POSIX manual use: cache remains atomic, just not locked
    fcntl = None
try:
    from isabelle_hook_common import extract_thy_edits
except Exception as _e:
    # Fail OPEN, but say so: a missing/broken helper silently disables the guard,
    # which otherwise looks identical to "nothing to block". The note is non-blocking
    # (still exit 0) so it can never brick the workflow.
    sys.stderr.write(
        "[no-guessed-proofs hook] shared helper unavailable (%s); guard disabled.\n" % _e
    )
    sys.exit(0)

# Fallbacks for a bare, manual run. A managed installation should pass --window and
# --found-via explicitly, so deployment policy remains with its caller.
DEFAULT_WINDOW = 30
DEFAULT_FOUND_VIA = ("sledgehammer", "try0")
DEFAULT_ISABELLE_COMMAND = "@isabelleCommand@"
QUERY_MARKER = "NO_GUESSED_PROOFS_METHOD "
QUERY_VERSION = "1"
QUERY_THEORY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "Hook_Searchable_Methods.thy")

# Parsed argv. `found_via`: the proof-search tool names whose result proves a method
# was FOUND (not guessed). `remediation`: the block message's "Fix:" line. Both are
# supplied by Nix so the guard carries no hard-coded proof-tool policy or MCP verbs.
Config = namedtuple(
    "Config", "window allowed found_via remediation isabelle_command searchable_override")
MethodHit = namedtuple("MethodHit", "name offset by_offset")
TranscriptEvent = namedtuple("TranscriptEvent", "kind call_id name text")

# --- `by` closer parsing -------------------------------------------------------------
# A `by` closer runs `by method` or `by method method` (an initial method then a
# terminal one). We must extract EVERY method NAME it applies -- not just the first
# token -- or a guessed searchable method behind a structural one slips through:
# `by unfold_locales auto` runs `auto` after the structural `unfold_locales`, yet a
# first-token-only check would see only `unfold_locales`.
#
# The Isabelle/Isar method grammar makes a precise, false-positive-safe extraction
# possible. Outside parentheses a `by` takes at most two methods, each a BARE method
# name -- arguments (`simp add: ..`, `rule foo`) are legal ONLY inside `( .. )`. So:
#   * outside parens, every top-level token is a method name (method1, optional method2);
#   * inside parens, the method name is the first token after `(` or a combinator
#     (`,` `;` `|`); everything after it up to the next combinator is arguments.
# This is why `by (auto simp: foo)` yields only `auto` (not `foo`, an argument) and
# `by (metis (full_types) x)` yields only `metis` (`(full_types)` is an argument group).

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_INLINE_WS = " \t\r\f\v"

# Isar outer-syntax keywords that can legally follow a completed `by <method>` on the
# same line (proof chaining, flow, and statement starters). Outside parens the token
# after method1 is method2 UNLESS it is one of these -- exactly how Isabelle's own parser
# stops (a reserved word is not a method nameref). None of these is a proof method, so
# listing one can never suppress a real guess; the same-line bound in _by_method_names is
# a second guard, so a keyword accidentally omitted here still cannot spill into the next
# command across a newline.
_ISAR_KEYWORDS = frozenset("""
    then thus hence with from also finally moreover ultimately next qed done end
    show have obtain fix assume define let case note using unfolding apply proof
    lemma theorem corollary proposition schematic_goal definition abbreviation
    fun function primrec inductive coinductive datatype codatatype record typedef
    instance instantiation interpretation sublocale declare lemmas notation text
    section subsection subsubsection paragraph context locale class and where for
""".split())


def _skip_ws(text, i, newlines):
    """Advance past whitespace from i. With newlines=False, stop AT a newline (used to
    keep method2 detection on method1's own line)."""
    ws = (_INLINE_WS + "\n") if newlines else _INLINE_WS
    n = len(text)
    while i < n and text[i] in ws:
        i += 1
    return i


def _skip_balanced(text, i):
    """i is at '('. Return the index just past the matching ')' (end of text if
    unbalanced). Used to skip an argument group whole, mining no names from it."""
    depth, n = 0, len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _paren_method_names(text, i):
    """i is at the '(' of a parenthesised method expression. Return (names, end): the
    method names inside -- the first name after '(' and after each combinator `, ; |`,
    recursing through nested METHOD groups -- and the index past the matching ')'. A
    '(' reached in ARGUMENT position (after a method name, e.g. `metis (full_types)`) is
    a method argument, so its whole group is skipped, not mined."""
    names, depth, n = [], 0, len(text)
    expect = True  # True when the next identifier is a method name, not an argument
    while i < n:
        c = text[i]
        if c == "(":
            if expect:
                depth += 1        # nested method group: its first name is a method
                i += 1
            else:
                i = _skip_balanced(text, i)  # argument group: skip whole (...)
                expect = False
        elif c == ")":
            depth -= 1
            expect = False        # a completed method; next must be a combinator/close
            i += 1
            if depth == 0:
                break
        elif c in ",;|":
            expect = True
            i += 1
        else:
            m = _IDENT.match(text, i)
            if m:
                if expect:
                    names.append((m.group(0), m.start()))
                    expect = False
                i = m.end()
            else:
                i += 1            # whitespace, + * ? [n] ':' digits, ...
    return names, i


def _parse_method_unit(text, i):
    """Parse ONE method at i (the first non-space char). Return (names, end), or None if
    there is no method here. A '(' opens a parenthesised expression; a bare identifier is
    a method name with no arguments (the only form legal outside parens). A reserved Isar
    keyword is NOT a method -> None, so the optional-second-method scan stops at the next
    command rather than mining it."""
    n = len(text)
    if i >= n:
        return None
    if text[i] == "(":
        return _paren_method_names(text, i)
    m = _IDENT.match(text, i)
    if not m or m.group(0) in _ISAR_KEYWORDS:
        return None
    return [(m.group(0), m.start())], m.end()


def by_method_hits(text):
    """Every method NAME applied by a `by` closer in `text`, in order. method1 may sit on
    a following line; the optional method2 is taken only when it starts on the SAME line
    method1 ends and is not a reserved keyword, so the scan never spills into the next
    command. Inside parens the full combinator grammar is mined (see _paren_method_names).
    A spurious `\\bby\\b` match (e.g. inside `a.by`) yields nothing unless a real method
    token follows, so it cannot manufacture a hit."""
    hits, n = [], len(text)
    for mo in re.finditer(r"\bby\b", text):
        i = _skip_ws(text, mo.end(), newlines=True)   # method1 may be on the next line
        unit = _parse_method_unit(text, i)
        if unit is None:
            continue
        hits.extend(MethodHit(name, offset, mo.start()) for name, offset in unit[0])
        j = _skip_ws(text, unit[1], newlines=False)   # method2 must be on the same line
        if j < n and text[j] != "\n":
            unit2 = _parse_method_unit(text, j)
            if unit2 is not None:
                hits.extend(MethodHit(name, offset, mo.start()) for name, offset in unit2[0])
    return hits


def by_method_names(text):
    """Compatibility view used by parser-focused tests and manual diagnostics."""
    return [hit.name for hit in by_method_hits(text)]


def _hit_touches_changes(hit, changed_ranges):
    """Whether a parsed closer was introduced by one of the changed source spans."""
    if changed_ranges is None:
        return True
    syntax_spans = ((hit.by_offset, hit.by_offset + 2),
                    (hit.offset, hit.offset + len(hit.name)))
    return any(max(start, token_start) < min(end, token_end)
               for start, end in changed_ranges
               for token_start, token_end in syntax_spans)


def fragment_method_hits(fragment):
    """Parse complete post-edit syntax but return only newly introduced closers."""
    text = fragment.context_text if fragment.context_text is not None else fragment.text
    return text, [
        hit for hit in by_method_hits(text)
        if _hit_touches_changes(hit, fragment.changed_ranges)
    ]


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
    allowed = set()
    found_via = []
    remediation = None
    isabelle_command = DEFAULT_ISABELLE_COMMAND
    searchable_override = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--window" and i + 1 < len(argv):
            try:
                window = int(argv[i + 1])
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


def _cache_root():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "isabelle-agent-hooks", "searchable-methods")


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return "missing"


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        methods = obj.get("methods") if isinstance(obj, dict) else None
        if isinstance(methods, list) and methods and all(isinstance(m, str) for m in methods):
            return set(methods)
    except Exception:
        pass
    return None


def _discover_identity(command):
    proc = subprocess.run([command, "getenv", "-b", "ISABELLE_HOME"], text=True,
                          capture_output=True, timeout=15)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("could not resolve ISABELLE_HOME")
    home = os.path.realpath(proc.stdout.strip().splitlines()[-1])
    stable = hashlib.sha256((command + "\0" + home).encode()).hexdigest()[:20]
    sources = [
        "src/HOL/Tools/try0.ML",
        "src/HOL/Try0_HOL.thy",
        "src/HOL/Tools/Sledgehammer/sledgehammer_prover.ML",
        "src/HOL/Tools/Sledgehammer/sledgehammer_proof_methods.ML",
    ]
    material = [QUERY_VERSION, command, home, _file_hash(QUERY_THEORY)]
    material.extend(_file_hash(os.path.join(home, rel)) for rel in sources)
    fingerprint = hashlib.sha256("\0".join(material).encode()).hexdigest()[:24]
    return home, stable, fingerprint


def _run_discovery(command):
    proc = subprocess.run(
        [command, "process_theories", "-O", "-l", "HOL", "-D",
         os.path.dirname(QUERY_THEORY), "Hook_Searchable_Methods"],
        text=True, capture_output=True, timeout=90)
    output = proc.stdout + "\n" + proc.stderr
    methods = set()
    for line in output.splitlines():
        marker = line.find(QUERY_MARKER)
        if marker < 0:
            continue
        expression = line[marker + len(QUERY_MARKER):].strip()
        # Parenthesised reconstructors are printed as `(metis ...)` / `(smt ...)`.
        match = _IDENT.search(expression)
        if match:
            methods.add(match.group(0))
    if proc.returncode != 0 or not methods:
        detail = next((line.strip() for line in output.splitlines() if line.strip()),
                      "no method registry was produced")
        raise RuntimeError(detail)
    return methods


def discover_searchable_methods(command):
    """Return (methods, warning). A stale valid cache is preferred to unsafe emptiness."""
    try:
        _home, stable, fingerprint = _discover_identity(command)
    except Exception as exc:
        return None, "method discovery failed: %s" % exc

    root = _cache_root()
    current = os.path.join(root, "%s-%s.json" % (stable, fingerprint))
    cached = _load_cache(current)
    if cached:
        return cached, None

    lock_file = None
    try:
        os.makedirs(root, exist_ok=True)
        lock_file = open(os.path.join(root, stable + ".lock"), "a+")
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        cached = _load_cache(current)
        if cached:
            return cached, None
        methods = _run_discovery(command)
        fd, tmp = tempfile.mkstemp(prefix="methods-", suffix=".json", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": QUERY_VERSION, "created": time.time(),
                           "methods": sorted(methods)}, f)
                f.write("\n")
            os.replace(tmp, current)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return methods, None
    except Exception as exc:
        stale = []
        try:
            stale = sorted(
                (p for p in os.listdir(root) if p.startswith(stable + "-") and p.endswith(".json")),
                key=lambda p: os.path.getmtime(os.path.join(root, p)), reverse=True)
        except Exception:
            pass
        for name in stale:
            cached = _load_cache(os.path.join(root, name))
            if cached:
                return cached, "method discovery failed; using previous cache: %s" % exc
        return None, "method discovery failed and no cache is available: %s" % exc
    finally:
        if lock_file is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            lock_file.close()


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
        evidence = recent_method_evidence(transcript, cfg.window, hit.name, cfg.found_via)
        if evidence is None or evidence in used_evidence:
            blocked = fragment, scan_text, hit
            break
        used_evidence.add(evidence)
    if blocked is None:
        sys.exit(0)
    fragment, scan_text, hit = blocked

    found_names = "/".join(cfg.found_via) or "a proof-search tool"
    remediation = cfg.remediation or (
        "Fix: run " + found_names + " on this goal, then write the method it returns. "
        "If nothing is found, the step is too big -- break it into smaller `have`s."
    )
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
    sys.exit(2)  # exit 2 => PreToolUse block, stderr fed back to the model


# Transcript content-block schemas. VERIFIED for Claude (tool_use / tool_result, under
# {"message":{"content":[...]}}). The Codex/OpenCode function-call variants below are a
# BEST-EFFORT guess: if they are wrong for a given agent, the escape hatch silently
# never fires there and a legitimately *found* method is over-blocked (fail-safe, but
# annoying). Update these constants and their payload tests when verified schemas
# change.
_USE_TYPES = ("tool_use", "function_call", "tool_call")
_RESULT_TYPES = ("tool_result", "function_call_output", "tool_output", "function_output")

# Only these input fields count as "a proof-search tool was run" for trigger detection
# in a command -- NOT the whole serialized input. Otherwise an ordinary Edit whose
# *content* merely mentions "try0" (prose, a filename) would spuriously unlock a guess.
# `isar_text` is the field the iq-dev REPL's repl_step carries its Isar command in, so a
# `repl_step {"isar_text": "try0"}` run (try0/sledgehammer invoked as a step, not via the
# dedicated repl_sledgehammer tool) is seen. These are proof-command fields, never an edit
# body, so widening the set does not open the whole-content substring hole guarded above.
_CMD_KEYS = ("command", "method", "tactic", "tac", "proof", "step", "isar_text")


def _is_hammer(name, cmd, triggers, trigger_rxs):
    """True if a ('call', name, cmd) event is one of the `triggers` (proof-search
    tools). Tool NAMES are namespaced with separators (repl_sledgehammer,
    mcp__x__sledgehammer), so a trigger matches there as a substring. In command/method
    FIELDS a trigger must be a whole token (\\b..\\b) so an incidental substring -- e.g.
    a path try0_results.thy -- does not count as a run."""
    return any(t in name for t in triggers) or any(rx.search(cmd) for rx in trigger_rxs)


_EDIT_CALL_NAMES = (
    "write", "edit", "apply_patch", "multiedit", "save_file",
)

_EVIDENCE_INVALIDATING_NAMES = _EDIT_CALL_NAMES + (
    "repl_step", "repl_undo", "repl_reset", "repl_load",
)


def _is_edit_call(name):
    """Whether a transcript call can be the edit currently guarded by PreToolUse."""
    return name == "bash" or any(part in name for part in _EDIT_CALL_NAMES)


def _invalidates_search_evidence(name, cmd, is_hammer):
    """Whether a tool call can move the proof/file state searched by the hammer."""
    if is_hammer:
        return False
    return bool(cmd.strip()) or any(part in name for part in _EVIDENCE_INVALIDATING_NAMES)


def _result_text(v):
    """Flatten a tool_result payload (str, or a list/dict of text blocks) to lowercase."""
    if isinstance(v, str):
        return v.lower()
    if isinstance(v, list):
        parts = []
        for it in v:
            if isinstance(it, str):
                parts.append(it)
            elif isinstance(it, dict):
                t = it.get("text") or it.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts).lower()
    if isinstance(v, dict):
        t = v.get("text") or v.get("content") or ""
        return t.lower() if isinstance(t, str) else ""
    return ""


def _iter_blocks(obj):
    """Yield content-block dicts from the transcript shapes we support: Claude's
    {"message":{"content":[...]}}, a bare {"content":[...]}, and a line that is itself
    an event ({"type":"function_call",...}). De-duplicated by identity per line."""
    if not isinstance(obj, dict):
        return
    seen = set()

    def emit(it):
        if isinstance(it, dict) and id(it) not in seen:
            seen.add(id(it))
            return True
        return False

    if obj.get("type") in _USE_TYPES + _RESULT_TYPES and emit(obj):
        yield obj
    for container in (obj.get("message"), obj):
        if isinstance(container, dict) and isinstance(container.get("content"), list):
            for it in container["content"]:
                if emit(it):
                    yield it


def _read_events(path, max_lines):
    """Parse the transcript tail into ID-bearing call/result events. Tolerant;
    unrecognisable lines are skipped. Bounded: only the last `max_lines` lines are
    JSON-parsed, so this stays cheap even on a long session (the guard runs on every
    theory write). Reads UTF-8 explicitly. Returns [] on any I/O trouble (fail safe)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=max_lines)
    except Exception:
        return []
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        for b in _iter_blocks(obj):
            t = b.get("type")
            if t in _USE_TYPES:
                name = (b.get("name") or "").lower()
                raw = b.get("input") or b.get("arguments") or {}
                if isinstance(raw, dict):
                    cmd = " ".join(str(raw.get(k, "")) for k in _CMD_KEYS).lower()
                elif isinstance(raw, str):
                    cmd = raw.lower()
                else:
                    cmd = ""
                call_id = b.get("id") or b.get("call_id") or b.get("tool_call_id")
                events.append(TranscriptEvent("call", call_id, name, cmd))
            elif t in _RESULT_TYPES:
                body = b.get("content", b.get("output", b.get("text", "")))
                call_id = b.get("tool_use_id") or b.get("call_id") or b.get("tool_call_id")
                events.append(TranscriptEvent("result", call_id, "", _result_text(body)))
    return events


# Transcript tail bound: read enough lines to hold `window` tool calls with their
# results (a call+result spans a handful of JSONL lines), with a floor so a small
# window still sees recent context. Keeps work bounded on a long session.
_TAIL_LINES_PER_CALL = 8
_TAIL_MIN_LINES = 100


def recent_method_evidence(path, window, method, triggers):
    """Return unique current evidence that `method` was found, or None.

    Within the recent window, one of the `triggers` (proof-search
    tools) ran AND the method being written appears in *that run's own result* -- i.e.
    this very method was actually *found*, not merely that some search ran (possibly on
    an unrelated goal) while the method's name happens to appear in some other,
    unrelated result.

    The result must FOLLOW its trigger call and no later completed proof/file-state-
    changing call may intervene. Some harnesses append the edit currently undergoing
    PreToolUse before invoking this hook; when that in-flight edit is the final event
    and has no result yet, it is omitted so it cannot invalidate itself or consume a
    recency-window slot. Harnesses that invoke the hook before appending the edit need
    no special handling. A completed first theory write remains in the transcript and
    consumes the escape hatch before a later write, so evidence from an earlier goal
    cannot authorize guesses indefinitely. Read-only calls may remain interleaved when
    call IDs let us pair the hammer with its own result.

    The window is the last `window` tool CALLS plus every event after the earliest of
    them. Missing/unparseable transcripts and non-matching results yield None."""
    if not path or not os.path.exists(path):
        return None
    events = _read_events(path, max(window * _TAIL_LINES_PER_CALL, _TAIL_MIN_LINES))
    if not events:
        return None
    # Claude Code versions differ on whether the guarded tool_use is persisted before
    # or after PreToolUse runs. In the former ordering, the current edit is the final
    # call and cannot yet have a result. Drop only that in-flight edit. Earlier edits
    # (including a completed edit immediately before it) and proof-state calls remain
    # evidence-invalidating. Do this before slicing the call window so a small window
    # still measures calls preceding the edit rather than counting the edit itself.
    if events[-1].kind == "call" and _is_edit_call(events[-1].name):
        events = events[:-1]
    if not events:
        return None
    call_positions = [i for i, e in enumerate(events) if e.kind == "call"]
    if not call_positions:
        return None
    start = call_positions[-window] if len(call_positions) >= window else 0
    recent = events[start:]

    triggers = [t.lower() for t in triggers]
    trigger_rxs = [re.compile(r"\b" + re.escape(t) + r"\b") for t in triggers]
    mrx = re.compile(r"\b" + re.escape(method.lower()) + r"\b")
    pending_by_id = {}
    pending_adjacent = False
    found = None
    state_epoch = 0
    for event_index, e in enumerate(recent):
        if e.kind == "call":
            is_hammer = _is_hammer(e.name, e.text, triggers, trigger_rxs)
            if is_hammer:
                found = None  # a newer search supersedes an older result
            elif _invalidates_search_evidence(e.name, e.text, is_hammer):
                state_epoch += 1
                found = None
            if e.call_id:
                if is_hammer:
                    pending_by_id[e.call_id] = state_epoch
            else:
                # Schemas without ids retain the old adjacency semantics.
                pending_adjacent = is_hammer
        elif e.kind == "result":
            if e.call_id:
                if e.call_id in pending_by_id:
                    search_epoch = pending_by_id.pop(e.call_id)
                    if search_epoch == state_epoch and mrx.search(e.text):
                        found = ("id", str(e.call_id))
            else:
                if pending_adjacent and mrx.search(e.text):
                    found = ("event", str(start + event_index))
                pending_adjacent = False
    return found


def method_found_recently(path, window, method, triggers):
    """Compatibility boolean view used by tests and manual transcript diagnostics."""
    return recent_method_evidence(path, window, method, triggers) is not None


if __name__ == "__main__":
    main()
