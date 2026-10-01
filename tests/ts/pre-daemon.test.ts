/**
 * pi-daemon — pre-daemon.ts onboarding tool tests.
 * PreDaemonTool must register the catalog tool, gate only this
 * extension's tools until the catalog is read, and reset per session.
 * It defines no custom renderer, so pi's native collapsed rendering
 * applies.
 */

import { test, assert, assertEq } from "./harness.ts";
import { PreDaemonTool, default as factory } from "../../pi/extensions/pre-daemon.ts";

type Handler = (event: any, ctx?: any) => any;

class MockPi {
  readonly tools = new Map<string, any>();
  readonly handlers = new Map<string, Handler[]>();

  on(name: string, handler: Handler): void {
    const list = this.handlers.get(name) ?? [];
    list.push(handler);
    this.handlers.set(name, list);
  }

  registerTool(def: any): void {
    this.tools.set(def.name, def);
  }

  gate(): Handler {
    return this.handlers.get("tool_call")![0];
  }

  sessionStart(): Handler {
    return this.handlers.get("session_start")![0];
  }

  async runTool<T = { content: Array<{ text: string }>; details?: unknown }>(
    name: string,
  ): Promise<T> {
    const tool = this.tools.get(name);
    assert(tool?.execute, `${name} has execute`);
    return tool.execute("call1", {}, undefined, undefined, {});
  }
}

test("pre-daemon: registers the catalog tool, gate, and session reset", () => {
  const pi = new MockPi();
  factory(pi as never);
  assert(pi.tools.has("pre_daemon"), "pre_daemon tool registered");
  assertEq(pi.tools.get("pre_daemon").annotations.readOnlyHint, true);
  assertEq(pi.tools.get("pre_daemon").renderResult, undefined, "no custom result renderer");
  assertEq(pi.tools.get("pre_daemon").renderCall, undefined, "no custom call renderer");
  assertEq(pi.handlers.get("tool_call")?.length, 1, "one gate handler");
  assertEq(pi.handlers.get("session_start")?.length, 1, "one reset handler");
});

test("pre-daemon: gate blocks only this extension's tools", async () => {
  const pi = new MockPi();
  factory(pi as never);
  const gate = pi.gate();
  for (const name of ["daemon_tasks", "daemon_gc_reap"]) {
    const result = await gate({ toolName: name });
    assertEq(result?.block, true, `${name} blocked before pre_daemon`);
  }
  for (const name of ["read", "grep", "bash", "team_ls"]) {
    assertEq(await gate({ toolName: name }), undefined, `${name} untouched`);
  }
});

test("pre-daemon: reading the catalog unlocks the tools", async () => {
  const pi = new MockPi();
  factory(pi as never);
  const result = await pi.runTool("pre_daemon");
  assert(result.content[0].text.includes("pre_daemon"), "catalog text");
  assertEq(await pi.gate()({ toolName: "daemon_tasks" }), undefined, "unlocked");
  // The gate itself also acknowledges a direct pre_daemon call.
  await pi.sessionStart()({}, { hasUI: false });
  assertEq((await pi.gate()({ toolName: "daemon_tasks" }))?.block, true, "reset");
  assertEq(await pi.gate()({ toolName: "pre_daemon" }), undefined, "ack via gate");
  assertEq(await pi.gate()({ toolName: "daemon_tasks" }), undefined, "unlocked again");
});

test("pre-daemon: catalog lists every registered tool and the conventions", () => {
  const catalog = new PreDaemonTool().catalog();
  for (const name of ["bash", "daemon_tasks", "daemon_gc_reap"]) {
    assert(catalog.includes(name), `catalog names ${name}`);
  }
  assert(catalog.includes("Conventions:"), "catalog has conventions");
  assert(catalog.includes("Features:"), "catalog has a feature summary");
});
