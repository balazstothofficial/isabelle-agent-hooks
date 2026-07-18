"""Edit extraction pipeline for the Isabelle PreToolUse hooks.

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
envelope -- including an apply_patch heredoc inside a Bash command), the I/Q MCP
write_file/save_file/open_file creation paths, and the isabelle-pide-mcp `edit` tool.
Codex functions.exec
programs are unwrapped into those same direct events when their nested tool arguments
are literal. The PIDE edit is checked when its `origin` is a *.thy path (a
session-qualified theory name such as HOL.Nat is not a *.thy path, so it is not
recognised).
"""
import sys, json, re, difflib
from functools import lru_cache

from .models import EditFragment
from .mutations import PatchMutation, ReplacementMutation, VirtualFileStore
from .javascript import (
    orchestrated_tool_calls as orchestrated_tool_calls, _orchestrated_events,
)
from .protocol import (
    BASH_TOOL,
    CONTENT_KEYS,
    EXEC_SOURCE_KEYS,
    MULTI_EDIT_TOOL,
    NEW_KEYS,
    OLD_KEYS,
    PATCH_MARKER,
    PATH_KEYS,
    THY_EXT,
    is_exec_tool,
    is_mcp_write_tool,
    is_patch_tool,
)
from .syntax import _canon_lookup, _strip_scan, _strip

# The replacement half of a replace-style edit, and its paired "old" half. When both are
# present the edit is a REPLACE, so only the lines the new text changes are actually
# added -- the unchanged anchor lines it reproduces from the old text must be excluded
# (see _split_edit), and the strip can be seeded at the edit site (see _edit_site_state).
# Covers Edit / MultiEdit (old_string/new_string), the iq-dev write_file str_replace
# (old_str/new_str), and the isabelle-pide-mcp `edit` tool (old_text/text). `text` is
# last in NEW_KEYS so a more specific new_string/new_str wins; it only ever pairs here
# when an OLD key is also present (a bare `text` -- Write, a PIDE insert -- is scanned
# whole via CONTENT_KEYS, untouched by this pairing).
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
    if is_patch_tool(tool):
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
        before, post, added, ranges, offset = [], [], [], [], 0
        for kind, body in hunk_lines:
            if kind != "+":
                if before:
                    before.append("\n")
                before.append(body)
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
            sections.append(PatchMutation(
                path, "\n".join(added), "".join(before), "".join(post),
                tuple(ranges)))
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


@lru_cache(maxsize=32)
def _strip_cached(source):
    """Share full-snapshot lexical work across policies and changed fragments."""
    return _strip(source)


def _stripped_ranges(source, raw_ranges):
    """Translate raw source ranges to offsets in the construct-stripped source."""
    return [
        (len(_strip_cached(source[:start])), len(_strip_cached(source[:end])))
        for start, end in raw_ranges
    ]


def _with_context(fragment, context_source, raw_ranges, context_line=1,
                  before_source=None, after_source=None):
    """Attach post-edit parsing context and changed spans to an added fragment."""
    return fragment._replace(
        context_text=_strip_cached(context_source),
        context_source=context_source,
        context_line=context_line,
        changed_ranges=_stripped_ranges(context_source, raw_ranges),
        before_source=before_source,
        after_source=after_source,
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
            fragment, new, [(offsets[j1], offsets[j2])], context_line=1,
            before_source=old, after_source=new))
    return fragments


def _replacement_fragments(paths, old, new, replace_all=False, snapshots=None):
    """Extract one independently seeded changed fragment per replacement occurrence."""
    lead, changed = _split_edit(old, new)
    if not changed.strip() and snapshots is None:
        return []
    path, disk = snapshots.read(paths) if snapshots is not None else _read_path(paths)
    if disk is None:
        if not changed.strip():
            return []
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
        if not changed.strip():
            return []
        return [EditFragment(path, _strip(changed), changed, 1)]
    if not replace_all:
        positions = positions[:1]
    if snapshots is not None:
        post = content
        for idx in reversed(positions):
            post = post[:idx] + new_c + post[idx + len(old_c):]
        snapshots.write(path, post)
    if not changed.strip():
        return []
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
                fragment, post, [(start, start + len(changed_c))], context_line=1,
                before_source=content, after_source=post))
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


def _attach_snapshot_pairs(fragments, snapshots):
    changes = snapshots.changes()
    if not changes:
        return fragments
    by_key = {
        VirtualFileStore._key(path): pair for path, pair in changes.items()
    }
    return [
        fragment._replace(before_source=pair[0], after_source=pair[1])
        if fragment.path and (pair := by_key.get(
            VirtualFileStore._key(fragment.path))) is not None
        else fragment
        for fragment in fragments
    ]


def _extract_thy_edits_data(data, snapshots=None):
    """Analyze one hook request, attaching one original/final snapshot per path."""
    owns_snapshots = snapshots is None
    if owns_snapshots:
        snapshots = VirtualFileStore(_read_path)
    fragments, transcript = _extract_thy_edits_data_impl(data, snapshots)
    if owns_snapshots and fragments:
        fragments = _attach_snapshot_pairs(fragments, snapshots)
    return fragments, transcript


def _extract_thy_edits_data_impl(data, snapshots):
    if not isinstance(data, dict):
        return None, ""

    tool = data.get("tool_name", "") or ""
    ti = data.get("tool_input", {}) or {}
    transcript = data.get("transcript_path", "") or ""
    if is_exec_tool(tool):
        if isinstance(ti, str):
            source = ti
        elif isinstance(ti, dict):
            source = _first_str(ti, EXEC_SOURCE_KEYS)
        else:
            source = None
        if not source:
            return None, transcript
        fragments = []
        for event in _orchestrated_events(source, transcript):
            nested, _ = _extract_thy_edits_data(event, snapshots)
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
    patch = _find_patch(ti, tool) if (is_patch_tool(tool) or tool == BASH_TOOL) else None
    if patch is not None:
        sections, touched_thy = _parse_apply_patch(patch)
        if touched_thy:
            fragments = [
                _with_context(
                    EditFragment(
                        mutation.path,
                        _strip(mutation.added_text),
                        mutation.added_text,
                        1,
                    ),
                    mutation.context_text,
                    mutation.changed_ranges,
                    context_line=1,
                    before_source=mutation.before_text,
                    after_source=mutation.context_text,
                )
                for mutation in sections
                if mutation.added_text.strip()
            ]
            # Reconstruct full snapshots when every hunk has a unique exact anchor.
            # This supplies relocation analysis with the same before/after view as
            # Write/Edit without weakening ordinary patch checking when reconstruction
            # is impossible.
            for mutation in sections:
                current_path, current = snapshots.read((mutation.path,))
                if current is None:
                    if not mutation.before_text:
                        snapshots.write(mutation.path, mutation.context_text)
                    continue
                occurrences = current.count(mutation.before_text)
                if mutation.before_text and occurrences == 1:
                    snapshots.write(
                        current_path,
                        current.replace(mutation.before_text, mutation.context_text, 1),
                    )
            return (fragments or None), transcript
        if is_patch_tool(tool):
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
    elif tool == MULTI_EDIT_TOOL:
        for k in PATH_KEYS:
            if k in ti:
                add_path(ti[k])
        for e in ti.get("edits", []) or []:
            if isinstance(e, dict):
                new = e.get("new_string", e.get("new_str", ""))
                if isinstance(new, str):
                    old = _first_str(e, OLD_KEYS)
                    if old is not None:
                        mutation = ReplacementMutation(
                            tuple(paths), old, new, bool(e.get("replace_all")))
                        fragments.extend(_replacement_fragments(
                            mutation.paths, mutation.old, mutation.new,
                            mutation.replace_all, snapshots))
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
                paths, old, new, bool(ti.get("replace_all")), snapshots))
            for k in CONTENT_KEYS:
                if k not in NEW_KEYS and k in ti:
                    add_text(ti[k], paths[0] if paths else None)
        else:
            for k in CONTENT_KEYS:
                if k in ti:
                    value = ti[k]
                    # A whole-buffer write to an existing file must not re-flag every
                    # old closer. Diff it against disk; a new file is checked whole.
                    path, disk = snapshots.read(paths)
                    if (k in ("content", "text", "data", "code", "source")
                            and isinstance(value, str) and disk is not None):
                        fragments.extend(_full_write_fragments(path, disk, value))
                        snapshots.write(path, value)
                    else:
                        add_text(value, paths[0] if paths else None)
                        if (k in ("content", "text", "data", "code", "source")
                                and isinstance(value, str) and paths):
                            snapshots.write(paths[0], value)
        # MCP write tools may use unknown param names: scan all string values.
        has_known_content = any(isinstance(ti.get(k), str) for k in CONTENT_KEYS)
        if (not fragments and not has_known_content
                and is_mcp_write_tool(tool)):
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
