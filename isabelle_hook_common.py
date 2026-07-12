"""Compatibility facade for the shared Isabelle hook parsing API."""
from isabelle_hooks.edits import (
    EditFragment as EditFragment,
    extract_thy_edit as extract_thy_edit,
    extract_thy_edits as extract_thy_edits,
    orchestrated_tool_calls as orchestrated_tool_calls,
)
from isabelle_hooks.protocol import (
    BASH_TOOL as BASH_TOOL,
    CONTENT_KEYS as CONTENT_KEYS,
    EXEC_TOOL_NAMES as EXEC_TOOL_NAMES,
    MCP_WRITE_SUBSTRS as MCP_WRITE_SUBSTRS,
    NEW_KEYS as NEW_KEYS,
    OLD_KEYS as OLD_KEYS,
    PATCH_MARKER as PATCH_MARKER,
    PATCH_TOOL_SUBSTR as PATCH_TOOL_SUBSTR,
    PATH_KEYS as PATH_KEYS,
    THY_EXT as THY_EXT,
)
from isabelle_hooks.syntax import _strip as _strip
