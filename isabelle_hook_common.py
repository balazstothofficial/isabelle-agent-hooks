"""Shared parsing for the Isabelle PreToolUse hooks.

extract_thy_edits() reads the hook's stdin JSON and returns
    (fragments, transcript_path)
where each fragment retains its target path, source line and ADDED Isabelle text
with comments, cartouches and string literals stripped (so command keywords like
`by`/`apply` cannot be matched
inside prose). For a str-replace edit (an old/new pair) only the lines new_str
actually changes are ADDED -- the unchanged anchor lines it reproduces from old_str
are excluded, so a pre-existing closer sitting in the anchor is not re-flagged as a
fresh guess (see _split_edit). When that changed block lands inside a `text ‹..›`,
comment or string whose delimiter is OUTSIDE the edited fragment, the strip is seeded
with the state read from the file on disk at the edit site, so in-prose words like
"checked by a countermodel" are not mis-read as a `by` closer (see _edit_site_state).
Returns None when the call does not target an Isabelle theory file
(only *.thy is recognised, case-insensitively -- Foo.THY is Foo.thy on a
case-insensitive filesystem, so the gate must not be fooled by the case), has no
added text, or is unparseable. Fails OPEN on its
own errors -- a parse failure or unrecognised payload shape yields None so the caller
never blocks on a bug of its own. One deliberate exception: a non-Bash edit that
carries added content but no recognisable path key is still checked (the *.thy path
gate is skipped when no path is present), so that case errs toward over-blocking
rather than letting an edit slip through unchecked.

Covers Write / Edit / MultiEdit / Bash, Codex's apply_patch (a unified-diff style
envelope -- including an apply_patch heredoc inside a Bash command), the iq-dev MCP
write_file/save_file, and the isabelle-pide-mcp `edit` tool. Codex functions.exec
programs are unwrapped into those same direct events when their nested tool arguments
are literal. The PIDE edit is checked when its `origin` is a *.thy path (a
session-qualified theory name such as HOL.Nat is not a *.thy path, so it is not
recognised).
"""
import sys, json, re, difflib, ast
from collections import namedtuple


# A separately-stripped added block. `line` is the 1-based source line at which the
# block begins when it can be derived. For edits against readable files, context_text
# is the stripped post-edit theory and changed_ranges identifies the spans introduced
# by this edit within it. This lets a consumer parse syntax that crosses an edit
# boundary without treating pre-existing proof text as newly added.
EditFragment = namedtuple(
    "EditFragment",
    "path text source line context_text context_source context_line changed_ranges",
    defaults=(None, None, None, None),
)

# The Isabelle theory-file extension -- single-sourced (it recurs in the write-target
# regex and the path/section gates). Lowercase; the checks compare case-insensitively.
THY_EXT = ".thy"

PATH_KEYS = ("file_path", "path", "filename", "file", "target", "origin")
CONTENT_KEYS = ("content", "text", "new_string", "new_str", "data", "code", "source")
# The replacement half of a replace-style edit, and its paired "old" half. When both are
# present the edit is a REPLACE, so only the lines the new text changes are actually
# added -- the unchanged anchor lines it reproduces from the old text must be excluded
# (see _split_edit), and the strip can be seeded at the edit site (see _edit_site_state).
# Covers Edit / MultiEdit (old_string/new_string), the iq-dev write_file str_replace
# (old_str/new_str), and the isabelle-pide-mcp `edit` tool (old_text/text). `text` is
# last in NEW_KEYS so a more specific new_string/new_str wins; it only ever pairs here
# when an OLD key is also present (a bare `text` -- Write, a PIDE insert -- is scanned
# whole via CONTENT_KEYS, untouched by this pairing).
NEW_KEYS = ("new_string", "new_str", "text")
OLD_KEYS = ("old_string", "old_str", "old_text")

# Tool-name classes used to pick the payload-extraction strategy below. The SET of
# tools that reaches these guards is owned by the caller's matcher -- keep it in sync
# with this parser contract (documented in README.md): a tool added to the matcher must
# be handled here via PATH_KEYS + CONTENT_KEYS or one of these name-based branches.
# Otherwise it falls into the non-Bash path gate, which over-blocks a content-bearing
# edit that exposes no known path key.
BASH_TOOL = "Bash"
PATCH_TOOL_SUBSTR = "apply_patch"      # Codex's edit tool
MCP_WRITE_SUBSTRS = ("write_file", "save_file")  # iq-dev MCP write verbs
EXEC_TOOL_NAMES = ("functions.exec", "functions_exec")


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
        if tool == "exec_command":
            if "cmd" in fields:
                events.append({
                    "tool_name": BASH_TOOL,
                    "tool_input": {"command": fields["cmd"]},
                    "transcript_path": transcript,
                })
        elif PATCH_TOOL_SUBSTR in tool:
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
        elif any(s in tool for s in MCP_WRITE_SUBSTRS) or "open_file" in tool:
            if fields:
                events.append({
                    "tool_name": tool,
                    "tool_input": fields,
                    "transcript_path": transcript,
                })
    return events

# apply_patch envelope markers. Codex (and any agent that edits via apply_patch)
# sends a patch like:
#     *** Begin Patch
#     *** Update File: Foo.thy
#     @@
#     -old line
#     +new line
#     *** End Patch
# We key on the literal "*** Begin Patch" marker so detection is independent of the
# param name carrying the envelope, and parse per-file so only added (+) lines in
# *.thy sections are checked.
PATCH_MARKER = "*** Begin Patch"
_PATCH_FILE_HDR = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")
_PATCH_MOVE_HDR = re.compile(r"^\*\*\* Move to: (.+?)\s*$")

# A *.thy that is actually WRITTEN by a Bash command -- a redirect target (`> F.thy`,
# `>> F.thy`, `1> F.thy`; this also covers `cat > F.thy` and a `cat > F.thy <<EOF`
# heredoc), `tee`, or a copy/move/in-place edit whose *destination* is a *.thy. A bare
# mention (a grep pattern, a comment, a path passed to a read-only command) is NOT a
# theory edit and must not arm the guard. Case-insensitive so `Foo.THY` still matches.
#
# Known, accepted gaps (Bash is a SECONDARY edit path -- real theory editing goes
# through the Write/Edit tools, the MCP write_file/save_file, and apply_patch, which
# are gated precisely by path/section): a fully dynamic redirect target (`> "$f"`
# where the name is only in a variable) and non-redirect writers (`dd of=`,
# `python -c "open(...)"`) are not recognised. Interpolated paths that still contain
# the literal `.thy` (`> "$dir/Foo.thy"`) ARE recognised -- the char class allows the
# usual shell path/expansion characters.
_THY = r"['\"]?[\w./+$~:@{}-]*" + re.escape(THY_EXT) + r"\b"  # a *.thy filename token
# The destination of cp/mv/install/sed -i is the LAST operand, so require the *.thy to
# sit at a command boundary -- otherwise a `cp Foo.thy backup/` (theory as SOURCE)
# would arm the guard on an unrelated "by" elsewhere in the line.
_END = r"(?=\s*(?:$|[<>|;&\n]))"
_THY_WRITE = re.compile(
    r"""(?:
          (?:\d?>>?)\s*""" + _THY + r"""                    # > >> 1>  (target is NEXT token)
        | \btee\b(?:\s+-a\b)?\s+""" + _THY + r"""           # tee / tee -a  FILE.thy
        | \b(?:cp|mv|install)\b[^|;&\n]*?\s""" + _THY + _END + r"""  # cp/mv/install ... DEST.thy
        | \bsed\b[^|;&\n]*?-i[^|;&\n]*?\s""" + _THY + _END + r"""    # sed -i ... FILE.thy
      )""",
    re.IGNORECASE | re.VERBOSE,
)


# Cartouche delimiters in both notations: the ASCII `\<open>..\<close>` and the
# Unicode `‹..›`. Either close ends either open -- they are the same construct.
_CART_OPEN = (r"\<open>", "‹")
_CART_CLOSE = (r"\<close>", "›")


def _canon_symbols(s):
    """Fold the two interchangeable Isabelle cartouche notations to one form, so an
    on-disk lookup succeeds regardless of which the file and the edit each used. Isabelle
    treats `\\<open>`/`\\<close>` and `‹`/`›` as the same symbols, and the MCP's str_replace
    matches its old_str against the file modulo that equivalence -- so the hook's own
    location of the edit (see _edit_site_state) must fold them too, or a file written with
    `\\<open>` is never found from an edit phrased with `‹` (the seed is lost and in-prose
    text is misread as a `by` closer). Only the cartouche pair matters for strip state;
    other `\\<sym>` names never open a span, so leaving them untouched is fine."""
    return s.replace(_CART_OPEN[0], _CART_OPEN[1]).replace(_CART_CLOSE[0], _CART_CLOSE[1])


def _canon_lookup(s):
    """Fold text the way the edit tools match their old text against the file, so the
    edit-site lookup (see _edit_site_state) reproduces their comparison: cartouche
    notation folded (see _canon_symbols) AND trailing whitespace per line ignored -- the
    PIDE `edit` tool matches old_text "modulo trailing whitespace", and str_replace edits
    are routinely re-indented on save. A file written with `\\<open>` and trailing spaces
    is thus still located from an edit phrased with `‹` and none. Notation and trailing
    whitespace are both neutral to strip state, so folding them leaves the seed intact."""
    return "\n".join(line.rstrip() for line in _canon_symbols(s).split("\n"))


def _starts_any(text, i, toks):
    """Return the first token in `toks` that `text` starts with at `i`, else None."""
    for t in toks:
        if text.startswith(t, i):
            return t
    return None


def _strip_scan(blob, state=None):
    """Blank out everything that is not Isabelle *command* text, so proof keywords
    like `by`/`apply` can only match in real code -- never inside prose, comments or
    literals. Removed: ML comments `(* .. *)` (nesting), cartouches `\\<open>..\\<close>`
    / `‹..›` (nesting, either notation), and `"string literals"` (escape-aware).

    A SINGLE left-to-right scan, not three sequential passes -- so a marker that
    belongs to one construct can never be mis-read as opening another. This closes a
    bypass the pass-based version had: a `\\<open>` (or `‹`) sitting inside a string
    literal is verbatim text there, so it must not open a cartouche span that would
    swallow a following real `by <method>`. Likewise a `"` inside a comment/cartouche
    is text, not a string start.

    RESUMABLE. `state` is the construct open at the START of `blob` (None at top level,
    or ('comment', depth) / ('cart', [closers]) / ('string',)); the function returns
    (out, end_state) so a caller can seed the scan with the state at an edit site --
    an edit whose enclosing `text ‹..›` / `(* .. *)` opener lives OUTSIDE the edited
    fragment is then still recognised as prose (see _edit_site_state). When resuming
    inside a construct no leading space is emitted (the opener was already consumed
    upstream); a construct newly opened at top level becomes one space so neighbouring
    tokens stay separated. Unbalanced input degrades safely: an unclosed construct
    consumes to end (a syntax error anyway) and is reported in end_state; a stray closer
    at top level is emitted verbatim."""
    out, i, n = [], 0, len(blob)
    mode = state[0] if state else None
    depth = state[1] if state and state[0] == "comment" else 0
    stack = list(state[1]) if state and state[0] == "cart" else []
    while i < n:
        if mode == "comment":
            if blob.startswith("(*", i):
                depth, i = depth + 1, i + 2
            elif blob.startswith("*)", i):
                depth, i = depth - 1, i + 2
                if depth == 0:
                    mode = None
            else:
                if blob[i] == "\n":
                    out.append("\n")
                i += 1
            continue
        if mode == "cart":
            o = _starts_any(blob, i, _CART_OPEN)
            if o:
                stack.append(_CART_CLOSE[_CART_OPEN.index(o)])
                i += len(o)
            elif stack and blob.startswith(stack[-1], i):
                i += len(stack[-1])
                stack.pop()
                if not stack:
                    mode = None
            else:
                if blob[i] == "\n":
                    out.append("\n")
                i += 1
            continue
        if mode == "string":
            if blob[i] == '"':
                mode, i = None, i + 1
            else:
                if blob[i] == "\n":
                    out.append("\n")
                i += 2 if blob[i] == "\\" else 1
            continue
        # Top level: dispatch into a construct, emitting one separator space for it.
        if blob.startswith("(*", i):
            out.append(" ")
            mode, depth, i = "comment", 1, i + 2
            continue
        op = _starts_any(blob, i, _CART_OPEN)
        if op:
            out.append(" ")
            mode, stack, i = "cart", [_CART_CLOSE[_CART_OPEN.index(op)]], i + len(op)
            continue
        if blob[i] == '"':
            out.append(" ")
            mode, i = "string", i + 1
            continue
        out.append(blob[i])
        i += 1
    if mode == "comment":
        end = ("comment", depth)
    elif mode == "cart":
        end = ("cart", stack)
    elif mode == "string":
        end = ("string",)
    else:
        end = None
    return "".join(out), end


def _strip(blob):
    """_strip_scan from the top level, keeping only the stripped text (the common case)."""
    return _strip_scan(blob)[0]


def _split_edit(old, new):
    """Split a str-replace's `new` into (lead, changed): the unchanged anchor lines it
    shares with `old` at the front (`lead`), and the genuinely-changed block after them
    (`changed`). A str_replace's new_str reproduces anchor context (whole lines carried
    over from old_str) around the change, so scanning all of new_str would flag a
    `by <method>` closer that was ALREADY in the file -- it lives on an unchanged anchor
    line, not a fresh guess. Only `changed` is scanned; `lead` is returned so a caller
    can reconstruct the text preceding the change (for edit-site state seeding).

    Line-granularity is deliberate: a `by <method>` closer lives within a line, so
    splitting only on whole identical lines can never cut a closer across the boundary
    (a character-level common-affix split could, hiding a real guess -- a soundness
    hole). It stays fail-safe the other way too: a changed line that merely reorders an
    existing closer is kept and re-checked (a harmless over-block), never dropped. When
    `old` is empty/absent (an insert, not a replace) all of `new` is changed."""
    if not old:
        return "", new
    old_lines, new_lines = old.split("\n"), new.split("\n")
    p = 0
    while p < len(old_lines) and p < len(new_lines) and old_lines[p] == new_lines[p]:
        p += 1
    s = 0
    while (s < len(old_lines) - p and s < len(new_lines) - p
           and old_lines[-1 - s] == new_lines[-1 - s]):
        s += 1
    return "\n".join(new_lines[:p]), "\n".join(new_lines[p:len(new_lines) - s])


def _first_str(d, keys):
    """Return d[k] for the first k in `keys` whose value is a str, else None."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return None


def _edit_site_state(paths, old, lead):
    """The strip-state at the START of a str-replace's changed region, read from the
    file on disk: whether that point sits inside a comment / cartouche / string whose
    delimiter is OUTSIDE the replaced fragment (e.g. editing prose deep inside a
    `text ‹..›` block). Returns a seed state for _strip_scan, or None (top level, or
    couldn't tell -> current fragment-only behaviour). Fail-open: any I/O or lookup
    trouble yields None, never an exception.

    The text before the changed region is exactly `content_up_to_old + lead`, and since
    `lead` is the unchanged leading anchor it is a verbatim prefix of `old`, so this
    equals a real prefix of the on-disk file -- we strip that and take its end state.
    `old` must occur exactly once (str_replace's own contract) or the location is
    ambiguous and we bail. All three are folded (see _canon_lookup) to reproduce the edit
    tools' own matching -- cartouche notation and trailing whitespace -- so a file written
    with `\\<open>` (and trailing spaces) is still located from an edit phrased with `‹`;
    the fold preserves strip state, so the seed is unaffected."""
    if not old:
        return None
    old_c, lead_c = _canon_lookup(old), _canon_lookup(lead)
    for p in paths:
        if not p.strip().lower().endswith(THY_EXT):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                content = _canon_lookup(f.read())
        except Exception:
            continue
        idx = content.find(old_c)
        if idx < 0 or content.find(old_c, idx + 1) != -1:
            continue  # absent, or not unique -> can't trust the edit location
        return _strip_scan(content[:idx] + lead_c)[1]
    return None


def _find_patch(ti, tool):
    """Return the apply_patch envelope string carried in `ti`, or None. Detected by
    the "*** Begin Patch" marker in any string value (so it is independent of the
    param name, and also catches an apply_patch heredoc inside a Bash command); for
    an explicit apply_patch tool, fall back to common key names if the marker is not
    visible. Returns None when absent so callers fall through to the normal path."""
    for v in ti.values():
        if isinstance(v, str) and PATCH_MARKER in v:
            return v
    if "apply_patch" in tool:
        for k in ("input", "patch", "diff", "changes", "content"):
            v = ti.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return None


def _parse_apply_patch(blob):
    """Return contextual added hunks plus whether the patch targets a theory.

    Each section is (effective-path, added-text, post-hunk-text, raw changed ranges).
    Context lines are retained in post-hunk-text so syntax such as a context `by`
    followed by an added method can be parsed as one expression. `Move to:` changes
    the effective target path; classifying only the original path would let a patch
    move a non-theory file into `*.thy` without running either guard.
    """
    sections, path, touched_thy = [], None, False
    hunk_lines = []

    def flush_hunk():
        nonlocal hunk_lines
        if path is None or not path.lower().endswith(THY_EXT) or not hunk_lines:
            hunk_lines = []
            return
        post, added, ranges, offset = [], [], [], 0
        for kind, body in hunk_lines:
            if kind == "-":
                continue
            if post:
                post.append("\n")
                offset += 1
            start = offset
            post.append(body)
            offset += len(body)
            if kind == "+":
                added.append(body)
                ranges.append((start, offset))
        if added:
            sections.append((path, "\n".join(added), "".join(post), ranges))
        hunk_lines = []

    def flush_file():
        nonlocal touched_thy
        flush_hunk()
        if path is not None and path.lower().endswith(THY_EXT):
            touched_thy = True

    for line in blob.splitlines():
        m = _PATCH_FILE_HDR.match(line)
        if m:
            flush_file()
            path = m.group(2).strip()
            continue
        m = _PATCH_MOVE_HDR.match(line)
        if m:
            flush_hunk()
            path = m.group(1).strip()
            continue
        if line.startswith("@@"):
            flush_hunk()
            continue
        if line.startswith("*** "):  # End Patch / other directives
            continue
        if path is not None and line[:1] in ("+", "-", " "):
            hunk_lines.append((line[0], line[1:]))
    flush_file()
    return sections, touched_thy


def _read_path(paths):
    """Return (path, content) for the first readable theory path."""
    for path in paths:
        if not path.strip().lower().endswith(THY_EXT):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return path, f.read()
        except Exception:
            continue
    return None, None


def _stripped_ranges(source, raw_ranges):
    """Translate raw source ranges to offsets in the construct-stripped source."""
    return [
        (len(_strip(source[:start])), len(_strip(source[:end])))
        for start, end in raw_ranges
    ]


def _with_context(fragment, context_source, raw_ranges, context_line=1):
    """Attach post-edit parsing context and changed spans to an added fragment."""
    return fragment._replace(
        context_text=_strip(context_source),
        context_source=context_source,
        context_line=context_line,
        changed_ranges=_stripped_ranges(context_source, raw_ranges),
    )


def _full_write_fragments(path, old, new):
    """Line-diff a whole-buffer write, preserving state and source line per block."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    new_keepends = new.splitlines(keepends=True)
    offsets = [0]
    for line_text in new_keepends:
        offsets.append(offsets[-1] + len(line_text))
    fragments = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal" or j1 == j2:
            continue
        source = "\n".join(new_lines[j1:j2])
        prefix = "\n".join(new_lines[:j1])
        if j1:
            prefix += "\n"
        seed = _strip_scan(prefix)[1]
        fragment = EditFragment(path, _strip_scan(source, seed)[0], source, j1 + 1)
        fragments.append(_with_context(
            fragment, new, [(offsets[j1], offsets[j2])], context_line=1))
    return fragments


def _replacement_fragments(paths, old, new, replace_all=False):
    """Extract one independently seeded changed fragment per replacement occurrence."""
    lead, changed = _split_edit(old, new)
    if not changed.strip():
        return []
    path, disk = _read_path(paths)
    if disk is None:
        seed = _edit_site_state(paths, old, lead)
        return [EditFragment(paths[0] if paths else None,
                             _strip_scan(changed, seed)[0], changed, 1)]

    content, old_c, new_c, lead_c = (
        _canon_lookup(disk), _canon_lookup(old), _canon_lookup(new), _canon_lookup(lead))
    positions, start = [], 0
    while old_c and (idx := content.find(old_c, start)) >= 0:
        positions.append(idx)
        start = idx + max(1, len(old_c))
    if not positions or (not replace_all and len(positions) != 1):
        return [EditFragment(path, _strip(changed), changed, 1)]
    if not replace_all:
        positions = positions[:1]
    fragments = []
    changed_c = _canon_lookup(changed)
    changed_in_new = new_c.find(changed_c, len(lead_c)) if changed_c else -1
    for idx in positions:
        source_line = content[:idx].count("\n") + lead_c.count("\n") + 1
        fragment = EditFragment(
            path,
            _strip_scan(changed, _strip_scan(content[:idx] + lead_c)[1])[0],
            changed,
            source_line,
        )
        if changed_in_new >= 0:
            post = content[:idx] + new_c + content[idx + len(old_c):]
            start = idx + changed_in_new
            fragments.append(_with_context(
                fragment, post, [(start, start + len(changed_c))], context_line=1))
        else:
            fragments.append(fragment)
    return fragments


def extract_thy_edits():
    try:
        # Read stdin as UTF-8 explicitly: the payload can carry multibyte cartouche
        # glyphs (‹ ›), which a non-UTF-8 locale default would mangle. Real stdin
        # exposes a binary `.buffer`; the tests inject a text StringIO that does not.
        stream = sys.stdin
        raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception:
        return None, ""
    return _extract_thy_edits_data(data)


def _extract_thy_edits_data(data):
    if not isinstance(data, dict):
        return None, ""

    tool = data.get("tool_name", "") or ""
    ti = data.get("tool_input", {}) or {}
    transcript = data.get("transcript_path", "") or ""
    if tool in EXEC_TOOL_NAMES:
        if isinstance(ti, str):
            source = ti
        elif isinstance(ti, dict):
            source = _first_str(ti, ("code", "source", "text"))
        else:
            source = None
        if not source:
            return None, transcript
        fragments = []
        for event in _orchestrated_events(source, transcript):
            nested, _ = _extract_thy_edits_data(event)
            if nested:
                fragments.extend(nested)
        return (fragments or None), transcript
    if not isinstance(ti, dict):
        return None, transcript

    # apply_patch (Codex's edit tool, or an apply_patch heredoc run via Bash): a
    # unified-diff envelope, parsed per-file so only added (+) lines in *.thy
    # sections are checked. Handled before the per-tool branches below since its
    # payload shape matches none of them.
    #
    # Only the apply_patch tool and Bash are treated as carrying an envelope. For any
    # other tool (Write/Edit/MCP), a "*** Begin Patch" marker in the payload is just
    # incidental content (e.g. a Write documenting a patch) -- it must NOT be parsed as
    # the edit, or (a) a marker with no *.thy section would suppress the real check
    # (a trivial bypass), and (b) a marker with a *.thy section would attribute the
    # edit to that theory instead of the tool's real, possibly non-theory, target.
    patch = _find_patch(ti, tool) if (PATCH_TOOL_SUBSTR in tool or tool == BASH_TOOL) else None
    if patch is not None:
        sections, touched_thy = _parse_apply_patch(patch)
        if touched_thy:
            fragments = [
                _with_context(
                    EditFragment(path, _strip(source), source, 1),
                    context_source,
                    raw_ranges,
                    context_line=1,
                )
                for path, source, context_source, raw_ranges in sections
                if source.strip()
            ]
            return (fragments or None), transcript
        if PATCH_TOOL_SUBSTR in tool:
            # A genuine apply_patch that touches no theory file -> nothing to check.
            return None, transcript
        # A Bash heredoc whose patch touches no theory: fall through to the normal Bash
        # path -- the surrounding command may still write a *.thy (redirect/tee), and
        # the *.thy write gate below decides.

    paths, fragments = [], []

    def add_path(v):
        if isinstance(v, str) and v.strip():
            paths.append(v)

    def add_text(v, path=None, seed=None, line=1):
        if isinstance(v, str) and v:
            fragments.append(EditFragment(path, _strip_scan(v, seed)[0], v, line))

    if tool == BASH_TOOL:
        command = ti.get("command", "")
        if not isinstance(command, str) or not _THY_WRITE.search(command):
            return None, transcript
        add_text(command)
    elif tool == "MultiEdit":
        for k in PATH_KEYS:
            if k in ti:
                add_path(ti[k])
        for e in ti.get("edits", []) or []:
            if isinstance(e, dict):
                new = e.get("new_string", e.get("new_str", ""))
                if isinstance(new, str):
                    old = _first_str(e, OLD_KEYS)
                    if old is not None:
                        fragments.extend(_replacement_fragments(
                            paths, old, new, bool(e.get("replace_all"))))
                    else:
                        add_text(new, paths[0] if paths else None)
    else:
        for k in PATH_KEYS:
            if k in ti:
                add_path(ti[k])
        # A str-replace edit (Edit, MCP write_file str_replace) carries an old/new pair:
        # reduce new_str to only the lines it changes, so a pre-existing closer on an
        # unchanged anchor line is not re-flagged as a fresh guess. Other content keys
        # (Write's whole-file `content`, an insert's lone new_str, ...) have no anchor to
        # subtract, so they are scanned whole.
        old = _first_str(ti, OLD_KEYS)
        new = _first_str(ti, NEW_KEYS)
        if old is not None and new is not None:
            fragments.extend(_replacement_fragments(
                paths, old, new, bool(ti.get("replace_all"))))
            for k in CONTENT_KEYS:
                if k not in NEW_KEYS and k in ti:
                    add_text(ti[k], paths[0] if paths else None)
        else:
            for k in CONTENT_KEYS:
                if k in ti:
                    value = ti[k]
                    # A whole-buffer write to an existing file must not re-flag every
                    # old closer. Diff it against disk; a new file is checked whole.
                    path, disk = _read_path(paths)
                    if (k in ("content", "text", "data", "code", "source")
                            and isinstance(value, str) and disk is not None):
                        fragments.extend(_full_write_fragments(path, disk, value))
                    else:
                        add_text(value, paths[0] if paths else None)
        # MCP write tools may use unknown param names: scan all string values.
        has_known_content = any(isinstance(ti.get(k), str) for k in CONTENT_KEYS)
        if (not fragments and not has_known_content
                and any(s in tool for s in MCP_WRITE_SUBSTRS)):
            for v in ti.values():
                if isinstance(v, str):
                    add_text(v, paths[0] if paths else None)

    if not fragments or not any(f.source.strip() for f in fragments):
        return None, transcript

    # Only Isabelle theory files (*.thy) are recognised. The extension check is
    # case-insensitive: on a case-insensitive filesystem (macOS) Foo.THY names the
    # same file as Foo.thy, so a case variant must not slip past the gate.
    if tool != BASH_TOOL:
        thy_in_paths = any(p.strip().lower().endswith(THY_EXT) for p in paths)
        if paths and not thy_in_paths:
            return None, transcript

    return fragments, transcript


def extract_thy_edit():
    """Compatibility facade returning the formerly exposed joined check text."""
    fragments, transcript = extract_thy_edits()
    if fragments is None:
        return None, transcript
    return "\n".join(fragment.text for fragment in fragments), transcript
