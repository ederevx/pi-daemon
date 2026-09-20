/**
 * pi-daemon — daemon.ts extension tests.
 * RcBackground must own the /bg lifecycle against the injected pi-rc
 * surface: announce/takeover handling, run-state publishing, hosted
 * detach, unhosted handover, and /new+/resume carrying — with zero
 * module-global state and no leaks across instances.
 */

import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import {
  RcBackground,
  windowlessCandidates,
  default as factory,
} from "../../pi/extensions/daemon.ts";

interface ExeResult {
  code: number;
  stdout: string;
  stderr: string;
  killed: boolean;
}

/** Records pi-rc invocations; scriptable per call. */
class ExecScript {
  readonly calls: string[][] = [];
  private index = 0;

  constructor(
    private readonly responses: Array<
      Partial<ExeResult> | ((args: string[]) => Partial<ExeResult>)
    >,
  ) {}

  async run(_file: string, args: string[]): Promise<ExeResult> {
    // Windows runs pi-rc through the Python interpreter, so the script
    // path leads the args there; drop it so tests assert commands
    // uniformly on every platform.
    const logical =
      args.length > 0 && /(^|[\\/])pi-rc$/.test(args[0])
        ? args.slice(1)
        : args;
    this.calls.push(logical);
    const spec = this.responses[Math.min(this.index, this.responses.length - 1)];
    this.index++;
    const r = typeof spec === "function" ? spec(logical) : spec;
    return {
      code: r.code ?? 0,
      stdout: r.stdout ?? "",
      stderr: r.stderr ?? "",
      killed: r.killed ?? false,
    };
  }
}

type TestCtx = {
  sessionManager: { getSessionFile: () => string | null };
  ui: { notify: (t: string, k?: string) => void };
  isIdle?: () => boolean;
  shutdown?: () => void;
  shutdownCalled?: boolean;
  waitForIdle?: () => Promise<void>;
};

function ctx(): TestCtx {
  const c: TestCtx = {
    sessionManager: { getSessionFile: () => "/x/conv.jsonl" },
    ui: { notify: () => {} },
    isIdle: () => true,
    waitForIdle: async () => {},
  };
  c.shutdown = () => {
    c.shutdownCalled = true;
  };
  return c;
}

/** Runs fn with a hosted-session env and awaits the body. */
async function hosted(session: string, fn: () => Promise<void>): Promise<void> {
  // Point the endpoint at a scratch path so the handover hold never
  // reaches a real daemon running on the test host.
  await withEnv(
    {
      PI_HOSTED_SESSION: session,
      PI_HOSTED: session ? "1" : "",
      HOME: "/home/x",
      XDG_RUNTIME_DIR: join(scratchDir(), "runtime"),
    },
    fn,
  );
}

test("daemon: announce records the session file and follows a busy handoff", async () => {
  await hosted("pi-test1", async () => {
    const exe = new ExecScript([{ code: 0, stdout: "handoff other\tbusy\n" }]);
    const app = new RcBackground(exe.run.bind(exe));
    const c = ctx();
    await app.announce(c);
    assertEq(exe.calls.length, 1);
    assertEq(exe.calls[0][0], "announce");
    assertEq(exe.calls[0][1], "test1");
    assertEq(exe.calls[0][2], "/x/conv.jsonl");
    assertEq(exe.calls[0][3], "--takeover");
    assertEq(c.shutdownCalled, true, "duplicate yields to the busy holder");
  });
});

test("daemon: announce warns (no shutdown) when unavailable", async () => {
  await hosted("pi-test1", async () => {
    const exe = new ExecScript([{ code: 0, stdout: "handoff other\tidle\n" }]);
    const app = new RcBackground(exe.run.bind(exe));
    const c = ctx();
    c.shutdown = undefined;
    await app.announce(c);
    assertEq(c.shutdownCalled, undefined);
    assertEq(exe.calls.length, 1);
  });
});

test("daemon: announce absorbs idle holders and skips repeats", async () => {
  await hosted("pi-test1", async () => {
    const exe = new ExecScript([{ code: 0, stdout: "took-over\tpi-idle1\n" }]);
    const app = new RcBackground(exe.run.bind(exe));
    await app.announce(ctx());
    await app.announce(ctx()); // already announced: no second exec
    assertEq(exe.calls.length, 1, "duplicate announce skipped");
  });
});

test("daemon: no-op outside hosting", async () => {
  await hosted("", async () => {
    const exe = new ExecScript([{ code: 0, stdout: "" }]);
    const app = new RcBackground(exe.run.bind(exe));
    await app.announce(ctx());
    await app.setState("busy", ctx());
    await app.detach(ctx());
    assertEq(exe.calls.length, 0, "no pi-rc calls without a hosted session");
  });
});

test("daemon: setState publishes busy/idle for the hosted session", async () => {
  await hosted("pi-test2", async () => {
    const exe = new ExecScript([{ code: 0 }, { code: 0 }]);
    const app = new RcBackground(exe.run.bind(exe));
    await app.setState("busy", ctx());
    await app.setState("idle", ctx());
    assertEq(exe.calls[0][0], "state");
    assertEq(exe.calls[0][1], "test2");
    assertEq(exe.calls[0][2], "busy");
    assertEq(exe.calls[1][2], "idle");
  });
});

test("daemon: hosted /bg detaches through the daemon bridge", async () => {
  await hosted("pi-test3", async () => {
    const exe = new ExecScript([{ code: 0, stdout: "" }]);
    const app = new RcBackground(exe.run.bind(exe));
    await app.background(ctx());
    assertEq(exe.calls[0][0], "detach");
    assertEq(exe.calls[0][1], "test3");
  });
});

test("daemon: unhosted /bg hands over and exits after settle", async () => {
  await hosted("", async () => {
    const exe = new ExecScript([
      { code: 0, stdout: "target:pi-basedir" },
      { code: 0, stdout: "hosted" },
    ]);
    const app = new RcBackground(exe.run.bind(exe));
    const c = ctx();
    await app.background(c);
    assertEq(exe.calls.length, 2);
    assertEq(exe.calls[0][0], "handover");
    assertEq(exe.calls[0][3], "--check");
    assertEq(exe.calls[1][0], "handover");
    assertEq(exe.calls[1][3], "--after-exit");
    assertEq(c.shutdownCalled, true, "graceful shutdown after handover");
  });
});

test("daemon: unhosted /bg stays up when handover is unavailable", async () => {
  await hosted("", async () => {
    const exe = new ExecScript([{ code: 4, stdout: "", stderr: "unreachable" }]);
    const app = new RcBackground(exe.run.bind(exe));
    const c = ctx();
    await app.background(c);
    assertEq(c.shutdownCalled, undefined, "never shuts down without a verdict");
  });
});

test("daemon: ephemeral sessions cannot be handed over", async () => {
  await hosted("", async () => {
    const exe = new ExecScript([]);
    const app = new RcBackground(exe.run.bind(exe));
    const c = ctx();
    c.sessionManager = { getSessionFile: () => null };
    await app.background(c);
    assertEq(exe.calls.length, 0);
  });
});

test("daemon: /new and /resume are carried, never aborted", async () => {
  await hosted("pi-test4", async () => {
    const exe = new ExecScript([{ code: 0, stdout: "" }, { code: 0, stdout: "" }]);
    const app = new RcBackground(exe.run.bind(exe));
    assertEq(
      JSON.stringify(await app.beforeSwitch({ reason: "new" }, ctx())),
      JSON.stringify({ cancel: true }),
    );
    assertEq(exe.calls[0][0], "carry");
    const c = ctx();
    c.sessionManager.getSessionFile = () => "/x/old.jsonl";
    assertEq(
      JSON.stringify(
        await app.beforeSwitch({ reason: "resume", targetSessionFile: "/x/other.jsonl" }, c),
      ),
      JSON.stringify({ cancel: true }),
    );
    assertEq(exe.calls[1][0], "resume");
    assertEq(await app.beforeSwitch({ reason: "fork" }, ctx()), undefined);
    assertEq(exe.calls.length, 2, "unrelated switches untouched");
  });
});

test("daemon: factory wires the command and events", () => {
  const pi = new MockPi();
  factory(pi as never);
  assert(pi.commands.has("bg"), "/bg registered");
  assertEq(pi.onCalls.get("session_start") ?? 0, 2, "two session_start listeners");
  assertEq(pi.onCalls.get("before_agent_start") ?? 0, 1);
  assertEq(pi.onCalls.get("agent_end") ?? 0, 1);
  assertEq(pi.onCalls.get("agent_settled") ?? 0, 1);
  assertEq(pi.onCalls.get("session_before_switch") ?? 0, 1);
});

class MockPi {
  readonly commands = new Map<string, unknown>();
  readonly onCalls = new Map<string, number>();
  readonly sessionStarters: Array<(event: any) => Promise<void>> = [];
  readonly messages: string[] = [];
  on(name: string, handler: any): void {
    this.onCalls.set(name, (this.onCalls.get(name) ?? 0) + 1);
    if (name === "session_start") this.sessionStarters.push(handler);
  }
  registerCommand(name: string, def: unknown): void {
    this.commands.set(name, def);
  }
  async exec(): Promise<ExeResult> {
    return { code: 0, stdout: "", stderr: "", killed: false };
  }
  async sendUserMessage(text: string): Promise<void> {
    this.messages.push(text);
  }
}

test("daemon: auto /reload is silent and never messages the agent", async () => {
  const pi = new MockPi();
  factory(pi as never);
  // the auto-reload handler is the last session_start listener registered
  const reloadHandler = pi.sessionStarters[pi.sessionStarters.length - 1];
  await withEnv({ XDG_STATE_HOME: scratchDir() }, async () => {
    const diffPath = join(scratchDir(), "pi-pty-host", "extensions-diff.json");
    mkdirSync(join(scratchDir(), "pi-pty-host"), { recursive: true });
    writeFileSync(diffPath, JSON.stringify({ added: ["a.ts"], changed: ["b.ts"] }));
    // the daemon stamped a diff and typed /reload: the reload happens
    await reloadHandler({ reason: "reload" });
    // ...but no "Extensions updated" message is injected into the agent
    assertEq(pi.messages.length, 0, "auto reload must not message the agent");
    assert(!existsSync(diffPath), "diff stamp consumed silently");
  });
  // a manual /reload without a stamp is equally silent
  await withEnv({ XDG_STATE_HOME: scratchDir() }, async () => {
    await reloadHandler({ reason: "reload" });
    assertEq(pi.messages.length, 0, "no stamp, still no message");
  });
});

test("daemon: windowless python prefers the GUI-subsystem twin", async () => {
  // A console interpreter would flash a window when the detached daemon
  // starts; the GUI-subsystem twin must lead the candidate list.
  assertEq(
    windowlessCandidates("C:\\Python311\\python.exe")[0],
    "C:\\Python311\\pythonw.exe",
    "python.exe maps to its pythonw twin",
  );
  assertEq(windowlessCandidates("python3.exe")[0], "pythonw.exe",
    "python3.exe maps to pythonw.exe");
  assertEq(windowlessCandidates("py.exe")[0], "pyw", "py.exe maps to pyw");
  assert(windowlessCandidates("python").includes("pythonw"),
    "bare python offers pythonw");
});
