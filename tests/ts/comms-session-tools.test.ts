/**
 * pi-daemon — the session-reach tools over agent-comms.
 *
 * The tools are a thin argv surface over `pi/bin/pi-comms session ...`, so
 * the tests drive them against the stub CLI: each one's argv is checked,
 * and the session wait's own chain of holds is checked page by page — what
 * it streams, the waiting state it publishes, and the ways it ends early
 * without running a command.
 */

import { join, resolve } from "node:path";

import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import { ProcessRunner } from "../../pi/extensions/daemon.ts";
import { CommsCli } from "../../pi/extensions/comms/client.ts";
import { CommsArgv } from "../../pi/extensions/comms/protocol.ts";
import {
	CommsSessionTools,
} from "../../pi/extensions/comms/session-tools.ts";
import { SessionWaitFeed } from "../../pi/extensions/comms/session-wait-feed.ts";
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

/** One `session` answer per held page, as the stub hands them out. */
function scripted(
	...answers: Array<{ page: Record<string, unknown>; exit?: number }>
): Record<string, string> {
	return {
		STUB_STATE: join(scratchDir(),
			`stub-state-${Math.random().toString(16).slice(2)}.json`),
		STUB_ANSWERS: JSON.stringify({
			session: answers.map(({ page, exit }) => ({
				stdout: page, exit: exit ?? 0,
			})),
		}),
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
			`--dir=${resolve("/srv/a")} --limit=5 --after=c1`,
			"the conversations argv");
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
			.includes(`--dir=${join(scratchDir(), "sub")}`),
			"resolved against cwd");
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

test("comms_session_wait: a wait without a cursor starts now and holds from the page's cursor", async () => {
	const first = { status: "ok", name: "pi-a", state: "busy", events: [],
		cursor: { name: "pi-a", activity: null, stateSeq: 1 }, more: false,
		maxWaitMs: 60000 };
	const second = { status: "ended", name: "pi-a", state: "idle",
		events: [{ type: "ended", reason: "exit" }],
		cursor: { name: "pi-a", activity: "m1", stateSeq: 2 }, more: false };
	const env = invocation(undefined, 0,
		scripted({ page: first }, { page: second }));
	await withEnv(env, async () => {
		const result = await call("comms_session_wait", { name: "pi-a" });
		assert(!result.isError, "not an error");
		const calls = recorded(env.STUB_LOG);
		assertEq(calls[0].join(" "),
			"--format json --agent pi-test session wait pi-a",
			"a wait without a cursor starts now");
		assert(calls[1].includes(`--cursor=${JSON.stringify(first.cursor)}`),
			"the second hold continues from the page");
		assert(calls[1].some((arg) => arg.startsWith("--timeout-ms=")),
			"bounded by the service's own limit");
		assertEq(result.structuredContent.status, "ended", "the run ended");
	});
});

test("comms_session_wait: a given cursor is sent as JSON on the first hold", async () => {
	const cursor = { name: "pi-a", activity: "m1", stateSeq: 3 };
	const env = invocation(undefined, 0, scripted({ page: {
		status: "ended", name: "pi-a", state: "idle",
		events: [{ type: "ended", reason: "exit" }], cursor, more: false } }));
	await withEnv(env, async () => {
		const result = await call("comms_session_wait",
			{ name: "pi-a", cursor });
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			`--format json --agent pi-test session wait pi-a ` +
			`--cursor=${JSON.stringify(cursor)}`, "the cursor argv");
		assert(!result.isError, "not an error");
	});
});

test("comms_session_wait: the session settling ends the wait with what it did", async () => {
	const busy = { status: "ok", name: "pi-a", state: "busy",
		events: [
			{ type: "activity", kind: "user", entryId: "m1",
				text: "run the tests" },
			{ type: "activity", kind: "assistant", entryId: "m2",
				text: "on it" },
			{ type: "activity", kind: "tool_call", entryId: "m3",
				tool: "bash", args: '{"cmd":"ls"}' },
			{ type: "activity", kind: "tool_result", entryId: "m4",
				tool: "bash", text: "a.txt" },
		],
		cursor: { name: "pi-a", activity: "m4", stateSeq: 1 }, more: false,
		maxWaitMs: 60000 };
	const idle = { status: "ok", name: "pi-a", state: "idle",
		events: [{ type: "state", state: "idle", seq: 2 }],
		cursor: { name: "pi-a", activity: "m4", stateSeq: 2 }, more: false };
	const env = invocation(undefined, 0,
		scripted({ page: busy }, { page: idle }));
	await withEnv(env, async () => {
		const result = await call("comms_session_wait", { name: "pi-a" });
		const text = result.content[0].text as string;
		assert(text.includes("pi-a settled: idle"), "the status line");
		assert(text.includes("user: run the tests"), "the prompt");
		assert(text.includes("assistant: on it"), "the reply");
		assert(text.includes("→ bash"), "the tool call");
		assert(text.includes("← bash: a.txt"), "the tool result");
		assertEq(result.structuredContent.status, "settled", "settled");
	});
});

test("comms_session_wait: an idle session that does nothing holds until the deadline", async () => {
	const idle = { status: "timed-out", name: "pi-a", state: "idle",
		events: [], cursor: { name: "pi-a", activity: null, stateSeq: 1 },
		more: false };
	const env = invocation(undefined, 0, scripted(
		{ page: idle, exit: 3 }, { page: idle, exit: 3 }));
	await withEnv(env, async () => {
		const result = await call("comms_session_wait",
			{ name: "pi-a", timeoutMs: 1 });
		assert(!result.isError, "a wait that ran out is not an error");
		assertEq(result.structuredContent.status, "timed-out", "timed out");
	});
});

test("comms_session_wait: an ended run ends the wait", async () => {
	const env = invocation(undefined, 0, scripted({ page: {
		status: "ended", name: "pi-a", state: "idle",
		events: [{ type: "ended", reason: "died" }],
		cursor: { name: "pi-a", activity: null, stateSeq: 1 },
		more: false } }));
	await withEnv(env, async () => {
		const result = await call("comms_session_wait", { name: "pi-a" });
		assert((result.content[0].text as string).includes("ended: died"),
			"the ending is reported");
		assertEq(result.structuredContent.status, "ended", "ended");
	});
});

test("comms_session_wait: activity streams to the window while it holds", async () => {
	const busy = { status: "ok", name: "pi-a", state: "busy",
		events: [
			{ type: "activity", kind: "user", entryId: "m1",
				text: "run the tests" },
		],
		cursor: { name: "pi-a", activity: "m1", stateSeq: 1 }, more: false,
		maxWaitMs: 60000 };
	const idle = { status: "ok", name: "pi-a", state: "idle",
		events: [{ type: "state", state: "idle", seq: 2 }],
		cursor: { name: "pi-a", activity: "m1", stateSeq: 2 }, more: false };
	const env = invocation(undefined, 0, {
		...scripted({ page: busy }, { page: idle }), STUB_DELAY: "0.1" });
	const seen: string[] = [];
	await withEnv(env, async () => {
		await call("comms_session_wait", { name: "pi-a" },
			{ waitUpdateMs: 20 },
			{ onUpdate: (update) => seen.push(update.content[0].text) });
		assert(seen.some((text) => text.includes("user: run the tests")),
			"the prompt reached the window");
		assert(seen.every((text) => text.split("\n").length <= 10),
			"within the collapsed preview");
	});
});

test("comms_session_wait: the whole call publishes and restores the waiting state", async () => {
	const states: boolean[] = [];
	const env = invocation(undefined, 0, scripted({ page: {
		status: "ended", name: "pi-a", state: "idle",
		events: [{ type: "ended", reason: "exit" }],
		cursor: { name: "pi-a", activity: null, stateSeq: 1 },
		more: false } }));
	await withEnv(env, async () => {
		await call("comms_session_wait", { name: "pi-a" },
			{ setWaitState: (waiting: boolean) => states.push(waiting) });
		assertEq(states.join(","), "true,false",
			"published and restored");
	});
});

test("comms_session_wait: a queued message ends the wait before it runs", async () => {
	const env = invocation({ status: "ok" });
	await withEnv(env, async () => {
		const result = await call("comms_session_wait", { name: "pi-a" },
			{}, { ctx: { hasPendingMessages: () => true } });
		assertEq(result.structuredContent.stoppedBy, "yield", "yielded");
		assertEq(recorded(env.STUB_LOG).length, 0, "the CLI never ran");
	});
});

test("comms_session_wait: a message queued mid-wait yields with the cursor so far", async () => {
	const cursor = { name: "pi-a", activity: null, stateSeq: 1 };
	const env = invocation(undefined, 0, {
		...scripted({ page: { status: "ok", name: "pi-a", state: "busy",
			events: [], cursor, more: false, maxWaitMs: 60000 } }),
		STUB_DELAY: "1" });
	let queued = false;
	await withEnv(env, async () => {
		const timer = setTimeout(() => { queued = true; }, 150);
		const result = await call("comms_session_wait",
			{ name: "pi-a", cursor }, { waitUpdateMs: 20 },
			{ ctx: { hasPendingMessages: () => queued } });
		clearTimeout(timer);
		assertEq(result.structuredContent.stoppedBy, "yield", "yielded");
		assertEq(JSON.stringify(result.structuredContent.cursor),
			JSON.stringify(cursor), "the cursor so far");
	});
});

test("comms_session_wait: an aborted call never starts its hold", async () => {
	const env = invocation({ status: "ok" });
	const controller = new AbortController();
	controller.abort();
	await withEnv(env, async () => {
		const result = await call("comms_session_wait", { name: "pi-a" },
			{}, { signal: controller.signal });
		assertEq(result.structuredContent.stoppedBy, "abort", "aborted");
		assertEq(recorded(env.STUB_LOG).length, 0, "the CLI never ran");
	});
});

test("comms_session_wait: a refusal is one tool error naming the exit", async () => {
	const env = invocation({ error: "no-session", code: -32602 }, 5,
		{ STUB_STDERR: "pi-comms: no-session [code -32602]\n" });
	await withEnv(env, async () => {
		const result = await call("comms_session_wait", { name: "ghost" });
		assert(result.isError, "a refusal is an error");
		const text = result.content[0].text as string;
		assert(text.includes("exit 5"), "names the exit code");
		assert(text.includes("no-session"), "carries the reason");
	});
});

test("SessionWaitFeed: the result keeps the newest lines when it overflows", () => {
	const feed = new SessionWaitFeed("pi-a", () => {}, 0);
	feed.add({ state: "idle", events: Array.from({ length: 6 },
		(_value, index) => ({ type: "activity", kind: "assistant",
			entryId: `m${index}`, text: `line ${index} ${"x".repeat(60)}` })) });
	const text = feed.result("settled", { name: "pi-a" }, 400);
	assert(text.includes("earlier lines omitted"),
		"the dropped lines are counted");
	assert(text.includes("line 5"), "the newest line is kept");
	assert(!text.includes("line 0 "), "the oldest line went first");
	assert(text.length <= 400, "the result fits its bound");
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

test("CommsArgv: a session wait without options sends only its name", () => {
	assertEq(CommsArgv.sessionWait({ name: "pi-a" }).join(" "),
		"session wait pi-a", "the minimal wait argv");
	assertEq(CommsArgv.reach({ name: "pi-a", node: "rog" }).join(" "),
		"--at=rog session open --name=pi-a", "reach names its peer");
});
