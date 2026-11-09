/**
 * pi-daemon — sessions roster and stop tool tests.
 *
 * Both tools are driven through a fake requester: the roster's command
 * shaping, live-versus-all rendering and stopped count, and the stop
 * tool's own-session refusal, error surfacing and success text. The
 * transport itself is covered by the transcript suite.
 */

import { assert, assertEq, test } from "./harness.ts";
import { SessionRoster } from "../../pi/extensions/sessions/roster.ts";
import { SessionStop } from "../../pi/extensions/sessions/stop.ts";

/** Records the outgoing commands and returns one canned reply. */
class FakeRequester {
	readonly requests: Array<Record<string, unknown>> = [];
	reply: Record<string, unknown> = { ok: true, sessions: [] };

	async request(cmd: Record<string, unknown>): Promise<Record<string, unknown>> {
		this.requests.push(cmd);
		return this.reply;
	}
}

/** Minimal ExtensionAPI stand-in. */
class MockPi {
	readonly tools = new Map<string, any>();

	registerTool(def: any): void {
		this.tools.set(def.name, def);
	}
}

const live = [
	{ name: "pi-alpha", state: "busy", dir: "/w/a", attached: true, clients: 2, file: "/s/a.jsonl" },
	{ name: "pi-beta", state: "idle", dir: "/w/b", attached: false, clients: 0, file: null },
];

async function roster(params: Record<string, unknown>, reply: Record<string, unknown>) {
	const client = new FakeRequester();
	client.reply = reply;
	const pi = new MockPi();
	new SessionRoster(client).register(pi as never);
	const result = await pi.tools.get("sessions").execute("call1", params);
	return { text: result.content[0].text as string, details: result.details as any, client };
}

async function stop(
	params: Record<string, unknown>,
	reply: Record<string, unknown>,
	own = "pi-alpha",
) {
	const client = new FakeRequester();
	client.reply = reply;
	const pi = new MockPi();
	new SessionStop(client, own).register(pi as never);
	const result = await pi.tools.get("session_stop").execute("call1", params);
	return { result, client };
}

test("sessions: registers read-only and lists only live records by default", async () => {
	const { text, details, client } = await roster({}, { ok: true, sessions: live, stopped: 4 });
	assertEq(client.requests.length, 1, "one request");
	assertEq(client.requests[0].cmd, "list", "list command");
	assertEq(client.requests[0].all, false, "live-only");
	assert(text.startsWith("2 live sessions:"), `header: ${text}`);
	assert(text.includes("- pi-alpha busy attached x2 /w/a"), `alpha line: ${text}`);
	assert(text.includes("- pi-beta idle detached /w/b"), `beta line: ${text}`);
	assert(text.includes("4 stopped records not shown"), `stopped note: ${text}`);
	assertEq(details.count, 2, "count");
	assertEq(details.names.join(","), "pi-alpha,pi-beta", "names");
});

test("sessions: all asks for the stopped records and marks a missing file", async () => {
	const all = [
		...live,
		{ name: "pi-gamma", state: "stopped", phase: "exited", dir: "/w/c", attached: false, file: null, fileExists: false },
	];
	const { text, client } = await roster({ all: true }, { ok: true, sessions: all });
	assertEq(client.requests[0].all, true, "all");
	assert(text.startsWith("3 records:"), `header: ${text}`);
	assert(text.includes("- pi-gamma stopped detached /w/c (no conversation file)"), `gamma: ${text}`);
	assert(!text.includes("not shown"), `no stopped note: ${text}`);
});

test("sessions: a daemon error is one line, never thrown", async () => {
	const { text, details } = await roster({}, { ok: false, error: "no-daemon" });
	assert(text.startsWith("sessions: no-daemon"), `error line: ${text}`);
	assertEq(details.ok, false, "ok flag");
});

test("session_stop: stops a named session and says it stays resumable", async () => {
	const { result, client } = await stop({ name: "pi-beta" }, { ok: true });
	assertEq(client.requests[0].cmd, "stop", "stop command");
	assertEq(client.requests[0].name, "pi-beta", "target");
	assert(result.content[0].text.includes("pi-rc attach pi-beta"), `text: ${result.content[0].text}`);
	assertEq(result.isError, undefined, "not an error");
});

test("session_stop: refuses its own session without asking the daemon", async () => {
	const { result, client } = await stop({ name: "alpha" }, { ok: true }, "pi-alpha");
	assertEq(client.requests.length, 0, "no request");
	assertEq(result.isError, true, "error result");
	assertEq(result.details.error, "own-session", "code");
	const exact = await stop({ name: "pi-alpha" }, { ok: true }, "pi-alpha");
	assertEq(exact.result.details.error, "own-session", "full name refused too");
});

test("session_stop: a missing name and a daemon refusal are tool errors", async () => {
	const empty = await stop({ name: "  " }, { ok: true });
	assertEq(empty.result.details.error, "name-required", "name check");
	const refused = await stop({ name: "pi-beta" }, { ok: false, error: "no-such-session" });
	assertEq(refused.result.isError, true, "refusal is an error");
	assert(refused.result.content[0].text.includes("no-such-session"), `text: ${refused.result.content[0].text}`);
});
