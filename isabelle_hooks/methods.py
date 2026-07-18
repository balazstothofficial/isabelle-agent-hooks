"""Parsing of Isabelle ``by`` method expressions and changed-token filtering."""
import re
from collections import namedtuple


MethodHit = namedtuple("MethodHit", "name offset by_offset")

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
# listing one can never suppress a real guess; the same-line bound in by_method_hits is
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
