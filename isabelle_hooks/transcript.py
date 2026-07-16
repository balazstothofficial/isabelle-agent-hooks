"""Transcript normalization and single-use proof-search evidence matching."""
import json
import os
import re
from collections import deque, namedtuple

from .javascript import orchestrated_tool_calls
from .protocol import (
    COMMAND_KEYS,
    CONTENT_KEYS,
    EXEC_SOURCE_KEYS,
    NEW_KEYS,
    OLD_KEYS,
    PATH_KEYS,
    PROOF_SEARCH_QUERY_KEYS,
    RESULT_EVENT_TYPES,
    THY_EXT,
    USE_EVENT_TYPES,
    is_edit_tool_name,
    is_evidence_invalidating_tool,
    is_exec_tool,
    is_open_file_tool,
    is_proof_search_wrapper_tool,
    is_proof_state_query_tool,
    tool_name_ends_with,
)
from .edits import _first_str, _split_edit
from .syntax import _strip


TranscriptEvent = namedtuple("TranscriptEvent", "kind call_id name text mutates thy_text",
                             defaults=(False, ""))

# Transcript content-block schemas. VERIFIED for Claude (tool_use / tool_result, under
# {"message":{"content":[...]}}). The Codex/OpenCode function-call variants below are a
# BEST-EFFORT guess: if they are wrong for a given agent, the escape hatch silently
# never fires there and a legitimately *found* method is over-blocked (fail-safe, but
# annoying). Update these constants and their payload tests when verified schemas
# change.
# Only these input fields count as "a proof-search tool was run" for trigger detection
# in a command -- NOT the whole serialized input. Otherwise an ordinary Edit whose
# *content* merely mentions "try0" (prose, a filename) would spuriously unlock a guess.
# `isar_text` is the field the iq-dev REPL's repl_step carries its Isar command in, so a
# `repl_step {"isar_text": "try0"}` run (try0/sledgehammer invoked as a step, not via the
# dedicated repl_sledgehammer tool) is seen. These are proof-command fields, never an edit
# body, so widening the set does not open the whole-content substring hole guarded above.
def _is_hammer(name, cmd, triggers, trigger_rxs):
    """True if a ('call', name, cmd) event is one of the `triggers` (proof-search
    tools). Tool NAMES are namespaced with separators (repl_sledgehammer,
    mcp__x__sledgehammer), so a trigger matches there as a substring. In command/method
    FIELDS a trigger must be a whole token (\\b..\\b) so an incidental substring -- e.g.
    a path try0_results.thy -- does not count as a run."""
    return any(tool_name_ends_with(name, trigger) for trigger in triggers) or any(
        rx.search(cmd) for rx in trigger_rxs)


def _is_edit_call(name, mutates=False):
    """Whether a transcript call can be the edit currently guarded by PreToolUse."""
    return mutates or is_edit_tool_name(name)


def _invalidates_search_evidence(name, cmd, is_hammer, mutates=False):
    """Whether a tool call can move the proof/file state searched by the hammer."""
    if is_hammer:
        return False
    return bool(cmd.strip()) or mutates or is_evidence_invalidating_tool(name)


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


def _exec_source(raw):
    """Extract a functions.exec program from transcript arguments."""
    if isinstance(raw, dict):
        for key in EXEC_SOURCE_KEYS:
            if isinstance(raw.get(key), str):
                return raw[key]
        return None
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except Exception:
        return raw
    return _exec_source(decoded) if isinstance(decoded, dict) else raw


def _call_text(name, raw):
    """Extract only fields that can genuinely invoke a proof command/search."""
    if not isinstance(raw, dict):
        return raw.lower() if isinstance(raw, str) else ""
    keys = COMMAND_KEYS
    if is_proof_search_wrapper_tool(name):
        keys += PROOF_SEARCH_QUERY_KEYS
    return " ".join(str(raw.get(k, "")) for k in keys).lower()


def _mutates_open_file(name, raw):
    """I/Q open_file is read-only unless it creates a file with initial content."""
    return bool(
        is_open_file_tool(name)
        and isinstance(raw, dict)
        and (raw.get("create_if_missing") is True
             or any(isinstance(raw.get(k), str) for k in CONTENT_KEYS))
    )


def _added_thy_text(name, raw):
    """The construct-stripped text a transcript edit call ADDS to a theory, or "".

    This is how an *in-theory* proof search becomes visible: a PIDE-backed MCP has
    no search tool, so the agent runs sledgehammer/try0 by writing the command into
    the theory (via its `edit` tool) -- the trigger word then appears only in the
    edit's added *.thy text, never in a command field. Only edit-tool payloads with
    an explicit *.thy path are considered; a str-replace's unchanged anchor lines
    are subtracted (removing a search command must not re-arm it) and the added
    text is stripped of comments/cartouches/strings so prose can never arm it."""
    if not isinstance(raw, dict) or not is_edit_tool_name(name):
        return ""
    path = _first_str(raw, PATH_KEYS)
    if path is None or not path.strip().lower().endswith(THY_EXT):
        return ""
    new = _first_str(raw, NEW_KEYS)
    if new is None:
        new = _first_str(raw, CONTENT_KEYS)
    if new is None:
        return ""
    old = _first_str(raw, OLD_KEYS) or ""
    return _strip(_split_edit(old, new)[1])


def _call_events(name, raw, call_id):
    """Normalize one transcript call, including nested functions.exec tools."""
    if is_exec_tool(name):
        source = _exec_source(raw)
        nested = orchestrated_tool_calls(source) if source else []
        if nested:
            return [
                TranscriptEvent(
                    "call",
                    call_id,
                    nested_name.lower(),
                    _call_text(nested_name, fields),
                    _mutates_open_file(nested_name, fields),
                    _added_thy_text(nested_name.lower(), fields),
                )
                for nested_name, _argument, fields in nested
            ]
    return [TranscriptEvent(
        "call", call_id, name, _call_text(name, raw), _mutates_open_file(name, raw),
        _added_thy_text(name, raw))]


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

    if obj.get("type") in USE_EVENT_TYPES + RESULT_EVENT_TYPES and emit(obj):
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
            if t in USE_EVENT_TYPES:
                name = (b.get("name") or "").lower()
                raw = b.get("input") or b.get("arguments") or {}
                call_id = b.get("id") or b.get("call_id") or b.get("tool_call_id")
                events.extend(_call_events(name, raw, call_id))
            elif t in RESULT_EVENT_TYPES:
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
    """Return the current evidence keys proving `method` was FOUND, oldest first.

    Within the recent window, one of the `triggers` (proof-search
    tools) ran AND the method being written appears in *that run's own result* -- i.e.
    this very method was actually *found*, not merely that some search ran (possibly on
    an unrelated goal) while the method's name happens to appear in some other,
    unrelated result. Each qualifying run yields one evidence key; the caller consumes
    one key per written closer, so a single find can never authorize two closers.
    Search runs are read-only, so evidence from several searches coexists (two finds
    may authorize two closers in one write) until a completed proof/file-state-changing
    call invalidates all of it.

    A search may also run *in the theory itself*: a PIDE-backed MCP has no search
    tool, so the agent inserts `sledgehammer`/`try0` into the *.thy via its edit tool
    and reads the find back from a proof-state query (`get_state`). Such an edit whose
    ADDED command text contains a trigger word arms an in-theory search; a later
    query result naming the method is then its evidence, keyed to the arming edit
    (repeated polls of the same find yield the same key, not fresh evidence). The
    arming edit changes the file, so like any edit it first invalidates all earlier
    evidence.

    The result must FOLLOW its trigger call and no later completed proof/file-state-
    changing call may intervene. Some harnesses append the edit currently undergoing
    PreToolUse before invoking this hook; when that in-flight edit is the final event
    and has no result yet, it is omitted so it cannot invalidate itself or consume a
    recency-window slot. Harnesses that invoke the hook before appending the edit need
    no special handling. A completed first theory write remains in the transcript and
    invalidates all prior evidence before a later write, so evidence from an earlier
    goal cannot authorize guesses indefinitely. Read-only calls may remain interleaved
    when call IDs let us pair the hammer with its own result.

    The window is the last `window` tool CALLS plus every event after the earliest of
    them. Missing/unparseable transcripts and non-matching results yield []."""
    if not path or not os.path.exists(path):
        return []
    events = _read_events(path, max(window * _TAIL_LINES_PER_CALL, _TAIL_MIN_LINES))
    if not events:
        return []
    # Claude Code versions differ on whether the guarded tool_use is persisted before
    # or after PreToolUse runs. In the former ordering, the current edit is the final
    # call and cannot yet have a result. Drop only that in-flight edit. Earlier edits
    # (including a completed edit immediately before it) and proof-state calls remain
    # evidence-invalidating. Do this before slicing the call window so a small window
    # still measures calls preceding the edit rather than counting the edit itself.
    if (events[-1].kind == "call"
            and _is_edit_call(events[-1].name, events[-1].mutates)):
        events = events[:-1]
    if not events:
        return []
    call_positions = [i for i, e in enumerate(events) if e.kind == "call"]
    if not call_positions:
        return []
    start = call_positions[-window] if len(call_positions) >= window else 0
    recent = events[start:]

    triggers = [t.lower() for t in triggers]
    trigger_rxs = [re.compile(r"\b" + re.escape(t) + r"\b") for t in triggers]
    mrx = re.compile(r"\b" + re.escape(method.lower()) + r"\b")
    pending_by_id = {}
    pending_adjacent = False
    in_theory = None   # (evidence key, epoch) of the latest armed in-theory search
    query_calls = set()  # call ids of proof-state queries (results may carry finds)
    found = {}         # insertion-ordered evidence keys, all still valid
    state_epoch = 0
    for event_index, e in enumerate(recent):
        if e.kind == "call":
            is_hammer = _is_hammer(e.name, e.text, triggers, trigger_rxs)
            arms_in_theory = bool(
                not is_hammer and e.thy_text
                and any(rx.search(e.thy_text.lower()) for rx in trigger_rxs))
            if (not is_hammer
                    and _invalidates_search_evidence(e.name, e.text, is_hammer, e.mutates)):
                state_epoch += 1
                found.clear()
            if arms_in_theory:
                key = (("id", str(e.call_id)) if e.call_id
                       else ("event", str(start + event_index)))
                in_theory = (key, state_epoch)
            if is_proof_state_query_tool(e.name) and e.call_id:
                query_calls.add(e.call_id)
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
                        found[("id", str(e.call_id))] = None
                elif (in_theory is not None and in_theory[1] == state_epoch
                        and e.call_id in query_calls and mrx.search(e.text)):
                    found[in_theory[0]] = None
            else:
                if pending_adjacent and mrx.search(e.text):
                    found[("event", str(start + event_index))] = None
                pending_adjacent = False
    return list(found)


def method_found_recently(path, window, method, triggers):
    """Compatibility boolean view used by tests and manual transcript diagnostics."""
    return bool(recent_method_evidence(path, window, method, triggers))
