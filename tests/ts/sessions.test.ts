/**
 * pi-daemon — sessions roster and stop tool tests.
 *
 * Both tools are driven through a fake requester: the roster's command
 * shaping, live-versus-all rendering, conversation file and stopped
 * count, and the stop tool's own-session refusal, error surfacing and
 * outcome text. The transport itself is covered by the transcript suite.
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
	return { result, text: result.content[0].text as string, details: result.details as any, client, pi };
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
	const { result, text, details, client, pi } = await roster(
		{}, { ok: true, sessions: live, stopped: 4 });
	// `annotations` is how pi decides a tool mutates nothing; a turn that
	// lists sessions must not ask for a write.
	assertEq(pi.tools.get("sessions").annotations.readOnlyHint, true, "read-only");
	assertEq(client.requests.length, 1, "one request");
	assertEq(client.requests[0].cmd, "list", "list command");
	assertEq(client.requests[0].all, false, "live-only");
	assert(text.startsWith("2 live sessions:"), `header: ${text}`);
	assert(text.includes("- pi-alpha busy attached x2 /w/a file=/s/a.jsonl"), `alpha line: ${text}`);
	assert(text.includes("- pi-beta idle detached /w/b file=none"), `beta line: ${text}`);
	assert(text.includes("4 stopped records not shown"), `stopped note: ${text}`);
	assertEq(details.count, 2, "count");
	assertEq(details.names.join(","), "pi-alpha,pi-beta", "names");
	assertEq(result.isError, false, "a served list is not an error");
});

test("sessions: all names the phase a stopped record ended in, and a missing file", async () => {
	const all = [
		...live,
		{ name: "pi-gamma", state: "idle", phase: "exited", dir: "/w/c", attached: false, file: "/s/g.jsonl", fileExists: false },
		{ name: "pi-delta", state: "idle", phase: "exited", dir: "/w/d", attached: false, file: null },
	];
	const { text, client } = await roster({ all: true }, { ok: true, sessions: all });
	assertEq(client.requests[0].all, true, "all");
	assert(text.startsWith("4 records:"), `header: ${text}`);
	// The daemon keeps a stopped record's last state and says "exited" in
	// the phase; rendering one as the other would invent a state.
	assert(text.includes("- pi-gamma idle phase=exited detached /w/c file=/s/g.jsonl (missing)"),
		`gamma: ${text}`);
	assert(text.includes("- pi-delta idle phase=exited detached /w/d file=none"),
		`delta: ${text}`);
	assert(!text.includes("not shown"), `no stopped note: ${text}`);
});

test("sessions: a daemon error is one line and a tool error, never thrown", async () => {
	const { text, details, result } = await roster({}, { ok: false, error: "no-daemon" });
	assert(text.startsWith("sessions: no-daemon"), `error line: ${text}`);
	assertEq(details.ok, false, "ok flag");
	assertEq(result.isError, true, "a failed list is a tool error");
});

test("session_stop: a queued stop reports the state and does not claim the run ended", async () => {
	const { result, client } = await stop(
		{ name: "pi-beta" }, { ok: true, queued: true, alive: true, state: "stopping" });
	assertEq(client.requests[0].cmd, "stop", "stop command");
	assertEq(client.requests[0].name, "pi-beta", "target");
	const text = result.content[0].text as string;
	assert(text.includes("stop requested (state=stopping, alive)"), `text: ${text}`);
	assert(!text.includes("run ended"), `no false completion: ${text}`);
	assertEq(result.details.ended, false, "still alive");
	assertEq(result.isError, undefined, "not an error");
});

test("session_stop: a record already ended says so and stays resumable", async () => {
	const { result } = await stop(
		{ name: "pi-beta" }, { ok: true, queued: false, alive: false, state: "idle" });
	const text = result.content[0].text as string;
	assert(text.includes("pi-beta: run ended (state=idle)"), `text: ${text}`);
	// Only a conversation file on disk can be resumed, and this reply cannot
	// see one, so it points at the roster instead of promising a resume.
	assert(text.includes("all: true shows whether its conversation file"), `file hint: ${text}`);
	assertEq(result.details.ended, true, "ended");
});

test("session_stop: refuses its own session in either spelling, without asking", async () => {
	const bare = await stop({ name: "alpha" }, { ok: true }, "pi-alpha");
	assertEq(bare.client.requests.length, 0, "no request for the bare name");
	assertEq(bare.result.isError, true, "error result");
	assertEq(bare.result.details.error, "own-session", "code");
	// The store resolves `pi-` in both directions, so a hosted session
	// named without the prefix reaches itself through the prefixed spelling.
	const prefixed = await stop({ name: "pi-alpha" }, { ok: true }, "alpha");
	assertEq(prefixed.client.requests.length, 0, "no request for the prefixed name");
	assertEq(prefixed.result.details.error, "own-session", "prefixed spelling refused");
	const exact = await stop({ name: "pi-alpha" }, { ok: true }, "pi-alpha");
	assertEq(exact.result.details.error, "own-session", "full name refused too");
	// The store resolves exactly one alias step, so a doubled spelling
	// reaches the session that already carries the prefix.
	const doubled = await stop({ name: "pi-pi-alpha" }, { ok: true }, "pi-alpha");
	assertEq(doubled.client.requests.length, 0, "no request for the doubled name");
	assertEq(doubled.result.details.error, "own-session", "doubled spelling refused");
});

test("session_stop: a missing name and a daemon refusal are tool errors", async () => {
	const empty = await stop({ name: "  " }, { ok: true });
	assertEq(empty.client.requests.length, 0, "no request");
	assertEq(empty.result.details.error, "name-required", "name check");
	const refused = await stop({ name: "pi-beta" }, { ok: false, error: "no-session" });
	assertEq(refused.result.isError, true, "refusal is an error");
	assert(refused.result.content[0].text.includes("no-session"), `text: ${refused.result.content[0].text}`);
});
