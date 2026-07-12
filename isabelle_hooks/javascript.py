"""Conservative normalization of literal tools calls inside Codex functions.exec."""
import ast
import re

from .protocol import (
    BASH_TOOL,
    EXEC_COMMAND_FIELD,
    is_exec_command_tool,
    is_mcp_write_tool,
    is_open_file_tool,
    is_patch_tool,
)


def _js_string_at(source, start):
    """Decode one quoted JavaScript string literal at ``start``.

    Codex's functions.exec generator emits ordinary single- or double-quoted
    literals for nested tool arguments.  Keep this deliberately small: template
    literals and computed values are not treated as inspectable writes.
    """
    if start >= len(source) or source[start] not in ("'", '"'):
        return None, start
    quote, i = source[start], start + 1
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == quote:
            token = source[start:i + 1]
            try:
                return ast.literal_eval(token), i + 1
            except Exception:
                return None, i + 1
        i += 1
    return None, len(source)


def _js_call_end(source, start):
    """Return the matching ``)`` for a call whose opening paren is at ``start``."""
    stack, quote, i = [")"], None, start + 1
    pairs = {"(": ")", "[": "]", "{": "}"}
    while i < len(source):
        c = source[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if source.startswith("//", i):
            j = source.find("\n", i + 2)
            i = len(source) if j < 0 else j + 1
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = len(source) if j < 0 else j + 2
            continue
        if c in ("'", '"', "`"):
            quote, i = c, i + 1
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or c != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return i
        i += 1
    return None


def _js_tool_calls(source):
    """Yield literal ``tools.NAME(ARG)`` calls from a functions.exec program."""
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("//", i):
            j = source.find("\n", i + 2)
            i = n if j < 0 else j + 1
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if source[i] in ("'", '"'):
            _, i = _js_string_at(source, i)
            continue
        if source[i] == "`":
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                elif source[i] == "`":
                    i += 1
                    break
                else:
                    i += 1
            continue
        if not source.startswith("tools.", i):
            i += 1
            continue
        name_start = i + len("tools.")
        m = re.match(r"[A-Za-z_$][\w$]*", source[name_start:])
        if not m:
            i = name_start
            continue
        name = m.group(0)
        j = name_start + len(name)
        while j < len(source) and source[j].isspace():
            j += 1
        if j >= len(source) or source[j] != "(":
            i = j
            continue
        end = _js_call_end(source, j)
        if end is None:
            return out
        out.append((name, source[j + 1:end]))
        i = end + 1
    return out


def _js_object_strings(argument):
    """Extract literal string fields from a generated JavaScript object argument."""
    fields = {}
    key_re = re.compile(r'''(?:^|[,{}])\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][\w$]*))\s*:''')
    for match in key_re.finditer(argument):
        key = next(g for g in match.groups() if g is not None)
        i = match.end()
        while i < len(argument) and argument[i].isspace():
            i += 1
        value, _ = _js_string_at(argument, i)
        if isinstance(value, str):
            fields[key] = value
    return fields


def orchestrated_tool_calls(source):
    """Return literal nested ``tools.NAME(ARG)`` calls in source order.

    Each item is ``(name, raw_argument, literal_string_fields)``. Consumers that
    need more than mutation extraction can therefore share this conservative parser.
    """
    if not isinstance(source, str):
        return []
    return [
        (tool, argument, _js_object_strings(argument))
        for tool, argument in _js_tool_calls(source)
    ]


def _orchestrated_events(source, transcript):
    """Normalize inspectable nested mutation calls from Codex functions.exec."""
    events = []
    for tool, argument, fields in orchestrated_tool_calls(source):
        if is_exec_command_tool(tool):
            if EXEC_COMMAND_FIELD in fields:
                events.append({
                    "tool_name": BASH_TOOL,
                    "tool_input": {"command": fields[EXEC_COMMAND_FIELD]},
                    "transcript_path": transcript,
                })
        elif is_patch_tool(tool):
            i = 0
            while i < len(argument) and argument[i].isspace():
                i += 1
            patch, _ = _js_string_at(argument, i)
            if isinstance(patch, str):
                events.append({
                    "tool_name": tool,
                    "tool_input": {"patch": patch},
                    "transcript_path": transcript,
                })
        elif is_mcp_write_tool(tool) or is_open_file_tool(tool):
            if fields:
                events.append({
                    "tool_name": tool,
                    "tool_input": fields,
                    "transcript_path": transcript,
                })
    return events
