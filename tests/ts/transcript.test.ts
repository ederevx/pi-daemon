/**
 * pi-daemon — transcript retrieval tool tests.
 *
 * Covers the control transport (`ControlClient`: handshake, reply,
 * rejection, missing endpoint, closed socket) against a real loopback
 * stub server, and the tool itself (`TranscriptTool`: registration,
 * view dispatch, target defaulting, range decoding, error surfacing)
 * against an injected requester.
 */

import { createServer, type Server, type Socket } from "node:net";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { test, assert, assertEq, scratchDir } from "./harness.ts";
import { ControlClient } from "../../pi/extensions/transcript/client.ts";
import { TranscriptTool } from "../../pi/extensions/transcript/tool.ts";

/** A loopback control stub: one authenticated JSONL reply per command. */
class StubControlServer {
	private readonly server: Server;
	private readonly replies: Array<Record<string, unknown>>;
	private port = 0;
	private cursor = 0;
	readonly commands: Array<Record<string, unknown>> = [];
	rejectHello = false;
	closeWithoutReply = false;

	constructor(replies: Array<Record<string, unknown>>) {
		this.replies = replies;
		this.server = createServer((socket) => this.handle(socket));
	}

	/** Bind to an ephemeral port and write an endpoint file. */
	async start(name: string): Promise<string> {
		await new Promise<void>((resolve) =>
			this.server.listen(0, "127.0.0.1", resolve));
		const address = this.server.address();
		this.port = typeof address === "object" && address ? address.port : 0;
		const path = join(scratchDir(), `${name}.sock`);
		writeFileSync(path, JSON.stringify({
			host: "127.0.0.1",
			port: this.port,
			token: "test-token",
		}));
		return path;
	}

	async stop(): Promise<void> {
		await new Promise<void>((resolve) => this.server.close(() => resolve()));
	}

	/** Serve one connection: hello, then exactly one command. */
	private handle(socket: Socket): void {
		let buffer = "";
		let greeted = false;
		socket.on("data", (chunk: Buffer) => {
			buffer += String(chunk);
			for (;;) {
				const index = buffer.indexOf("\n");
				if (index < 0) return;
				const line = buffer.slice(0, index);
				buffer = buffer.slice(index + 1);
				if (!line.trim()) continue;
				if (!greeted) {
					greeted = true;
					socket.write(JSON.stringify({
						ok: !this.rejectHello,
						proto: 1,
						caps: ["transcript.v1"],
					}) + "\n");
					continue;
				}
				this.commands.push(JSON.parse(line) as Record<string, unknown>);
				if (this.closeWithoutReply) {
					socket.destroy();
					return;
				}
				const reply = this.replies[this.cursor] ?? { ok: true };
				this.cursor++;
				socket.write(JSON.stringify(reply) + "\n");
				return;
			}
		});
		socket.on("error", () => undefined);
	}
}

/** Records the outgoing commands and returns a canned reply. */
class FakeRequester {
	readonly requests: Array<Record<string, unknown>> = [];
	reply: Record<string, unknown> = { ok: true, entryCount: 3 };

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

const sessionCtx = {
	sessionManager: { getSessionFile: () => join(scratchDir(), "wire.jsonl") },
};

async function runTool(
	params: Record<string, unknown>,
	ctx: unknown = sessionCtx,
): Promise<{ text: string; details: any }> {
	const pi = new MockPi();
	new TranscriptTool(new FakeRequester()).register(pi as never);
	const tool = pi.tools.get("transcript_read");
	const result = await tool.execute("call1", params, undefined, undefined, ctx);
	return { text: result.content[0].text, details: result.details };
}

// -- ControlClient ---------------------------------------------------------

test("client: handshake then command round-trips the reply", async () => {
	const server = new StubControlServer([{ ok: true, entryCount: 7 }]);
	const path = await server.start("round-trip");
	try {
		const reply = await new ControlClient(path).request({ cmd: "transcript-stat" });
		assertEq(reply.ok, true, "reply ok");
		assertEq(reply.entryCount, 7, "reply payload");
		assertEq(server.commands.length, 1, "one command");
		assertEq(server.commands[0].cmd, "transcript-stat", "command name");
	} finally {
		await server.stop();
	}
});

test("client: a rejected handshake becomes handshake-rejected", async () => {
	const server = new StubControlServer([{ ok: true }]);
	server.rejectHello = true;
	const path = await server.start("reject");
	try {
		const reply = await new ControlClient(path).request({ cmd: "transcript-stat" });
		assertEq(reply.ok, false, "not ok");
		assertEq(reply.error, "handshake-rejected", "error kind");
		assertEq(server.commands.length, 0, "no command sent");
	} finally {
		await server.stop();
	}
});

test("client: a missing endpoint file is no-daemon", async () => {
	const reply = await new ControlClient(join(scratchDir(), "absent.sock"))
		.request({ cmd: "transcript-stat" });
	assertEq(reply.ok, false, "not ok");
	assertEq(reply.error, "no-daemon", "error kind");
});

test("client: a socket that closes without a reply is unavailable", async () => {
	const server = new StubControlServer([{ ok: true }]);
	server.closeWithoutReply = true;
	const path = await server.start("closed");
	try {
		const reply = await new ControlClient(path).request({ cmd: "transcript-stat" });
		assertEq(reply.ok, false, "not ok");
		assertEq(reply.error, "daemon-unavailable", "error kind");
	} finally {
		await server.stop();
	}
});

// -- TranscriptTool --------------------------------------------------------

test("tool: registers read-only and appends nothing", () => {
	const pi = new MockPi();
	new TranscriptTool(new FakeRequester()).register(pi as never);
	const tool = pi.tools.get("transcript_read");
	assert(tool, "tool registered");
	assertEq(tool.annotations.readOnlyHint, true, "read-only hint");
	assertEq(tool.renderCall, undefined, "no custom call renderer");
	assertEq(tool.renderResult, undefined, "no custom result renderer");
});

test("tool: stat is the default view and the session file the default target", async () => {
	const requester = new FakeRequester();
	requester.reply = { ok: true, sessionId: "s", entryCount: 5, index: { state: "ok" } };
	const pi = new MockPi();
	new TranscriptTool(requester).register(pi as never);
	await pi.tools.get("transcript_read").execute(
		"call1", {}, undefined, undefined, sessionCtx);
	assertEq(requester.requests[0].cmd, "transcript-stat", "stat command");
	assertEq(requester.requests[0].file, sessionCtx.sessionManager.getSessionFile(),
		"current session file");
});

test("tool: entries dispatch carries cursor, ids, limit, and fields", async () => {
	const requester = new FakeRequester();
	requester.reply = { ok: true, entries: [{ id: "m1" }], hasMore: false };
	const pi = new MockPi();
	new TranscriptTool(requester).register(pi as never);
	const result = await pi.tools.get("transcript_read").execute(
		"call1",
		{ view: "entries", since: "m2", ids: ["m3"], limit: 2, fields: "full" },
		undefined, undefined, sessionCtx);
	const cmd = requester.requests[0];
	assertEq(cmd.cmd, "transcript-entries", "entries command");
	assertEq(cmd.since, "m2", "since");
	assertEq((cmd.ids as string[])[0], "m3", "ids");
	assertEq(cmd.limit, 2, "limit");
	assertEq(cmd.fields, "full", "fields");
	assert(result.content[0].text.includes("m1"), "renders the entries");
});

test("tool: path, tree, and range map to their commands", async () => {
	const cases: Array<[string, string]> = [
		["path", "transcript-path"],
		["tree", "transcript-tree"],
		["range", "transcript-range"],
	];
	for (const [view, expected] of cases) {
		const requester = new FakeRequester();
		requester.reply = { ok: true, bytes: "" };
		const pi = new MockPi();
		new TranscriptTool(requester).register(pi as never);
		await pi.tools.get("transcript_read").execute(
			"call1", { view }, undefined, undefined, sessionCtx);
		assertEq(requester.requests[0].cmd, expected, `${view} command`);
	}
});

test("tool: range decodes base64 bytes into text", async () => {
	const requester = new FakeRequester();
	const line = '{"type":"message","id":"m1"}\n';
	requester.reply = {
		ok: true,
		bytes: Buffer.from(line, "utf8").toString("base64"),
		offset: 10,
		length: line.length,
	};
	const pi = new MockPi();
	new TranscriptTool(requester).register(pi as never);
	const result = await pi.tools.get("transcript_read").execute(
		"call1", { view: "range" }, undefined, undefined, sessionCtx);
	assert(result.content[0].text.includes('"id":"m1"'), "decoded text");
	assert(result.content[0].text.includes("[10.."), "offset header");
});

test("tool: an explicit file overrides the session, and a missing target explains", async () => {
	const requester = new FakeRequester();
	const pi = new MockPi();
	new TranscriptTool(requester).register(pi as never);
	await pi.tools.get("transcript_read").execute(
		"call1", { file: "/store/other.jsonl" }, undefined, undefined, sessionCtx);
	assertEq(requester.requests[0].file, "/store/other.jsonl", "explicit file");

	const bare = await runTool({}, { sessionManager: { getSessionFile: () => undefined } });
	assertEq(bare.details.error, "no-session", "no-session detail");
	assert(bare.text.includes("No session file"), "no-session message");
	assertEq(bare.details.ok, false, "not ok");
});

test("tool: a daemon error is surfaced verbatim and never thrown", async () => {
	const requester = new FakeRequester();
	requester.reply = { ok: false, error: "bad-transcript" };
	const pi = new MockPi();
	new TranscriptTool(requester).register(pi as never);
	const result = await pi.tools.get("transcript_read").execute(
		"call1", {}, undefined, undefined, sessionCtx);
	assert(result.content[0].text.includes("bad-transcript"), "error text");
	assertEq(result.details.ok, false, "error detail");
});
