/**
 * pi-daemon — the session-reach tools over agent-comms.
 *
 * The tools are a thin argv surface over `pi/bin/pi-comms session ...`, so
 * the tests drive them against the stub CLI: each one's argv is checked,
 * and the feed's hold is checked through its own options — the waiting
 * state it publishes, the update it streams, and the two ways it stops
 * early without running a command.
 */

import { join } from "node:path";

import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import { ProcessRunner } from "../../pi/extensions/daemon.ts";
import { CommsCli } from "../../pi/extensions/comms/client.ts";
import { CommsArgv } from "../../pi/extensions/comms/protocol.ts";
import {
	CommsSessionTools,
} from "../../pi/extensions/comms/session-tools.ts";
import { execProcess, recorded, stub } from "./comms-stub.ts";

/** Minimal ExtensionAPI stand-in, as the other extension tests use. */
class FakePi {
	readonly tools = new Map<string, any>();

	registerTool(definition: any): void {
		this.tools.set(definition.name, definition);
	}
}

/** One canned invocation for the stub CLI. */
function invocation(
	stdout: unknown,
	exit = 0,
	extra: Record<string, string> = {},
): Record<string, string> {
	return {
		PI_COMMS_AGENT_ID: "pi-test",
		PI_COMMS_BIN: stub(),
		STUB_LOG: join(scratchDir(),
			`stub-${Math.random().toString(16).slice(2)}.log`),
		STUB_STDOUT: stdout === undefined ? "" : JSON.stringify(stdout),
		STUB_STDERR: "",
		STUB_EXIT: String(exit),
		...extra,
	};
}

/** Run one session tool: the options it owns, and the context it runs in. */
async function call(
	name: string,
	params: Record<string, unknown>,
	options: Record<string, unknown> = {},
	run: {
		signal?: AbortSignal;
		onUpdate?: (update: {
			content: Array<{ type: "text"; text: string }>;
			details: Record<string, unknown>;
		}) => void;
		ctx?: Record<string, unknown>;
	} = {},
	pis: FakePi = new FakePi(),
): Promise<any> {
	new CommsSessionTools(
		new CommsCli(new ProcessRunner(execProcess)),
		options as never,
	).register(pis as never);
	const tool = pis.tools.get(name);
	return tool.execute("call1", params, run.signal, run.onUpdate, {
		cwd: scratchDir(),
		...run.ctx,
	});
}

test("comms_sessions: a live listing runs `session list`", async () => {
	const env = invocation({ status: "ok", sessions: [], stopped: 2 });
	await withEnv(env, async () => {
		const result = await call("comms_sessions", {});
		assert(!result.isError, "not an error");
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test session list",
			"the live listing argv");
	});
});

test("comms_sessions: all and view reach the CLI as named options", async () => {
	const env = invocation({ status: "ok", conversations: [] });
	await withEnv(env, async () => {
		await call("comms_sessions", { view: "conversations", dir: "/srv/a",
			limit: 5, after: "c1" });
		await call("comms_sessions", { all: true });
		const calls = recorded(env.STUB_LOG);
		assertEq(calls[0].join(" "),
			"--format json --agent pi-test session conversations " +
			"--dir=/srv/a --limit=5 --after=c1", "the conversations argv");
		assertEq(calls[1].join(" "),
			"--format json --agent pi-test session list --all",
			"the listing with ended records");
	});
});

test("comms_sessions: a relative directory is resolved here", async () => {
	const env = invocation({ status: "ok", conversations: [] });
	await withEnv(env, async () => {
		await call("comms_sessions", { view: "conversations", dir: "sub" });
		assert(recorded(env.STUB_LOG)[0].join(" ")
			.includes(`--dir=${scratchDir()}/sub`), "resolved against cwd");
	});
});

test("comms_reach: each target spelling reaches the CLI", async () => {
	const env = invocation({ status: "hosted", name: "pi-new" });
	await withEnv(env, async () => {
		await call("comms_reach", { sessionId: "s1", dir: "/srv/a" });
		await call("comms_reach", { name: "pi-a" });
		await call("comms_reach", { action: "stop", name: "pi-a" });
		const calls = recorded(env.STUB_LOG);
		assertEq(calls[0].join(" "),
			"--format json --agent pi-test session open --id=s1 --dir=/srv/a",
			"opened by id");
		assertEq(calls[1].join(" "),
			"--format json --agent pi-test session open --name=pi-a",
			"opened by name");
		assertEq(calls[2].join(" "),
			"--format json --agent pi-test session stop pi-a",
			"stopped by name");
	});
});

test("comms_reach: a target is required and never guessed", async () => {
	const env = invocation({ status: "hosted" });
	await withEnv(env, async () => {
		const none = await call("comms_reach", {});
		assert(none.isError, "no target is a tool error");
		const two = await call("comms_reach", { name: "a", file: "/b" });
		assert(two.isError, "two targets are a tool error");
		const stop = await call("comms_reach", { action: "stop" });
		assert(stop.isError, "a stop needs its name");
		assertEq(recorded(env.STUB_LOG).length, 0, "the CLI never ran");
	});
});

test("comms_input: the text travels exactly as given", async () => {
	const env = invocation({ status: "ok", bytes: 3 });
	await withEnv(env, async () => {
		const result = await call("comms_input",
			{ name: "pi-a", text: "hi\r" });
		assert(!result.isError, "not an error");
		// The Enter a terminal would send: the byte itself, not an escape.
		assertEq(JSON.stringify(recorded(env.STUB_LOG)[0].slice(4)),
			JSON.stringify(["session", "input", "pi-a", "--text=hi\r"]),
			"the input argv");
	});
});

test("comms_feed: a first page prints the output and its cursor", async () => {
	const page = {
		status: "ok",
		name: "pi-a",
		events: [
			{ type: "output", offset: 0, text: "hello\n", skipped: 0 },
			{ type: "state", state: "busy", seq: 3 },
			{ type: "activity", kind: "tool_call", entryId: "m1" },
			{ type: "ended", reason: "exit" },
		],
		cursor: { name: "pi-a", output: 6, activity: "m1", stateSeq: 3 },
		more: false,
	};
	const env = invocation(page);
	await withEnv(env, async () => {
		const result = await call("comms_feed", { name: "pi-a",
			include: ["output", "activity"], timeoutMs: 2000, maxBytes: 4096 });
		assert(!result.isError, "not an error");
		const text = result.content[0].text as string;
		assert(text.startsWith("hello\n"), "the output leads");
		assert(text.includes("· tool_call m1"), "one line per activity");
		assert(text.includes("state: busy"), "the state line");
		assert(text.includes("ended: exit"), "the ended line");
		assert(text.includes('"output":6'), "the continuing cursor");
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test session feed pi-a " +
			"--include=output,activity --timeout-ms=2000 --max-bytes=4096",
			"the first-page argv");
		assertEq((result.structuredContent as any).cursor.output, 6,
			"the cursor is structured");
	});
});

test("comms_feed: a page that found nothing is a page, not a failure", async () => {
	const page = { status: "timed-out", name: "pi-a", events: [],
		cursor: { name: "pi-a" }, more: false };
	const env = invocation(page, 3);
	await withEnv(env, async () => {
		const result = await call("comms_feed", { name: "pi-a" });
		assert(!result.isError, "an empty hold is not an error");
		assert((result.content[0].text as string)
			.includes("no new output or activity"), "says what it found");
	});
});

test("comms_feed: the cursor is sent as JSON and only a hold waits", async () => {
	const cursor = { name: "pi-a", generation: 1, output: 6, activity: "m1",
		stateSeq: 3 };
	const env = invocation({ status: "timed-out", events: [], cursor }, 3);
	const states: boolean[] = [];
	await withEnv(env, async () => {
		await call("comms_feed", { name: "pi-a", cursor },
			{ setWaitState: (waiting: boolean) => states.push(waiting) });
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			`--format json --agent pi-test session feed pi-a ` +
			`--cursor=${JSON.stringify(cursor)}`, "the cursor argv");
		assertEq(states.join(","), "true,false",
			"a hold publishes and restores the waiting state");
	});
});

test("comms_feed: a first page never publishes a waiting state", async () => {
	const states: boolean[] = [];
	const env = invocation({ status: "ok", events: [], cursor: {} });
	await withEnv(env, async () => {
		await call("comms_feed", { name: "pi-a" },
			{ setWaitState: (waiting: boolean) => states.push(waiting) });
		assertEq(states.length, 0, "no waiting state for a read");
	});
});

test("comms_feed: a queued message ends the hold before it runs", async () => {
	const env = invocation({ status: "ok" });
	await withEnv(env, async () => {
		const result = await call("comms_feed", { name: "pi-a", cursor: {} },
			{}, { ctx: { hasPendingMessages: () => true } });
		assertEq(result.structuredContent.stoppedBy, "yield", "yielded");
		assertEq(recorded(env.STUB_LOG).length, 0, "the CLI never ran");
	});
});

test("comms_feed: an aborted call never starts its hold", async () => {
	const env = invocation({ status: "ok" });
	const controller = new AbortController();
	controller.abort();
	await withEnv(env, async () => {
		const result = await call("comms_feed", { name: "pi-a" },
			{}, { signal: controller.signal });
		assertEq(result.structuredContent.stoppedBy, "abort", "aborted");
		assertEq(recorded(env.STUB_LOG).length, 0, "the CLI never ran");
	});
});

test("comms_feed: a refusal is one tool error naming the exit", async () => {
	const env = invocation({ error: "no-session", code: -32602 }, 5,
		{ STUB_STDERR: "pi-comms: no-session [code -32602]\n" });
	await withEnv(env, async () => {
		const result = await call("comms_feed", { name: "ghost" });
		assert(result.isError, "a refusal is an error");
		const text = result.content[0].text as string;
		assert(text.includes("exit 5"), "names the exit code");
		assert(text.includes("no-session"), "carries the reason");
	});
});

test("comms_feed: a streamed update reports the elapsed time", async () => {
	const env = invocation({ status: "ok", events: [], cursor: {} }, 0,
		{ STUB_DELAY: "0.4" });
	const updates: Array<{ details: Record<string, unknown> }> = [];
	await withEnv(env, async () => {
		await call("comms_feed", { name: "pi-a", cursor: {} },
			{ waitUpdateMs: 50 },
			{ onUpdate: (update) => updates.push(update) });
		assert(updates.length > 0, "the window saw progress");
		assert(typeof updates[updates.length - 1].details.waitedMs === "number",
			"with the time it waited");
	});
});

test("comms_sessions: the argv carries the peer when one is named", async () => {
	const env = invocation({ status: "ok", sessions: [] });
	await withEnv(env, async () => {
		await call("comms_sessions", { node: "rog" });
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test --at=rog session list",
			"the peer comes first");
	});
});

test("CommsArgv: a feed without options sends only its name", () => {
	assertEq(CommsArgv.feed({ name: "pi-a" }).join(" "),
		"session feed pi-a", "the minimal feed argv");
	assertEq(CommsArgv.reach({ name: "pi-a", node: "rog" }).join(" "),
		"--at=rog session open --name=pi-a", "reach names its peer");
});
