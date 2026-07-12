"""Shared immutable data models for hook edit analysis."""
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
