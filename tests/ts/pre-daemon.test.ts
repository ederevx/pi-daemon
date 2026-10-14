/**
 * pi-daemon — pre-daemon.ts onboarding tool tests.
 * PreDaemonTool must register the catalog tool, gate its own tools — the
 * offload extension's bash included, but only while offloading is enabled
 * — until the catalog is read, and reset per session. It defines no
 * custom renderer, so pi's native collapsed rendering applies.
 */

import { test, assert, assertEq } from "./harness.ts";
import { PreDaemonTool, default as factory } from "../../pi/extensions/pre-daemon.ts";

type Handler = (event: any, ctx?: any) => any;

class MockPi {
  readonly tools = new Map<string, any>();
  readonly handlers = new Map<string, Handler[]>();
  /** Counts any attempt to run a command or send a message. */
  contacted = 0;

  on(name: string, handler: Handler): void {
    const list = this.handlers.get(name) ?? [];
    list.push(handler);
    this.handlers.set(name, list);
  }

  registerTool(def: any): void {
    this.tools.set(def.name, def);
  }

  exec(): never {
    this.contacted += 1;
    throw new Error("the catalog must not run anything");
  }

  sendMessage(): never {
    this.contacted += 1;
    throw new Error("the catalog must not send anything");
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

interface Mounted {
  pi: MockPi;
  policy: { enabled(): boolean };
  /** Flip the injected offload policy. */
  setOffloading(enabled: boolean): void;
}

/** Register the tool with an injected offload policy. */
function mount(offloading = true): Mounted {
  const pi = new MockPi();
  let on = offloading;
  const policy = { enabled: () => on };
  new PreDaemonTool(policy).register(pi as never);
  return {
    pi,
    policy,
    setOffloading: (enabled: boolean) => {
      on = enabled;
    },
  };
}

test("pre-daemon: registers the catalog tool, gate, and session reset", () => {
  const { pi } = mount();
  assert(pi.tools.has("pre_daemon"), "pre_daemon tool registered");
  assertEq(pi.tools.get("pre_daemon").annotations.readOnlyHint, true);
  assertEq(pi.tools.get("pre_daemon").renderResult, undefined, "no custom result renderer");
  assertEq(pi.tools.get("pre_daemon").renderCall, undefined, "no custom call renderer");
  assertEq(pi.handlers.get("tool_call")?.length, 1, "one gate handler");
  assertEq(pi.handlers.get("session_start")?.length, 1, "one reset handler");
  // The package entry point wires the same surface.
  const wired = new MockPi();
  factory(wired as never);
  assert(wired.tools.has("pre_daemon"), "the factory registers the tool too");
});

test("pre-daemon: gate blocks its own tools, bash included, until the catalog is read", async () => {
  const { pi } = mount();
  const gate = pi.gate();
  for (const name of ["bash", "daemon_tasks", "daemon_gc_reap", "transcript_read"]) {
    const result = await gate({ toolName: name });
    assertEq(result?.block, true, `${name} blocked before pre_daemon`);
  }
  const bash = await gate({ toolName: "bash" });
  assert(
    bash.reason.includes("not run locally or submitted to the daemon"),
    "bash's reason rules out both execution paths",
  );
  assert(bash.reason.includes("Call pre_daemon first"), "bash's reason names the way in");
});

test("pre-daemon: gate never touches other tools", async () => {
  const { pi } = mount();
  const gate = pi.gate();
  for (const name of ["read", "grep", "ls", "write", "team_ls", "pre_memory"]) {
    assertEq(await gate({ toolName: name }), undefined, `${name} untouched`);
  }
  assertEq(await gate({ toolName: "pre_daemon" }), undefined, "the way in is never gated");
});

test("pre-daemon: a disabled offload leaves bash ungated and unacknowledged", async () => {
  const mounted = mount(false);
  const gate = mounted.pi.gate();
  assertEq(await gate({ toolName: "bash" }), undefined, "the local shell owes nothing");
  assertEq(
    (await gate({ toolName: "daemon_tasks" }))?.block,
    true,
    "the daemon tools still need the catalog",
  );
  mounted.setOffloading(true);
  assertEq(
    (await gate({ toolName: "bash" }))?.block,
    true,
    "passing disabled bash did not acknowledge the session",
  );
});

test("pre-daemon: reading the catalog unlocks every gated tool", async () => {
  const { pi } = mount();
  const result = await pi.runTool("pre_daemon");
  assert(result.content[0].text.includes("pre_daemon"), "catalog text");
  assertEq(pi.contacted, 0, "the catalog touches no command or message surface");
  assertEq(await pi.gate()({ toolName: "daemon_tasks" }), undefined, "daemon tools unlocked");
  assertEq(await pi.gate()({ toolName: "bash" }), undefined, "bash unlocked");
});

test("pre-daemon: the gate acknowledges a direct call and session_start resets it", async () => {
  const { pi } = mount();
  await pi.sessionStart()({}, { hasUI: false });
  assertEq((await pi.gate()({ toolName: "bash" }))?.block, true, "blocked after a reset");
  assertEq(await pi.gate()({ toolName: "pre_daemon" }), undefined, "ack via gate");
  assertEq(await pi.gate()({ toolName: "bash" }), undefined, "unlocked again");
  await pi.sessionStart()({}, { hasUI: false });
  assertEq(
    (await pi.gate()({ toolName: "daemon_tasks" }))?.block,
    true,
    "a new session starts closed",
  );
});

test("pre-daemon: catalog states the tools, the conditional prerequisite, and the fallback", () => {
  const catalog = new PreDaemonTool().catalog();
  for (const name of ["bash", "daemon_tasks", "daemon_gc_reap", "transcript_read"]) {
    assert(catalog.includes(name), `catalog names ${name}`);
  }
  assert(catalog.includes("Conventions:"), "catalog has conventions");
  assert(catalog.includes("Features:"), "catalog has a feature summary");
  assert(
    catalog.includes("before bash when offloading is enabled"),
    "the prerequisite is stated for the offloaded shell",
  );
  assert(
    catalog.includes("When offloading is disabled, bash runs locally"),
    "the disabled-offload exception is stated",
  );
  assert(
    catalog.includes("after the acknowledgement, while offloading is enabled"),
    "the unreachable fallback is scoped to an acknowledged session",
  );
});
