"""Canonical external tool, payload, transcript, and hook-contract vocabulary."""
import re


# Hook process contract.
ALLOW_EXIT = 0
BLOCK_EXIT = 2

# Isabelle edit targets and common payload fields.
THY_EXT = ".thy"
PATH_KEYS = ("file_path", "path", "filename", "file", "target", "origin")
CONTENT_KEYS = ("content", "text", "new_string", "new_str", "data", "code", "source")
NEW_KEYS = ("new_string", "new_str", "text")
OLD_KEYS = ("old_string", "old_str", "old_text")

# Canonical direct-tool names and namespaced aliases.
BASH_TOOL = "Bash"
MULTI_EDIT_TOOL = "MultiEdit"
PATCH_TOOL_NAMES = ("apply_patch",)
PATCH_MARKER = "*** Begin Patch"
MCP_WRITE_TOOL_NAMES = ("write_file", "save_file")
OPEN_FILE_TOOL_NAMES = ("open_file",)
EXEC_TOOL_NAMES = ("functions.exec", "functions_exec")
EXEC_COMMAND_TOOL_NAMES = ("exec_command",)
EXEC_SOURCE_KEYS = ("code", "source", "text")
EXEC_COMMAND_FIELD = "cmd"
# Compatibility names retained by isabelle_hook_common's historical import surface.
PATCH_TOOL_SUBSTR = PATCH_TOOL_NAMES[0]
MCP_WRITE_SUBSTRS = MCP_WRITE_TOOL_NAMES

# Transcript content-block schemas and command-bearing input fields.
USE_EVENT_TYPES = ("tool_use", "function_call", "tool_call")
RESULT_EVENT_TYPES = (
    "tool_result", "function_call_output", "tool_output", "function_output")
COMMAND_KEYS = ("command", "method", "tactic", "tac", "proof", "step", "isar_text")

# Calls which represent an edit, or can move the proof/file state searched earlier.
EDIT_TOOL_NAMES = (
    "write", "edit", *PATCH_TOOL_NAMES, MULTI_EDIT_TOOL.lower(), *MCP_WRITE_TOOL_NAMES)
PROOF_STATE_TOOL_NAMES = ("repl_step", "repl_undo", "repl_reset", "repl_load")


def tool_name_matches(name, alias):
    """Match an exact or namespaced tool-name component, never an incidental substring."""
    if not isinstance(name, str) or not isinstance(alias, str):
        return False
    return re.search(
        r"(?:^|[^a-z0-9])" + re.escape(alias.lower()) + r"(?:$|[^a-z0-9])",
        name.lower(),
    ) is not None


def tool_name_ends_with(name, alias):
    """Match a namespaced tool whose final component is the requested alias."""
    if not isinstance(name, str) or not isinstance(alias, str):
        return False
    return re.search(
        r"(?:^|[^a-z0-9])" + re.escape(alias.lower()) + r"$", name.lower()
    ) is not None


def _matches_any(name, aliases):
    return any(tool_name_matches(name, alias) for alias in aliases)


def is_exec_tool(name):
    return isinstance(name, str) and name.lower() in EXEC_TOOL_NAMES


def is_exec_command_tool(name):
    return _matches_any(name, EXEC_COMMAND_TOOL_NAMES)


def is_patch_tool(name):
    return _matches_any(name, PATCH_TOOL_NAMES)


def is_mcp_write_tool(name):
    return _matches_any(name, MCP_WRITE_TOOL_NAMES)


def is_open_file_tool(name):
    return _matches_any(name, OPEN_FILE_TOOL_NAMES)


def is_edit_tool_name(name):
    return name.lower() == BASH_TOOL.lower() or _matches_any(name, EDIT_TOOL_NAMES)


def is_evidence_invalidating_tool(name):
    return is_edit_tool_name(name) or _matches_any(name, PROOF_STATE_TOOL_NAMES)
