"""Transcript normalization and single-use proof-search evidence matching."""
import json
import os
import re
from collections import deque, namedtuple

from .javascript import orchestrated_tool_calls
from .protocol import (
    COMMAND_KEYS,
    EXEC_SOURCE_KEYS,
    RESULT_EVENT_TYPES,
    USE_EVENT_TYPES,
    is_edit_tool_name,
    is_evidence_invalidating_tool,
    is_exec_tool,
    tool_name_ends_with,
)


TranscriptEvent = namedtuple("TranscriptEvent", "kind call_id name text")

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


def _is_edit_call(name):
    """Whether a transcript call can be the edit currently guarded by PreToolUse."""
    return is_edit_tool_name(name)


def _invalidates_search_evidence(name, cmd, is_hammer):
    """Whether a tool call can move the proof/file state searched by the hammer."""
    if is_hammer:
        return False
    return bool(cmd.strip()) or is_evidence_invalidating_tool(name)


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
                    " ".join(str(fields.get(k, "")) for k in COMMAND_KEYS).lower(),
                )
                for nested_name, _argument, fields in nested
            ]
    if isinstance(raw, dict):
        cmd = " ".join(str(raw.get(k, "")) for k in COMMAND_KEYS).lower()
    elif isinstance(raw, str):
        cmd = raw.lower()
    else:
        cmd = ""
    return [TranscriptEvent("call", call_id, name, cmd)]


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
