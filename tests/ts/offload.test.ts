/**
 * pi-daemon — offload.ts extension tests.
 * The ticket client, session-side task tracking, and the bash override
 * must own their flows against a scripted pi-rc: submit -> armed
 * delivery, status/result/list/cancel/remove/reset, the transparent
 * local fallback when the daemon is unreachable, and the PI_OFFLOAD=off
 * escape hatch. No module-global mutable state anywhere in the wiring.
 */

import { test, assert, assertEq, assertMatches, scratchDir, waitFor, withEnv } from "./harness.ts";
import { initTheme } from "@earendil-works/pi-coding-agent";
import { sessionKeyOf, TicketClient, default as factory } from "../../pi/extensions/offload.ts";
import { ProcessRunner } from "../../pi/extensions/daemon.ts";

interface ExeResult {
  code: number;
  stdout: string;
  stderr: string;
  killed: boolean;
}

function ok(stdout: string): ExeResult {
  return { code: 0, stdout, stderr: "", killed: false };
}

function ticketRecord(status: string, id = "t-1"): string {
  return JSON.stringify({
    id,
    session: "abc",
    cwd: "/x",
    command: "echo hi",
    kind: "shell",
    status,
    created: 1,
    started: 1,
    finished: status === "running" ? null : 2,
    exit: status === "running" ? null : 0,
    term: null,
    truncated: false,
    error: null,
  });
}

class FakePi {
  readonly sent: Array<{ message: unknown; options: unknown }> = [];
  readonly messageRenderers = new Map<string, unknown>();
  readonly tools = new Map<string, { name: string; execute?: unknown }>();
  readonly commands = new Map<string, unknown>();
  readonly execCalls: Array<{ file: string; args: string[] }> = [];
  /** Incremental ticket logs keyed by id, for offset reads. */
  readonly logs = new Map<string, string>();
  private readonly wait300 = new Map<string, number>();

  constructor(private readonly daemonDown = false) {}

  async exec(file: string, args: string[]): Promise<ExeResult> {
    // The Windows launch shape runs pi-rc through the Python
    // interpreter (the script path leads the args); normalize to the
    // POSIX shape - the script is the file, the args follow - so the
    // dispatch and recorded calls match on every platform.
    const python = /python/i.test(file);
    const cmd = (python ? args[1] : args[0]) ?? "";
    const rest = python ? args.slice(2) : args.slice(1);
    // Record the normalized pi-rc call (subcommand first, then its
    // arguments) so assertions can name the verb and its flags alike.
    this.execCalls.push({ file: python ? args[0] : file, args: [cmd, ...rest] });
    if (this.daemonDown) return { code: 4, stdout: "", stderr: "down", killed: false };
    if (cmd === "ticket-submit") return ok("ticket t-1\n");
    if (cmd === "ticket-wait") {
      const id = rest[0];
      const timeout = Number(rest[1] ?? 0);
      if (timeout > 0) {
        const n = (this.wait300.get(id) ?? 0) + 1;
        this.wait300.set(id, n);
        return ok(ticketRecord(n >= 2 ? "done" : "running") + "\n");
      }
      const settled = (this.wait300.get(id) ?? 0) >= 2;
      return ok(ticketRecord(settled ? "done" : "running") + "\n");
    }
    if (cmd === "ticket-output") {
      if (args.includes("--json")) {
        return ok(JSON.stringify({ ok: true, id: rest[0], status: "done", exit: 0, output: "out-json" }));
      }
      // Offset mode mirrors pi-rc's byte-faithful wire format: the
      // base64 payload from the offset, empty when the log has no
      // new bytes.
      const offset = Number(rest[1]);
      if (rest[1] !== undefined && !Number.isNaN(offset)) {
        const log = this.logs.get(rest[0]) ?? "";
        return ok(offset < log.length
          ? Buffer.from(log.slice(offset), "utf8").toString("base64")
          : "");
      }
      return ok("");
    }
    if (cmd === "ticket-list") return ok(ticketRecord("done") + "\n");
    if (cmd === "ticket-cancel") return ok("");
    if (cmd === "ticket-remove") return ok("");
    if (cmd === "tickets-reset") return ok("pi-rc: reset: cancelled 0, removed 0\n");
    return ok("");
  }

  sendMessage(message: unknown, options: unknown): Promise<void> {
    this.sent.push({ message, options });
    return Promise.resolve();
  }
  registerTool(tool: { name: string; execute?: unknown }): void {
    this.tools.set(tool.name, tool);
  }
  registerCommand(name: string, def: unknown): void {
    this.commands.set(name, def);
  }
  registerMessageRenderer(name: string, renderer: unknown): void {
    this.messageRenderers.set(name, renderer);
  }
  on(_name: string, _handler: unknown): void {}
}

function runTool<T= { content?: Array<{ type: string; text: string }>; details?: unknown }>(
  tool: { execute?: unknown } | undefined,
  params: unknown,
): Promise<T> {
  return runToolIO<T>(tool, params);
}

/** runTool variant that injects the tool-call IO surface: the abort
 *  signal, onUpdate capture, and a context with its own flow helpers,
 *  so the interruptibility and steering paths are exercisable. */
function runToolIO<T= { content?: Array<{ type: string; text: string }>; details?: unknown }>(
  tool: { execute?: unknown } | undefined,
  params: unknown,
  io: {
    signal?: AbortSignal;
    onUpdate?: (update: unknown) => void;
    ctx?: Record<string, unknown>;
  } = {},
): Promise<T> {
  const execute = tool?.execute as ((_id: string, p: unknown, s: unknown, o: unknown, c: unknown) => Promise<T>) | undefined;
  assert(typeof execute === "function", "tool has execute");
  return execute!("call1", params, io.signal, io.onUpdate, {
    sessionManager: {
      getSessionId: () => "test-session",
      getSessionFile: () => "/x/abc.jsonl",
    },
    cwd: scratchDir(),
    mode: "cli",
    hasPendingMessages: () => false,
    ...io.ctx,
  });
}

function mount(pi: FakePi) {
  factory(pi as never);
  return pi;
}

test("offload: factory registers the expected surface", () => {
  const pi = mount(new FakePi());
  assert(pi.tools.has("bash"), "bash tool registered");
  assert(pi.tools.has("daemon_tasks"), "daemon_tasks tool registered");
  assert(pi.commands.has("daemon-tasks"), "/daemon-tasks command registered");
  assert(!pi.tools.has("daemon_subagent_list"), "subagent tool removed");
  assert(!pi.tools.has("daemon_subagent_wait"), "subagent wait removed");
  const guidelines = (pi.tools.get("bash") as { promptGuidelines?: string[] } | undefined)
    ?.promptGuidelines ?? [];
  assert(guidelines.some((g) => /keep calling tools instead of sleeping/.test(g)),
    "the bash guideline tells agents not to sleep for a ticket");
});

test("offload: the completion card is collapsed, outcome-colored, and expandable", () => {
  initTheme("dark");
  const pi = mount(new FakePi());
  const render = pi.messageRenderers.get("daemon-task") as
    | ((m: unknown, o: unknown, t: unknown) => { render(width: number): string[] } | undefined)
    | undefined;
  assert(typeof render === "function", "daemon-task message renderer registered");
  const roles: string[] = [];
  const theme = {
    fg: (role: string, text: string) => { roles.push(role); return text; },
    bg: (_role: string, text: string) => text,
    bold: (text: string) => text,
  };
  const ticket = {
    id: "t-1", session: "abc", cwd: "/x", command: "echo hi",
    status: "done", created: 1, started: 1, finished: 2, exit: 0,
    term: null, truncated: false, error: null,
  };
  const message = {
    customType: "daemon-task",
    content: "Background task finished: echo hi\nticket t-1 done exit 0\nhello-out",
    display: true,
    details: { ticket },
  };
  const collapsed = render!(message, { expanded: false, outputPad: 1 }, theme)!
    .render(80).join("\n");
  assertMatches(collapsed, /t-1/);
  assertMatches(collapsed, /done \(exit 0\)/);
  assert(!collapsed.includes("hello-out"), "output stays hidden while collapsed");
  assert(roles.includes("success"), "a clean exit is success-colored");

  roles.length = 0;
  const expanded = render!(message, { expanded: true, outputPad: 1 }, theme)!
    .render(80).join("\n");
  assertMatches(expanded, /hello-out/, "expanding reveals the result");
  assert(roles.includes("success"), "the expanded card keeps the outcome color");

  roles.length = 0;
  render!(
    { ...message, details: { ticket: { ...ticket, status: "failed", exit: 3 } } },
    { expanded: false, outputPad: 1 },
    theme,
  )!.render(80);
  assert(roles.includes("error"), "a nonzero exit is error-colored");
});

test("offload: submit queues and the armed watcher delivers on completion", async () => {
  const pi = mount(new FakePi());
  const tools = pi.tools.get("daemon_tasks");
  const result = await runTool(tools, { action: "submit", command: "echo hi" });
  const text = result.content?.[0]?.text ?? "";
  assertMatches(text, /ticket t-1 queued/);
  // The armed delivery watcher polls to completion and steers the result.
  await waitFor(() =>
    pi.sent.some((s) => {
      const o = s.options as { deliverAs?: string };
      return o?.deliverAs === "steer";
    }),
    "completion steer delivered",
  );
  const sent = pi.sent.find((s) => (s.options as { deliverAs?: string }).deliverAs === "steer");
  const content = (sent!.message as { content?: string }).content ?? "";
  assertMatches(content, /Background task finished: echo hi/);
  assertMatches(content, /ticket t-1 done/);
});

test("offload: daemon_tasks status/result/list/cancel/remove/reset", async () => {
  const pi = mount(new FakePi());
  const tools = pi.tools.get("daemon_tasks");

  const status = await runTool<{ content: Array<{ text: string }> }>(tools, { action: "status", id: "t-1" });
  assertMatches(status.content[0].text, /ticket t-1/);

  const result = await runTool<{ content: Array<{ text: string }> }>(tools, { action: "result", id: "t-1", wait: 1 });
  assertMatches(result.content[0].text, /still running|ticket t-1/);

  const listed = await runTool<{ content: Array<{ text: string }> }>(tools, { action: "list" });
  assertMatches(listed.content[0].text, /t-1/);

  const cancelled = await runTool<{ content: Array<{ text: string }> }>(tools, { action: "cancel", id: "t-1" });
  assertMatches(cancelled.content[0].text, /t-1/);

  const removed = await runTool(tools, { action: "remove", id: "t-1" });
  assertEq(removed.content?.[0]?.text, "ticket t-1 removed");

  await assertReject(() => runTool(tools, { action: "reset" }),
    "the global reset is not an agent action");
  // no subagent actions accepted
  await assertReject(() => runTool(tools, { action: "submit", command: "" }), "submit without command rejected");
  // a ticket whose point is to wait is refused before the daemon sees it
  await assertReject(() => runTool(tools, { action: "submit", command: "sleep 30" }),
    "a wait-only submit is refused");
  await assertReject(() => runTool(tools, { action: "submit", command: "while pgrep x; do sleep 1; done" }),
    "a polling-loop submit is refused");
  const okSubmit = await runTool<{ content: Array<{ text: string }> }>(tools,
    { action: "submit", command: "grep -rn sleep README.md" });
  assertMatches(okSubmit.content[0].text, /queued/);
  assert(!pi.execCalls.some((c) => c.args[0] === "ticket-submit" && c.args.join(" ").includes("sleep 30")),
    "the refused wait never submitted");
});

test("offload: result wait completes by active polling with live status", async () => {
  const pi = mount(new FakePi());
  const tools = pi.tools.get("daemon_tasks");
  const updates: Array<{ content?: Array<{ text?: string }> }> = [];
  const result = await runToolIO<{ content: Array<{ text: string }> }>(
    tools,
    { action: "result", id: "t-1", wait: 5 },
    { onUpdate: (u) => updates.push(u as { content: Array<{ text?: string }> }) },
  );
  // The poll loop returned the finished ticket instead of the bound.
  assertMatches(result.content[0].text, /ticket t-1 done/);
  assertMatches(result.content[0].text, /exit 0/);
  // Live elapsed status was emitted while the ticket ran.
  assert(updates.length > 0, "live elapsed status emitted");
  assertMatches(updates[0].content?.[0]?.text ?? "", /waiting for ticket t-1/);
});

test("offload: a pre-aborted signal releases the result wait without blocking rounds", async () => {
  const pi = mount(new FakePi());
  const tools = pi.tools.get("daemon_tasks");
  const controller = new AbortController();
  controller.abort();
  const result = await runToolIO<{ content: Array<{ text: string }> }>(
    tools,
    { action: "result", id: "t-1", wait: 30 },
    { signal: controller.signal },
  );
  assertMatches(result.content[0].text, /still running/);
  // The wait must not hold a blocking pi-rc round trip open: only the
  // immediate status probe (two args) is allowed.
  assert(!pi.execCalls.some((c) => c.args[0] === "ticket-wait" && c.args.length > 2),
    "no blocking ticket-wait round trips after abort");
});

test("offload: a queued user message yields the result wait", async () => {
  const pi = mount(new FakePi());
  const tools = pi.tools.get("daemon_tasks");
  const result = await runToolIO<{ content: Array<{ text: string }> }>(
    tools,
    { action: "result", id: "t-1", wait: 30 },
    { ctx: { hasPendingMessages: () => true } },
  );
  assertMatches(result.content[0].text, /still running/);
  assert(!pi.execCalls.some((c) => c.args[0] === "ticket-wait" && c.args.length > 2),
    "no blocking round trips while a message is queued");
});

test("offload: watch streams incremental output and abort releases the wait", async () => {
  const pi = mount(new FakePi());
  pi.logs.set("t-1", "chunk-one-chunk-two");
  const tools = pi.tools.get("daemon_tasks");
  const updates: string[] = [];
  const result = await runToolIO<{ content: Array<{ text: string }> }>(
    tools,
    { action: "watch", id: "t-1" },
    { onUpdate: (u) => updates.push((u as { content: Array<{ text: string }> }).content[0].text) },
  );
  assertMatches(result.content[0].text, /ticket t-1 done/);
  assertMatches(result.content[0].text, /chunk-one-chunk-two/);
  assert(updates.length > 0, "watch streamed a tail update");
  assertMatches(updates[updates.length - 1], /chunk-one-chunk-two/);
});

test("offload: watch abort releases the wait with the ticket still running", async () => {
  const pi = mount(new FakePi());
  const tools = pi.tools.get("daemon_tasks");
  const controller = new AbortController();
  controller.abort();
  const result = await runToolIO<{ content: Array<{ text: string }> }>(
    tools,
    { action: "watch", id: "t-1" },
    { signal: controller.signal },
  );
  assertMatches(result.content[0].text, /released early/);
  assert(!pi.execCalls.some((c) => c.args[0] === "ticket-wait" && c.args.length > 2),
    "no blocking round trips after abort");
});

test("offload: bash defers with a success claim and the watcher steers the result", async () => {
  const pi = mount(new FakePi());
  pi.logs.set("t-1", "deferred-output");
  const bashTool = pi.tools.get("bash") as { execute: unknown };
  const execute = bashTool.execute as (
    _id: string,
    p: { command: string; cwd?: string; timeout?: number },
    _s: unknown,
    _o: unknown,
    _c: unknown,
  ) => Promise<{
    content: Array<{ type: string; text: string }>;
    structuredContent?: { exit_code?: number };
    isError?: boolean;
  }>;
  const out = await execute("call1", { command: "echo hi", cwd: scratchDir(), timeout: 7 }, undefined, undefined, {
    sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
    cwd: scratchDir(),
    mode: "cli",
  });
  // The claim is a success, never the stock tool's null-exit hand-off
  // error: the daemon owns the ticket and the agent is not held.
  assertEq(out.structuredContent?.exit_code, 0, "claim returns success");
  assertEq(out.isError, undefined, "not an error result");
  // The claim advertises the ticket and both continuations - keep
  // calling tools, or block explicitly - so an agent that would sleep
  // knows the result is steered in anyway.
  const claim = out.content?.[0]?.text ?? "";
  assertMatches(claim, /ticket t-1 taken by the daemon/);
  assertMatches(claim, /daemon_tasks result t-1 wait=<seconds>/);
  const submit = pi.execCalls.find((c) => c.args.includes("--timeout"));
  assert(submit !== undefined, "ticket-submit carries --timeout");
  const idx = submit!.args.indexOf("--timeout");
  assertEq(submit!.args[idx + 1], "7", "daemon timeout reaches the daemon");
  // The daemon_tasks wait kicks in on its own and steers the deferred
  // result in before the next model call, so the agent never bridges the
  // deferral or responds in between.
  await waitFor(() => pi.sent.length > 0);
  const steer = pi.sent[0];
  assertMatches(JSON.stringify(steer.message), /deferred-output/);
  assertEq((steer.options as { deliverAs?: string }).deliverAs, "steer",
    "the deferred result arrives as a steer");
});

test("offload: an abort leaves the daemon-owned ticket and its watcher running", async () => {
  const pi = mount(new FakePi());
  pi.logs.set("t-1", "late-output");
  const bashTool = pi.tools.get("bash") as { execute: unknown };
  const execute = bashTool.execute as (
    _id: string,
    p: { command: string; cwd?: string },
    s: unknown,
    _o: unknown,
    _c: unknown,
  ) => Promise<{ structuredContent?: { exit_code?: number } }>;
  const controller = new AbortController();
  controller.abort();
  const out = await execute("call1", { command: "echo long-running", cwd: scratchDir() }, controller.signal, undefined, {
    sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
    cwd: scratchDir(),
    mode: "cli",
  });
  assertEq(out.structuredContent?.exit_code, 0, "the claim still succeeds");
  assert(!pi.execCalls.some((c) => c.args[0] === "ticket-cancel"),
    "the daemon ticket is not cancelled by the call");
  await waitFor(() => pi.sent.length > 0);
  assertMatches(JSON.stringify(pi.sent[0].message), /late-output/);
});

test("offload: a sleep command is refused before the daemon sees it", async () => {
  const pi = mount(new FakePi());
  const bashTool = pi.tools.get("bash") as { execute: unknown };
  const execute = bashTool.execute as (
    _id: string,
    p: { command: string; cwd?: string },
    _s: unknown,
    _o: unknown,
    _c: unknown,
  ) => Promise<{
    content: Array<{ type: string; text: string }>;
    structuredContent?: { exit_code?: number };
    isError?: boolean;
  }>;
  const out = await execute("call1", { command: "sleep 30", cwd: scratchDir() }, undefined, undefined, {
    sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
    cwd: scratchDir(),
    mode: "cli",
  });
  assertEq(out.structuredContent?.exit_code, 1, "the refusal is a failure, not a claim");
  assertEq(out.isError, true, "rendered as an error");
  const text = out.content?.[0]?.text ?? "";
  assertMatches(text, /refused `sleep`/);
  assertMatches(text, /daemon_tasks result <id> wait=<seconds>/);
  assertEq(pi.execCalls.length, 0, "the daemon never sees the sleep");
  assertEq(pi.sent.length, 0, "no ticket and no delivery watcher");
});

test("offload: sleep in a polling loop is refused, a sleep word is not", async () => {
  const pi = mount(new FakePi());
  const bashTool = pi.tools.get("bash") as { execute: unknown };
  const execute = bashTool.execute as (
    _id: string,
    p: { command: string; cwd?: string; timeout?: number },
    _s: unknown,
    _o: unknown,
    _c: unknown,
  ) => Promise<{ isError?: boolean }>;
  const ctx = {
    sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
    cwd: scratchDir(),
    mode: "cli",
  };
  const loop = await execute("c1", { command: "while pgrep x; do sleep 1; done", cwd: scratchDir() }, undefined, undefined, ctx);
  assertEq(loop.isError, true, "a polling loop is a sleep call");
  assertEq(pi.execCalls.length, 0, "the loop never reached the daemon");
  await execute("c2", { command: "grep -rn sleep README.md", cwd: scratchDir(), timeout: 5 }, undefined, undefined, ctx);
  assert(pi.execCalls.some((c) => c.args[0] === "ticket-submit"),
    "a sleep word in an argument is not a wait and still reaches the daemon");
});

test("offload: PI_OFFLOAD=off keeps sleep in the local shell", async () => {
  await withEnv({ PI_OFFLOAD: "off" }, async () => {
    const pi = mount(new FakePi());
    const bashTool = pi.tools.get("bash") as { execute: unknown };
    const execute = bashTool.execute as (
      _id: string,
      p: { command: string; cwd?: string },
      _s: unknown,
      _o: unknown,
      _c: unknown,
    ) => Promise<{ content: Array<{ type: string; text: string }>; isError?: boolean }>;
    const out = await execute("c1", { command: "sleep 0", cwd: scratchDir() }, undefined, undefined, {
      sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
      cwd: scratchDir(),
      mode: "cli",
    });
    const text = out.content.map((b) => b.text).join("\n");
    assert(!/refused/.test(text), "the guard is scoped to the offload path");
    assertEq(out.isError, undefined, "a local sleep is not an error");
    assertEq(pi.execCalls.length, 0, "still no daemon traffic when offloading is off");
  });
});

test("offload: bash tool falls back to local execution when the daemon is down", async () => {
  const pi = mount(new FakePi(true));
  const bashTool = pi.tools.get("bash") as { execute: unknown };
  const execute = bashTool.execute as (
    _id: string,
    p: { command: string; cwd?: string },
    _s: unknown,
    _o: unknown,
    _c: unknown,
  ) => Promise<{ content: Array<{ type: string; text: string }> }>;
  const out = await execute("call1", { command: "printf fallback-works", cwd: scratchDir() }, undefined, undefined, {
    sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
    cwd: scratchDir(),
    mode: "cli",
  });
  const text = out.content.map((b) => (b as { text: string }).text).join("\n");
  assertMatches(text, /fallback-works/);
  assert(pi.execCalls.length > 0, "pi-rc was attempted first");
  // The file argument is the seam that broke Windows: on POSIX it is the
  // shebang'd pi-rc, on Windows it must be the Python interpreter (the
  // Windows branch is covered directly by the daemon.test ProcessRunner
  // test). The old client ignored `file` and hid the ENOENT.
  if (process.platform !== "win32") {
    assert(/(^|[\\/])pi-rc$/.test(pi.execCalls[0].file),
      `POSIX must exec pi-rc directly, got ${pi.execCalls[0].file}`);
  }
});

test("offload: PI_OFFLOAD=off runs bash locally without any daemon traffic", async () => {
  await withEnv({ PI_OFFLOAD: "off" }, async () => {
    const pi = mount(new FakePi());
    const bashTool = pi.tools.get("bash") as { execute: unknown };
    const execute = bashTool.execute as (
      _id: string,
      p: { command: string; cwd?: string },
      _s: unknown,
      _o: unknown,
      _c: unknown,
    ) => Promise<{ content: Array<{ type: string; text: string }> }>;
    const out = await execute("c1", { command: "printf direct-ok", cwd: scratchDir() }, undefined, undefined, {
      sessionManager: { getSessionId: () => "test-session", getSessionFile: () => "/x/abc.jsonl" },
      cwd: scratchDir(),
      mode: "cli",
    });
    const text = out.content.map((b) => (b as { text: string }).text).join("\n");
    assertMatches(text, /direct-ok/);
    assertEq(pi.execCalls.length, 0, "no pi-rc calls when offloading is off");
  });
});

test("offload: TicketClient routes pi-rc through the win32 interpreter", async () => {
  const calls: Array<{ file: string; args: string[] }> = [];
  const runner = new ProcessRunner(
    async (file, args) => {
      calls.push({ file, args });
      return { code: 0, stdout: "", stderr: "", killed: false };
    },
    "win32",
    () => "pythonw.exe",
  );
  await new TicketClient(runner).setState("sess", "busy");
  assertEq(calls.length, 1);
  assertEq(calls[0].file, "pythonw.exe");
  assert(/(^|[\\/])pi-rc$/.test(calls[0].args[0]),
    `interpreter must receive the pi-rc script, got ${calls[0].args[0]}`);
});

test("offload: sessionKeyOf derives the stable owning key", () => {
  withEnv({ PI_HOSTED_SESSION: "pi-sess1", PI_SESSION_FILE: undefined }, () => {
    assertEq(sessionKeyOf("/x/a.jsonl"), "sess1");
  });
  withEnv({ PI_HOSTED_SESSION: undefined, PI_SESSION_FILE: undefined }, () => {
    assertEq(sessionKeyOf("/dir/conv-name.jsonl"), "conv-name");
  });
  withEnv({ PI_HOSTED_SESSION: undefined, PI_SESSION_FILE: undefined }, () => {
    assertEq(sessionKeyOf(null), "standalone");
  });
});

test("offload: /daemon-tasks scopes to the command context's session file", async () => {
  // A non-hosted session has no PI_SESSION_FILE env, so the dock must
  // take its identity from the command context; otherwise it looks for
  // the session's tickets under "standalone" and never finds them.
  await withEnv({ PI_HOSTED_SESSION: undefined, PI_SESSION_FILE: undefined }, async () => {
    const pi = mount(new FakePi());
    const def = pi.commands.get("daemon-tasks") as {
      handler: (args: string, ctx: unknown) => Promise<void>;
    };
    assert(typeof def?.handler === "function", "daemon-tasks handler present");
    initTheme("dark");
    let dock: { sessionKey?: string } | undefined;
    const ui = {
      custom: (
        factory: (tui: unknown, theme: unknown, kb: unknown, done: (r: null) => void) => unknown,
      ) => {
        dock = factory({ requestRender() {} }, null, {}, () => {}) as typeof dock;
        return Promise.resolve();
      },
      notify() {},
    };
    await def.handler("", {
      mode: "tui",
      ui,
      sessionManager: { getSessionFile: () => "/x/abc.jsonl" },
    });
    assert(dock !== undefined, "dock mounted");
    assertEq(dock!.sessionKey, "abc");
  });
});

/** runTool variant that expects rejection. */
async function assertReject(fn: () => Promise<unknown>, message: string): Promise<void> {
  let threw = false;
  try {
    await fn();
  } catch {
    threw = true;
  }
  assert(threw, message);
}