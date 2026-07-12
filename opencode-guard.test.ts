// @ts-nocheck -- standalone bun:test, no tsconfig/@types here (matches opencode-guard.ts).
//
// Tests for the OpenCode adapter (opencode-guard.ts). The plugin reads its config
// (interpreter + hook descriptors) from `guards.json` in its sibling `../hooks`
// directory at load time, so each test lays out a worktree (.opencode/plugins/ +
// .opencode/hooks/), writes the guards.json + any hook scripts, drops a copy of the
// real plugin into .opencode/plugins/, and imports it. The copy differs from the
// shipped file only by an appended re-export of the module-private helpers (so the
// pure mappers can be unit-tested without exporting them from the plugin, which would
// confuse OpenCode's plugin loader). We cover:
//   * claudeToolName / toolInput   -- the pure OpenCode->Claude arg mapping
//   * the tool.execute.before loop -- matcher selection, exit-code-2 => throw (deny),
//     any other status => fail open, missing script => fail open, missing/invalid
//     guards.json => fail open, and guard resolution surviving a "/" worktree sentinel
//   * tool.execute.after transcript logging -- the escape-hatch evidence shape
//   * Node/Bun-compatible plugin-relative paths and non-Git transcript isolation
//
// Run: bun test opencode-guard.test.ts
import { test, expect, describe, beforeAll } from "bun:test";
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

const SOURCE = join(import.meta.dir, "opencode-guard.ts");
// A real interpreter to stand in for a packaged python3. Resolved at runtime so this
// works both locally and in CI (which has python3 on PATH).
const INTERP = Bun.which("python3") || "python3";
const REAL_NO_APPLY_HOOKS = [{ matcher: ".*write_file", script: "no_apply_scripts.py", args: [] }];
const REAL_PACKAGE_SCRIPTS = Object.fromEntries(
  readdirSync(join(import.meta.dir, "isabelle_hooks"))
    .filter((name) => name.endsWith(".py"))
    .map((name) => [
      join("isabelle_hooks", name),
      readFileSync(join(import.meta.dir, "isabelle_hooks", name), "utf8"),
    ]),
);
const REAL_NO_APPLY_SCRIPTS = {
  "no_apply_scripts.py": readFileSync(join(import.meta.dir, "no_apply_scripts.py"), "utf8"),
  "isabelle_hook_common.py": readFileSync(join(import.meta.dir, "isabelle_hook_common.py"), "utf8"),
  ...REAL_PACKAGE_SCRIPTS,
};

describe("shipped matcher compatibility", () => {
  test("the shared default accepts optional functions.exec orchestration", () => {
    const config = JSON.parse(readFileSync(join(import.meta.dir, "guards.json"), "utf8"));
    expect(config.defaultMatcher).toContain("functions[.]exec");
    expect(config.hooks.every((hook) => hook.matcher === undefined)).toBe(true);
    expect(config.hooks.every((hook) => hook.args === undefined)).toBe(true);
  });
});

describe("default matcher", () => {
  test("hooks inherit the top-level matcher and may override it", async () => {
    const root = makeWorktree({
      hooks: {
        "block.py": "import sys; sys.exit(2)\n",
        "allow.py": "import sys; sys.exit(0)\n",
      },
      config: {
        defaultMatcher: "Bash",
        hooks: [
          { script: "block.py" },
          { script: "allow.py", matcher: "Write" },
        ],
      },
    });
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: root });
    await expect(
      plugin["tool.execute.before"]({ tool: "bash" }, { args: { command: "x" } }),
    ).rejects.toThrow(/block\.py/);
  });
});

let counter = 0;
// Lay out a worktree: .opencode/hooks/ (scripts + guards.json) and .opencode/plugins/.
// `hooks` maps a script basename to a python body; `config` becomes guards.json
// (interpreter defaults to the resolved python3 so the exit-code contract holds).
function makeWorktree({ hooks = {}, config = null } = {}) {
  const root = mkdtempSync(join(tmpdir(), "opencode-guard-wt-"));
  const hooksDir = join(root, ".opencode", "hooks");
  mkdirSync(hooksDir, { recursive: true });
  mkdirSync(join(root, ".opencode", "plugins"), { recursive: true });
  for (const [name, body] of Object.entries(hooks)) {
    const target = join(hooksDir, name);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, body);
  }
  if (config !== null) {
    writeFileSync(join(hooksDir, "guards.json"), typeof config === "string" ? config : JSON.stringify(config));
  }
  return root;
}

// Copy the real plugin into <root>/.opencode/plugins so its `../hooks` (and thus
// guards.json) resolve to this worktree, re-export the private helpers, and import it.
// The config is read at module load, so a fresh copy per call re-reads guards.json.
async function loadModule(root) {
  let text = readFileSync(SOURCE, "utf8");
  text += "\nexport { claudeToolName, toolInput };\n";
  const path = join(root, ".opencode", "plugins", `opencode-guard-${process.pid}-${counter++}.ts`);
  writeFileSync(path, text);
  return import(path);
}

// Helper-only module: no worktree layout needed beyond a scratch dir for the copy.
async function loadHelpers() {
  return loadModule(makeWorktree());
}

describe("claudeToolName", () => {
  let m: any;
  beforeAll(async () => {
    m = await loadHelpers();
  });
  test("maps OpenCode tool ids to Claude tool names", () => {
    expect(m.claudeToolName("bash")).toBe("Bash");
    expect(m.claudeToolName("edit")).toBe("Edit");
    expect(m.claudeToolName("write")).toBe("Write");
    expect(m.claudeToolName("multiedit")).toBe("MultiEdit");
  });
  test("passes MCP tool ids through unchanged", () => {
    expect(m.claudeToolName("autocorrode_write_file")).toBe("autocorrode_write_file");
  });
});

describe("toolInput", () => {
  let m: any;
  beforeAll(async () => {
    m = await loadHelpers();
  });
  test("bash -> { command }", () => {
    expect(m.toolInput("bash", { command: "echo hi" })).toEqual({ command: "echo hi" });
  });
  test("edit -> Claude file_path/old_string/new_string", () => {
    expect(m.toolInput("edit", { filePath: "a.thy", oldString: "x", newString: "y" })).toEqual({
      file_path: "a.thy",
      old_string: "x",
      new_string: "y",
    });
  });
  test("write -> Claude file_path/content", () => {
    expect(m.toolInput("write", { filePath: "a.thy", content: "c" })).toEqual({
      file_path: "a.thy",
      content: "c",
    });
  });
  test("multiedit -> Claude file_path + edits[].old_string/new_string", () => {
    expect(
      m.toolInput("multiedit", {
        filePath: "a.thy",
        edits: [
          { oldString: "x", newString: "apply auto" },
          { oldString: "p", newString: "q" },
        ],
      }),
    ).toEqual({
      file_path: "a.thy",
      edits: [
        { old_string: "x", new_string: "apply auto" },
        { old_string: "p", new_string: "q" },
      ],
    });
  });
  test("multiedit with missing edits degrades to an empty list", () => {
    expect(m.toolInput("multiedit", { filePath: "a.thy" })).toEqual({
      file_path: "a.thy",
      edits: [],
    });
  });
  test("MCP tool args pass through raw", () => {
    const raw = { some: "thing" };
    expect(m.toolInput("autocorrode_write_file", raw)).toEqual(raw);
  });
  test("missing args degrade to an empty object", () => {
    expect(m.toolInput("bash", undefined)).toEqual({ command: undefined });
  });
});

// Run tool.execute.before against a worktree whose guards.json lists `hooks`, each
// backed by a python script that exits with the given code (or an arbitrary body).
async function runBefore(hooks, scripts, tool, args, interpreter = INTERP) {
  const config = { interpreter, hooks };
  const root = makeWorktree({ hooks: scripts, config });
  const m = await loadModule(root);
  const plugin = await m.IsabelleGuards({ worktree: root });
  return plugin["tool.execute.before"]({ tool }, { args });
}

describe("tool.execute.before", () => {
  test("the real guard blocks a direct OpenCode MCP write_file apply script", async () => {
    await expect(
      runBefore(
        REAL_NO_APPLY_HOOKS,
        REAL_NO_APPLY_SCRIPTS,
        "iq-dev_write_file",
        {
          path: "Foo.thy",
          command: "str_replace",
          old_str: "by simp",
          new_str: "apply (rule TrueI) done",
        },
      ),
    ).rejects.toThrow(/BLOCKED write to an Isabelle theory/);
  });

  test("the real guard allows a direct OpenCode MCP write_file control", async () => {
    await expect(
      runBefore(
        REAL_NO_APPLY_HOOKS,
        REAL_NO_APPLY_SCRIPTS,
        "iq-dev_write_file",
        {
          path: "Foo.thy",
          command: "str_replace",
          old_str: "by (rule TrueI)",
          new_str: "by simp",
        },
      ),
    ).resolves.toBeUndefined();
  });

  test("exit code 2 from a matching hook denies the call with a message", async () => {
    // Assert the message, not just that it threw: this proves the hook actually ran
    // and produced exit 2. No stderr => the fallback "Blocked by guard <script>".
    await expect(
      runBefore(
        [{ matcher: "Bash", script: "block.py", args: [] }],
        { "block.py": "import sys; sys.exit(2)\n" },
        "bash",
        { command: "x" },
      ),
    ).rejects.toThrow(/Blocked by guard block\.py/);
  });

  test("a denying hook's stderr is surfaced as the thrown message", async () => {
    await expect(
      runBefore(
        [{ matcher: "Bash", script: "deny.py", args: [] }],
        { "deny.py": 'import sys; sys.stderr.write("guessed proof: metis\\n"); sys.exit(2)\n' },
        "bash",
        { command: "x" },
      ),
    ).rejects.toThrow(/guessed proof: metis/);
  });

  test("any other exit code fails open (no throw)", async () => {
    await expect(
      runBefore(
        [{ matcher: "Bash", script: "allow.py", args: [] }],
        { "allow.py": "import sys; sys.exit(0)\n" },
        "bash",
        { command: "x" },
      ),
    ).resolves.toBeUndefined();
  });

  test("a spawn failure (unresolvable interpreter) fails OPEN, not closed", async () => {
    // res.status is null on a spawn error, not 2, so the guard must allow. This
    // distinguishes "correctly allowed" from "the hook never ran".
    await expect(
      runBefore(
        [{ matcher: "Bash", script: "block.py", args: [] }],
        { "block.py": "import sys; sys.exit(2)\n" },
        "bash",
        { command: "x" },
        "/nonexistent/interpreter-xyz",
      ),
    ).resolves.toBeUndefined();
  });

  test("a hook whose matcher does not match is skipped", async () => {
    await expect(
      runBefore(
        [{ matcher: "Write", script: "block.py", args: [] }],
        { "block.py": "import sys; sys.exit(2)\n" },
        "bash",
        { command: "x" },
      ),
    ).resolves.toBeUndefined();
  });

  test("a missing hook script fails OPEN, not closed", async () => {
    // Guards resolve from the plugin's own dir; an absent script would make python
    // exit 2 (== deny). The plugin must skip it instead of blocking every call.
    await expect(
      runBefore(
        [{ matcher: "Bash", script: "absent.py", args: [] }],
        {}, // no scripts written
        "bash",
        { command: "x" },
      ),
    ).resolves.toBeUndefined();
  });

  test("a missing guards.json fails OPEN (no hooks run)", async () => {
    // No config file at all: loadConfig() must degrade to an empty hook list rather
    // than throwing, so OpenCode keeps working on a misconfigured install.
    const root = makeWorktree({ hooks: { "block.py": "import sys; sys.exit(2)\n" } }); // config omitted
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: root });
    await expect(
      plugin["tool.execute.before"]({ tool: "bash" }, { args: { command: "x" } }),
    ).resolves.toBeUndefined();
  });

  test("an invalid guards.json fails OPEN (no hooks run)", async () => {
    const root = makeWorktree({ hooks: { "block.py": "import sys; sys.exit(2)\n" }, config: "{ not json" });
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: root });
    await expect(
      plugin["tool.execute.before"]({ tool: "bash" }, { args: { command: "x" } }),
    ).resolves.toBeUndefined();
  });

  test("malformed hook descriptors are skipped and fail open", async () => {
    const root = makeWorktree({
      hooks: { "block.py": "import sys; sys.exit(2)\n" },
      config: { hooks: [
        {},
        { matcher: "Bash", script: 42 },
        { matcher: "Bash", script: "block.py", args: "not-an-array" },
      ] },
    });
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: root });
    await expect(
      plugin["tool.execute.before"]({ tool: "bash" }, { args: { command: "x" } }),
    ).resolves.toBeUndefined();
  });

  test('a "/" worktree sentinel does not derail guard resolution', async () => {
    // OpenCode passes worktree "/" when the project is not a git worktree. Guards
    // must still resolve from the plugin file (../hooks), so a real deny still fires
    // even though the passed worktree is "/".
    const root = makeWorktree({
      hooks: { "block.py": "import sys; sys.exit(2)\n" },
      config: { interpreter: INTERP, hooks: [{ matcher: "Bash", script: "block.py", args: [] }] },
    });
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: "/" });
    await expect(
      plugin["tool.execute.before"]({ tool: "bash" }, { args: { command: "x" } }),
    ).rejects.toThrow(/Blocked by guard block\.py/);
  });
});

// The no_guessed_proofs escape hatch (recent_method_evidence in the Python guard)
// only accepts a searchable `by` method when that method appears in the search run's
// OWN result -- so the bridge must log a paired tool_result, not just the tool_use.
describe("transcript logging (escape-hatch evidence)", () => {
  function transcriptOf(root) {
    return join(tmpdir(), "opencode-isabelle-guard-" + encodeURIComponent(root) + ".jsonl");
  }
  function readTranscript(root) {
    const p = transcriptOf(root);
    if (!existsSync(p)) return [];
    return readFileSync(p, "utf8")
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => JSON.parse(l).message.content[0]);
  }
  // No hooks: exercise only the logging side. guards.json present but empty.
  async function pluginFor(root) {
    const m = await loadModule(root);
    return m.IsabelleGuards({ worktree: root });
  }

  test("tool.execute.after logs a tool_result paired to its call by id", async () => {
    const root = makeWorktree({ config: { hooks: [] } });
    const plugin = await pluginFor(root);
    await plugin["tool.execute.before"](
      { tool: "iq-dev_repl_sledgehammer", callID: "call_1" },
      { args: { goal: "x = x" } },
    );
    await plugin["tool.execute.after"](
      { tool: "iq-dev_repl_sledgehammer", callID: "call_1" },
      { output: "Try this: by (simp add: foo)" },
    );

    const blocks = readTranscript(root);
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({ type: "tool_use", id: "call_1", name: "iq-dev_repl_sledgehammer" });
    expect(blocks[1]).toMatchObject({ type: "tool_result", tool_use_id: "call_1" });
    // The result must FOLLOW the call (order the guard requires) and carry the method.
    expect(blocks[1].content).toContain("simp add: foo");
  });

  test("a null/missing result output is logged as an empty string, not dropped", async () => {
    const root = makeWorktree({ config: { hooks: [] } });
    const plugin = await pluginFor(root);
    await plugin["tool.execute.after"]({ tool: "bash", callID: "call_9" }, { output: undefined });

    const blocks = readTranscript(root);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toEqual({ type: "tool_result", tool_use_id: "call_9", content: "" });
  });

  test('a "/" worktree sentinel uses the project directory as the transcript key', async () => {
    const root = makeWorktree({ config: { hooks: [] } });
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: "/", directory: root });
    await plugin["tool.execute.before"](
      { tool: "read", callID: "non_git_call" },
      { args: { filePath: "Probe.thy" } },
    );

    expect(readTranscript(root)).toContainEqual(
      expect.objectContaining({ type: "tool_use", id: "non_git_call", name: "read" }),
    );
  });

  test("a denied write is not logged and cannot poison a corrected retry", async () => {
    const scripts = {
      ...REAL_NO_APPLY_SCRIPTS,
      "no_guessed_proofs.py": readFileSync(join(import.meta.dir, "no_guessed_proofs.py"), "utf8"),
      "Hook_Searchable_Methods.thy": readFileSync(
        join(import.meta.dir, "Hook_Searchable_Methods.thy"), "utf8",
      ),
    };
    const root = makeWorktree({
      hooks: scripts,
      config: {
        interpreter: INTERP,
        hooks: [{
          matcher: "Write",
          script: "no_guessed_proofs.py",
          args: ["--searchable", "auto", "--found-via", "sledgehammer"],
        }],
      },
    });
    const m = await loadModule(root);
    const plugin = await m.IsabelleGuards({ worktree: root });

    await plugin["tool.execute.before"](
      { tool: "sledgehammer", callID: "search" }, { args: { goal: "True" } },
    );
    await plugin["tool.execute.after"](
      { tool: "sledgehammer", callID: "search" }, { output: "Try this: by auto" },
    );
    await expect(plugin["tool.execute.before"](
      { tool: "write", callID: "denied" },
      { args: { filePath: "Foo.thy", content: "lemma a: True by auto\nlemma b: True by auto" } },
    )).rejects.toThrow(/BLOCKED write/);
    expect(readTranscript(root).some((block) => block.id === "denied")).toBe(false);

    await expect(plugin["tool.execute.before"](
      { tool: "write", callID: "corrected" },
      { args: { filePath: "Foo.thy", content: "lemma a: True by auto" } },
    )).resolves.toBeUndefined();
    expect(readTranscript(root)).toContainEqual(
      expect.objectContaining({ type: "tool_use", id: "corrected", name: "write" }),
    );
  });
});
