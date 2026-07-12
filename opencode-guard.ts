// @ts-nocheck -- standalone OpenCode/Bun plugin, not part of a TS project: there is
// no tsconfig or @types/node here, so suppress editor semantic errors (node: imports,
// implicit any). OpenCode type-checks and runs it at load; this is an inert comment.
//
// OpenCode adapter for the Isabelle agent hooks.
//
// Claude Code and Codex configure these guards through JSON hook config
// (.claude/settings.local.json / .codex/hooks.json). OpenCode has no such config --
// only a TypeScript plugin API -- so this plugin bridges OpenCode's
// `tool.execute.before` onto the SAME Python guards: it normalizes OpenCode's tool
// name + args into the stdin contract the guards read (see README "Contract"), runs
// each guard via the configured interpreter, and throws on exit code 2 to deny the
// call. It also logs every tool call AND its result to a per-worktree JSONL so
// no_guessed_proofs' "recent sledgehammer/try0" escape hatch keeps working: that
// guard only accepts a searchable `by` method when the method appears in the search
// run's OWN result, so a transcript with calls but no results can never satisfy it
// (every such write would then be over-blocked). tool.execute.after supplies the
// paired result event.
//
// It is config-driven -- see README "OpenCode" for install steps. On load it reads
// `guards.json` from the hooks directory (a sibling `../hooks` of this plugin):
//
//   {
//     "interpreter": "python3",              // optional; see INTERPRETER below
//     "defaultMatcher": "<project matcher>",
//     "hooks": [
//       { "script": "no_guessed_proofs.py" },
//       { "script": "no_apply_scripts.py" }
//     ]
//   }
//
// A missing or invalid guards.json fails open (no guards run) rather than blocking
// every tool call.
import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

// The Python guards (and guards.json) live in `../hooks` relative to THIS plugin
// file, never a host-supplied cwd/worktree: OpenCode passes "/" as its "no git
// worktree" sentinel (and "" before init), either of which would resolve the guards
// under the filesystem root ("/.opencode/hooks/..."), where python exits 2 on the
// missing file and every tool call is denied. The plugin ships at
// .opencode/plugins/<name>.ts and is loaded via `await import(path)`, so `../hooks`
// is a stable sibling regardless of cwd. Resolve through the standard ESM URL:
// OpenCode CLI runs plugins under Bun (which provides import.meta.dir), while the
// desktop app runs them under Node (which does not). import.meta.url works in both.
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const HOOKS_DIR = join(PLUGIN_DIR, "..", "hooks");

// Read guards.json once at load. Shape:
// { interpreter?, defaultMatcher?, hooks: [{script,matcher?,args?}] }.
// Any read/parse failure fails open (empty hook list) so a misconfigured install
// never bricks OpenCode.
function loadConfig() {
  try {
    const cfg = JSON.parse(readFileSync(join(HOOKS_DIR, "guards.json"), "utf8"));
    const defaultMatcher = cfg && typeof cfg.defaultMatcher === "string"
      ? cfg.defaultMatcher : undefined;
    const hooks = cfg && Array.isArray(cfg.hooks) ? cfg.hooks.flatMap((hook) => {
      if (!hook || typeof hook !== "object"
          || typeof hook.script !== "string" || hook.script.length === 0
          || (hook.args !== undefined
            && (!Array.isArray(hook.args)
              || !hook.args.every((arg) => typeof arg === "string")))) return [];
      const matcher = typeof hook.matcher === "string" ? hook.matcher : defaultMatcher;
      return typeof matcher === "string" ? [{ ...hook, matcher }] : [];
    }) : [];
    return {
      interpreter: cfg && typeof cfg.interpreter === "string" && cfg.interpreter
        ? cfg.interpreter : undefined,
      hooks,
    };
  } catch (e) {
    return { interpreter: undefined, hooks: [] };
  }
}
const CONFIG = loadConfig();

// The interpreter that runs each guard. A packaged install can pin an
// absolute python3 via guards.json; a plain install omits it and falls back to
// $ISABELLE_HOOKS_PYTHON or `python3` on PATH.
const INTERPRETER = CONFIG.interpreter || process.env.ISABELLE_HOOKS_PYTHON || "python3";
const HOOKS = CONFIG.hooks;

// OpenCode tool id -> the tool_name the Python guards key on (Claude's names).
function claudeToolName(tool) {
  if (tool === "bash") return "Bash";
  if (tool === "edit") return "Edit";
  if (tool === "write") return "Write";
  if (tool === "multiedit") return "MultiEdit";
  return tool; // MCP tools (e.g. "..._write_file") pass through
}

// OpenCode tool args -> the tool_input isabelle_hook_common.py reads.
function toolInput(tool, args) {
  args = args || {};
  if (tool === "bash") return { command: args.command };
  if (tool === "edit") return { file_path: args.filePath, old_string: args.oldString, new_string: args.newString };
  if (tool === "write") return { file_path: args.filePath, content: args.content };
  if (tool === "multiedit") return {
    file_path: args.filePath,
    edits: (args.edits || []).map((e) => ({ old_string: (e || {}).oldString, new_string: (e || {}).newString })),
  };
  return args; // MCP tools: hand raw args through (the parser scans string values)
}

export const IsabelleGuards = async ({ worktree, directory }) => {
  // worktree/directory are used only as a best-effort key for the per-worktree
  // transcript path (uniqueness across checkouts). OpenCode uses "/" as the
  // no-Git-worktree sentinel, so prefer its real project directory in that case.
  const root = worktree && worktree !== "/" ? worktree : directory || process.cwd();
  const transcript = join(tmpdir(), "opencode-isabelle-guard-" + encodeURIComponent(root) + ".jsonl");

  // Append one content block, wrapped as the {"message":{"content":[…]}} envelope
  // the Python transcript parser (no_guessed_proofs._iter_blocks) recognises.
  function logBlock(block) {
    try {
      appendFileSync(transcript, JSON.stringify({ message: { content: [block] } }) + "\n");
    } catch (e) { /* logging is best-effort */ }
  }

  // A tool CALL, tagged with OpenCode's callID as the `id` so the guard can pair it
  // with its own result (recent_method_evidence keys on tool_use_id).
  function logCall(id, tool, args) {
    logBlock({ type: "tool_use", id, name: tool, input: args || {} });
  }

  // The RESULT for call `id`. This is what makes the escape hatch work under OpenCode:
  // the guard needs the searched method to show up in the search run's own result, and
  // a call with no logged result can never supply that (so a legit `by <found-method>`
  // write would be blocked). `content` is the tool's textual output verbatim; the
  // Python side (_result_text) flattens str/list/dict shapes and lowercases them.
  function logResult(id, content) {
    logBlock({ type: "tool_result", tool_use_id: id, content: content == null ? "" : content });
  }

  return {
    "tool.execute.before": async (input, output) => {
      const tool = input.tool;
      const args = (output && output.args) || {};

      const tname = claudeToolName(tool);
      const payload = JSON.stringify({
        tool_name: tname,
        tool_input: toolInput(tool, args),
        transcript_path: transcript,
      });

      for (const h of HOOKS) {
        let re;
        try { re = new RegExp(h.matcher); } catch (e) { continue; }
        if (!re.test(tname) && !re.test(tool)) continue;
        const script = join(HOOKS_DIR, h.script);
        // A missing script makes python exit 2 -- indistinguishable from a guard
        // deny -- so skip (fail open) rather than block every tool call.
        if (!existsSync(script)) continue;
        const res = spawnSync(INTERPRETER, [script].concat(h.args || []), { input: payload, encoding: "utf8" });
        if (res.status === 2) {
          throw new Error((res.stderr || "").trim() || ("Blocked by guard " + h.script));
        }
        // Any other status (incl. spawn error) fails open, matching the guards.
      }
      // Record only calls which every matching guard allowed. A denied edit never
      // executes and therefore must not invalidate proof-search evidence on retry.
      // Search calls are still logged before execution and paired in the after hook.
      logCall(input.callID, tool, args);
    },
    "tool.execute.after": async (input, output) => {
      // Pair the result with its call (same callID) so a sledgehammer/try0 run's
      // FOUND method is in the transcript for the guard to see on the next theory
      // write. Without this the escape hatch never fires under OpenCode. Fires for
      // every tool; the guard ignores results of non-search calls.
      logResult(input.callID, output && output.output);
    },
  };
};
