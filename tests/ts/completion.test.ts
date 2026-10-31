/**
 * pi-daemon — the tool-completion contract tests.
 *
 * A deferred tool result must say so, a caller must be able to learn which
 * registered tool resolves it, and the resolver must return the operation's
 * real output and status. These tests own each piece in isolation: the
 * metadata, the per-call scope, the pi-rpc/1 provider, and the resolver.
 */

import { createEventBus } from "@earendil-works/pi-coding-agent";

import { ToolCompletionCalls } from "../../pi/extensions/completion/calls.ts";
import {
	COMPLETION_DESCRIBE,
	COMPLETION_SERVICE,
	COMPLETION_VERSION,
	CompletionMetadata,
	type CompletionBinding,
} from "../../pi/extensions/completion/protocol.ts";
import { ToolCompletionService } from "../../pi/extensions/completion/service.ts";
import { ToolCompletionTool } from "../../pi/extensions/completion/tool.ts";
import { test, assert, assertEq, assertMatches } from "./harness.ts";

/** A ticket record as the daemon returns it. */
function ticket(status: string, exit: number | null = status === "running" ? null : 0, error: string | null = null) {
	return {
		id: "t-1",
		session: "abc",
		cwd: "/x",
		command: "printf out",
		kind: "shell" as const,
		status,
		created: 1,
		started: 1,
		finished: status === "running" ? null : 2,
		exit,
		term: null,
		truncated: false,
		error,
	};
}

/** A ticket client that reports the given statuses in order, last sticking. */
class ScriptedClient {
	readonly waits: string[] = [];
	readonly outputReads: string[] = [];
	constructor(
		private readonly statuses: string[],
		private readonly output: string,
		private readonly exit: number | null = 0,
		private readonly error: string | null = null,
	) {}

	async wait(id: string): Promise<ReturnType<typeof ticket> & { id: string }> {
		this.waits.push(id);
		const step = this.statuses.length > 1 ? this.statuses.shift()! : this.statuses[0];
		return { ...ticket(step, step === "running" ? null : this.exit, this.error), id };
	}

	async outputAll(id: string): Promise<string> {
		this.outputReads.push(id);
		return this.output;
	}
}

test("completion: metadata is additive, versioned, and read back defensively", () => {
	const pending = CompletionMetadata.pending({ ticket: "t-9" });
	assertEq(pending.state, "pending");
	assertEq(pending.version, COMPLETION_VERSION);
	assertEq((pending as { service: string }).service, COMPLETION_SERVICE);

	const attached = CompletionMetadata.attach({ truncated: false }, pending);
	assertEq(attached.truncated, false, "the tool's own details are preserved");
	assertEq(CompletionMetadata.of(attached)?.state, "pending");

	const final = CompletionMetadata.of(CompletionMetadata.attach(undefined, CompletionMetadata.final()));
	assertEq(final?.state, "final");
	assertEq(CompletionMetadata.of({}), undefined, "an unmarked result reads as unadvertised");
	assertEq(CompletionMetadata.of({ toolCompletion: { version: 1, state: "pending" } }), undefined,
		"a pending mark without a payload is malformed");
	assertEq(CompletionMetadata.of({ toolCompletion: { state: "final" } }), undefined,
		"a mark without a version is malformed");
});

test("completion: each call's mark stays in its own scope", async () => {
	const calls = new ToolCompletionCalls();
	const tool = {
		async execute(input: string): Promise<{ details?: unknown; content: Array<{ type: string; text: string }> }> {
			if (input === "defer") calls.pending({ ticket: input });
			await new Promise((resolve) => setTimeout(resolve, input === "defer" ? 5 : 1));
			return { content: [{ type: "text", text: input }], details: { seen: input } };
		},
	};
	calls.attach(tool);
	const [deferred, local] = await Promise.all([
		tool.execute("defer"),
		tool.execute("local"),
	]);
	assertEq(CompletionMetadata.of(deferred.details)?.state, "pending");
	assertEq((CompletionMetadata.of(deferred.details) as { payload: { ticket: string } }).payload.ticket, "defer");
	assertEq(CompletionMetadata.of(local.details)?.state, "final", "a concurrent local call is never marked");
	assertEq((local.details as { seen: string }).seen, "local", "the tool's own details survive");
});

test("completion: the service answers hello and describe before any await", () => {
	const bus = createEventBus();
	const bindings: CompletionBinding[] = [
		{ tool: "bash", versions: [COMPLETION_VERSION], resolver: { name: ToolCompletionTool.NAME } },
	];
	const stop = new ToolCompletionService(bus, bindings).start();
	const replies: Array<{ id: string; result?: unknown; error?: { code: number; message: string } }> = [];
	const off = bus.on(`pi-rpc/${COMPLETION_SERVICE}/reply`, (data) => {
		replies.push(data as { id: string; result?: unknown });
	});

	bus.emit(`pi-rpc/${COMPLETION_SERVICE}`, { jsonrpc: "2.0", id: "a", method: "rpc.hello" });
	assertEq(replies.length, 1, "hello is answered synchronously");
	const hello = replies[0].result as { service: string; version: number; methods: string[] };
	assertEq(hello.service, COMPLETION_SERVICE);
	assertEq(hello.version, COMPLETION_VERSION);
	assert(hello.methods.includes(COMPLETION_DESCRIBE), "hello advertises describe");

	bus.emit(`pi-rpc/${COMPLETION_SERVICE}`, { jsonrpc: "2.0", id: "b", method: COMPLETION_DESCRIBE });
	const described = replies[1].result as { bindings: CompletionBinding[] };
	assertEq(described.bindings.length, 1);
	assertEq(described.bindings[0].tool, "bash");
	assertEq(described.bindings[0].resolver.name, ToolCompletionTool.NAME);

	bus.emit(`pi-rpc/${COMPLETION_SERVICE}`, { jsonrpc: "2.0", id: "c", method: "nope" });
	assertEq(replies[2].error?.code, -32601, "an unknown method is a JSON-RPC error");

	off();
	stop();
	bus.emit(`pi-rpc/${COMPLETION_SERVICE}`, { jsonrpc: "2.0", id: "d", method: "rpc.hello" });
	assertEq(replies.length, 3, "the unsubscribe stops serving");
});

test("completion: the resolver waits for the ticket and returns its real output", async () => {
	const client = new ScriptedClient(["running", "done"], "bridge-final-output\n");
	const tool = new ToolCompletionTool({ client } as never, 1);
	const result = await tool.execute({ version: 1, payload: { ticket: "t-1" } });
	assertEq(result.content[0].text, "bridge-final-output\n");
	assertEq(result.isError, undefined, "a clean exit is not an error");
	assertEq(client.waits.length, 2, "the resolver waited until the ticket left running");
	assertEq((result.details.toolCompletion as { state: string }).state, "final");
});

test("completion: a failed command keeps its output and reports the failure", async () => {
	const client = new ScriptedClient(["failed"], "boom-out\n", 3, "signal 9");
	const tool = new ToolCompletionTool({ client } as never, 1);
	const result = await tool.execute({ version: 1, payload: { ticket: "t-1" } });
	assertMatches(result.content[0].text, /boom-out/);
	assertMatches(result.content[0].text, /\[failed exit 3 signal 9\]/);
	assertEq(result.isError, true);
});

test("completion: an unsupported version or an empty payload fails loudly", async () => {
	const tool = new ToolCompletionTool({ client: new ScriptedClient(["done"], "") } as never, 1);
	let versionError = "";
	try {
		await tool.execute({ version: 2, payload: { ticket: "t-1" } });
	} catch (error) {
		versionError = (error as Error).message;
	}
	assertMatches(versionError, /unsupported completion version 2/);

	let payloadError = "";
	try {
		await tool.execute({ version: 1, payload: { other: "x" } });
	} catch (error) {
		payloadError = (error as Error).message;
	}
	assertMatches(payloadError, /names no operation/);
});

test("completion: the resolver registers as a codemode tool, never model-facing", () => {
	const registered: Array<{ name: string; exposure?: string; execute?: unknown }> = [];
	const pi = {
		registerTool: (tool: { name: string; exposure?: string; execute?: unknown }) => registered.push(tool),
	};
	new ToolCompletionTool({ client: new ScriptedClient(["done"], "") } as never, 1).register(pi as never);
	assertEq(registered.length, 1);
	assertEq(registered[0].name, ToolCompletionTool.NAME);
	assertEq(registered[0].exposure, "codemode");
	assert(typeof registered[0].execute === "function");
});
