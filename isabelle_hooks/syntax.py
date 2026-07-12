"""Isabelle lexical stripping and edit-site text canonicalization."""


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
