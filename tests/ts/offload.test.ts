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
import { sessionKeyOf, default as factory } from "../../pi/extensions/offload.ts";

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
  readonly entries: Array<{ kind: string; data: unknown }> = [];
  readonly tools = new Map<string, { name: string; execute?: unknown }>();
  readonly commands = new Map<string, unknown>();
  readonly execCalls: string[][] = [];
  private readonly wait300 = new Map<string, number>();

  constructor(private readonly daemonDown = false) {}

  async exec(_file: string, args: string[]): Promise<ExeResult> {
    this.execCalls.push(args);
    if (this.daemonDown) return { code: 4, stdout: "", stderr: "down", killed: false };
    const cmd = args[0];
    if (cmd === "ticket-submit") return ok("ticket t-1\n");
    if (cmd === "ticket-wait") {
      const id = args[1];
      const timeout = Number(args[2] ?? 0);
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
        return ok(JSON.stringify({ ok: true, id: args[1], status: "done", exit: 0, output: "out-json" }));
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
  appendEntry(kind: string, data: unknown): void {
    this.entries.push({ kind, data });
  }
  registerTool(tool: { name: string; execute?: unknown }): void {
    this.tools.set(tool.name, tool);
  }
  registerCommand(name: string, def: unknown): void {
    this.commands.set(name, def);
  }
  registerEntryRenderer(_name: string, _renderer: unknown): void {}
  on(_name: string, _handler: unknown): void {}
}

function runTool<T= { content?: Array<{ type: string; text: string }>; details?: unknown }>(
  tool: { execute?: unknown } | undefined,
  params: unknown,
): Promise<T> {
  const execute = tool?.execute as ((_id: string, p: unknown, _s: unknown, _o: unknown, _c: unknown) => Promise<T>) | undefined;
  assert(typeof execute === "function", "tool has execute");
  return execute!("call1", params, undefined, undefined, {
    sessionManager: {
      getSessionId: () => "test-session",
      getSessionFile: () => "/x/abc.jsonl",
    },
    cwd: scratchDir(),
    mode: "cli",
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

  assertMatches(
    (await runTool<{ content: Array<{ text: string }> }>(tools, { action: "reset" })).content[0].text,
    /ticketing reset/,
  );
  // no subagent actions accepted
  await assertReject(() => runTool(tools, { action: "submit", command: "" }), "submit without command rejected");
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