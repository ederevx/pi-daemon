/**
 * pi-daemon — agent-comms tool tests.
 *
 * The tools are a thin argv surface over `pi/bin/pi-comms`, so the tests
 * drive them against a stub CLI script: it records its argv and prints a
 * canned JSON answer with a canned exit code. Each tool gets one success
 * case, and the CLI's failure vocabulary is covered as tool errors.
 */

import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExecOptions, ExecResult } from "@earendil-works/pi-coding-agent";

import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import { ProcessRunner } from "../../pi/extensions/daemon.ts";
import { CommsCli } from "../../pi/extensions/comms/client.ts";
import { CommsArgv } from "../../pi/extensions/comms/protocol.ts";
import {
	COMMS_LOG,
	CommsMessageLog,
} from "../../pi/extensions/comms/message-log.ts";
import { CommsTools } from "../../pi/extensions/comms/tools.ts";

/** A stub `pi-comms`: log argv, print the canned answer, exit as told. */
function stub(): string {
	const path = join(scratchDir(), "pi-comms-stub");
	writeFileSync(path, `#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ.get("STUB_STDOUT", ""))
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
`, { mode: 0o755 });
	return path;
}

/** The real child-process seam, shaped like pi's `exec`. */
function execProcess(
	file: string,
	args: string[],
	options: ExecOptions = {},
): Promise<ExecResult> {
	return new Promise((resolve) => {
		const child = spawn(file, args, {
			cwd: options.cwd,
			stdio: ["ignore", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		let killed = false;
		const stop = () => {
			killed = true;
			child.kill("SIGKILL");
		};
		const timer = options.timeout === undefined
			? undefined
			: setTimeout(stop, options.timeout);
		options.signal?.addEventListener("abort", stop, { once: true });
		child.stdout.on("data", (chunk: Buffer) => { stdout += String(chunk); });
		child.stderr.on("data", (chunk: Buffer) => { stderr += String(chunk); });
		child.on("error", () => {
			if (timer) clearTimeout(timer);
			resolve({ stdout, stderr, code: 1, killed });
		});
		child.on("close", (code) => {
			if (timer) clearTimeout(timer);
			resolve({ stdout, stderr, code: code ?? 0, killed });
		});
	});
}

/** The branch a received envelope is appended to, as the session manager
 *  exposes it: the log reads it back on every call, and a navigation is a
 *  different branch holding different entries. */
class FakeSession {
	private current: Array<Record<string, unknown>> = [];

	get entries(): Array<Record<string, unknown>> {
		return this.current;
	}

	getBranch(): Array<Record<string, unknown>> {
		return this.current;
	}

	/** Navigate to a branch that holds nothing this one recorded. */
	switchBranch(): void {
		this.current = [];
	}
}

/** Minimal ExtensionAPI stand-in, as the other extension tests use. */
class FakePi {
	readonly tools = new Map<string, any>();
	readonly session = new FakeSession();
	/** Appends to refuse before the branch takes one, as a full store does. */
	refuseAppends = 0;

	registerTool(definition: any): void {
		this.tools.set(definition.name, definition);
	}

	/** One branch entry of the type the message log owns. */
	appendEntry(customType: string, data: unknown): void {
		if (this.refuseAppends > 0) {
			this.refuseAppends -= 1;
			throw new Error("the branch refused the entry");
		}
		this.session.entries.push({ type: "custom", customType, data });
	}
}

/** Run one tool handler through the stub CLI, against one branch. */
async function call(
	name: string,
	params: Record<string, unknown>,
	pi: FakePi = new FakePi(),
): Promise<any> {
	new CommsTools(
		new CommsCli(new ProcessRunner(execProcess)),
		new CommsMessageLog(pi as never),
	).register(pi as never);
	const tool = pi.tools.get(name);
	return tool.execute("call1", params, undefined, undefined, {
		cwd: scratchDir(),
		sessionManager: pi.session,
	});
}

/** The argv lines the stub recorded, in call order. */
function recorded(log: string): string[][] {
	let raw = "";
	try {
		raw = readFileSync(log, "utf8");
	} catch {
		return [];
	}
	return raw.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

/** One canned invocation: the stub script, a log, and its answer. */
function invocation(stdout: unknown, exit = 0, stderr = ""): Record<string, string> {
	return {
		PI_COMMS_AGENT_ID: "pi-test",
		PI_COMMS_BIN: stub(),
		STUB_LOG: join(scratchDir(), `stub-${Math.random().toString(16).slice(2)}.log`),
		STUB_STDOUT: stdout === undefined ? "" : JSON.stringify(stdout),
		STUB_STDERR: stderr,
		STUB_EXIT: String(exit),
	};
}

function sameJson(actual: unknown, expected: unknown, message: string): void {
	assertEq(JSON.stringify(actual), JSON.stringify(expected), message);
}

test("comms_send: a string payload uses --text and returns the answer", async () => {
	const env = invocation({ status: "accepted", sequence: 3 });
	await withEnv(env, async () => {
		const result = await call("comms_send", {
			to: "bob", payload: "hello", node: "n2", requestId: "r1",
		});
		assert(!result.isError, "not an error");
		sameJson(result.structuredContent, { status: "accepted", sequence: 3 },
			"structured answer");
		assertEq(
			recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test send bob --node=n2 --request-id=r1 --text=hello",
			"argv");
	});
});

test("comms_send: a non-string payload travels as positional JSON", async () => {
	const env = invocation({ status: "accepted" });
	await withEnv(env, async () => {
		const result = await call("comms_send", {
			to: "bob", payload: { n: 1 }, requestId: "r2",
		});
		assert(!result.isError, "not an error");
		assertEq(
			recorded(env.STUB_LOG)[0].join(" "),
			'--format json --agent pi-test send bob --request-id=r2 {"n":1}',
			"argv");
	});
});

test("comms_wait: returns the page and cursor", async () => {
	const answer = {
		status: "ok",
		messages: [{ sequence: 5 }],
		cursor: { sequence: 5 },
		more: false,
	};
	const env = invocation(answer);
	await withEnv(env, async () => {
		const result = await call("comms_wait", {
			after: 4, limit: 2, timeoutMs: 1000,
		});
		assert(!result.isError, "not an error");
		sameJson(result.structuredContent, answer, "structured answer");
		sameJson(result.details.answer.cursor, { sequence: 5 }, "cursor kept");
		assertEq(
			recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test wait --after=4 --limit=2 --timeout-ms=1000",
			"argv");
	});
});

test("comms_wait: an empty wait is a page, not an error", async () => {
	const answer = {
		status: "timed-out",
		messages: [],
		cursor: { sequence: 7 },
		more: false,
	};
	const env = invocation(answer, 3);
	await withEnv(env, async () => {
		const result = await call("comms_wait", { timeoutMs: 100 });
		assert(!result.isError, "empty wait is accepted");
		assertEq(result.details.timedOut, true, "marked timed out");
		sameJson(result.structuredContent, answer, "empty page returned");
	});
});

test("comms_replay: after a cursor and with all", async () => {
	const answer = { status: "ok", messages: [], cursor: { sequence: 9 } };
	const env = invocation(answer);
	await withEnv(env, async () => {
		const after = await call("comms_replay", { after: 8, limit: 1 });
		assert(!after.isError, "replay after is not an error");
		const all = await call("comms_replay", { all: true });
		assert(!all.isError, "replay all is not an error");
		const lines = recorded(env.STUB_LOG);
		assertEq(lines[0].join(" "),
			"--format json --agent pi-test replay --after=8 --limit=1", "after argv");
		assertEq(lines[1].join(" "), "--format json --agent pi-test replay --all", "all argv");
	});
});

test("comms_wait: a received page is recorded whole and once on the branch", async () => {
	const page = {
		status: "ok",
		messages: [
			{
				messageId: "m1", from: { agentId: "ann" },
				to: { agentId: "bob" }, sequence: 1, kind: "message",
				payload: "the verdict", acceptedAt: 10, expiresAt: 20,
			},
			{ messageId: "m2", sequence: 2, payload: { n: 1 } },
		],
		cursor: { sequence: 2 },
		more: false,
	};
	const env = invocation(page);
	await withEnv(env, async () => {
		const pi = new FakePi();
		await call("comms_wait", { timeoutMs: 100 }, pi);
		assertEq(pi.session.entries.length, 2, "both envelopes recorded");
		const first = pi.session.entries[0];
		assertEq(first.customType, COMMS_LOG, "the log's own type");
		// Every field the service put on the envelope is still there, so a
		// reader that quotes the verdict quotes the envelope itself.
		sameJson(first.data, {
		...page.messages[0], direction: "received",
		}, "the envelope is recorded whole");
		// A reload reads the branch back, so the same delivery records once.
		await call("comms_wait", { timeoutMs: 100 }, pi);
		assertEq(pi.session.entries.length, 2, "one delivery records once");
	});
});

test("comms_replay: every page of a replay-all is recorded, and repeats once", async () => {
	const pages = [
		{
			status: "ok", cursor: { sequence: 2 }, more: true,
			messages: [
				{ messageId: "m1", sequence: 1, payload: "one" },
				{ messageId: "m2", sequence: 2, payload: "two" },
			],
		},
		{
			status: "ok", cursor: { sequence: 3 }, more: false,
			// The first message repeats across a page boundary: one delivery.
			messages: [
				{ messageId: "m2", sequence: 2, payload: "two" },
				{ messageId: "m3", sequence: 3, payload: "three" },
			],
		},
	];
	const env = invocation(pages);
	await withEnv(env, async () => {
		const pi = new FakePi();
		await call("comms_replay", { all: true }, pi);
		assertEq(pi.session.entries.length, 3, "each message records once");
		assertEq(
			pi.session.entries.map((entry: any) => entry.data.messageId).join(","),
			"m1,m2,m3", "in the order the pages carried them");
	});
});

test("message log: one logger records on each branch that lacks the message, once each", async () => {
	const page = { status: "ok", messages: [{ messageId: "m1" }] };
	const pi = new FakePi();
	const log = new CommsMessageLog(pi as never);
	const ctx = { sessionManager: pi.session } as never;
	// One logger across the whole case: a cache keyed by process would
	// hide what the branch is asked on every call.
	log.record(page, ctx);
	assertEq(pi.session.entries.length, 1, "the first branch records it");
	log.record(page, ctx);
	assertEq(pi.session.entries.length, 1, "the branch that has it records once");
	pi.session.switchBranch();
	log.record(page, ctx);
	assertEq(pi.session.entries.length, 1,
		"another branch is this branch's to record");
});

test("message log: an append the branch refused claims nothing and is retried", async () => {
	const page = { status: "ok", messages: [{ messageId: "m1" }] };
	const pi = new FakePi();
	const log = new CommsMessageLog(pi as never);
	const ctx = { sessionManager: pi.session } as never;
	pi.refuseAppends = 1;
	log.record(page, ctx);
	assertEq(pi.session.entries.length, 0, "nothing was recorded");
	log.record(page, ctx);
	assertEq(pi.session.entries.length, 1, "the same logger retries it");
});

test("comms_ack: an answer that carries no envelope records nothing", async () => {
	const env = invocation({ status: "ok", acknowledged: 1 });
	await withEnv(env, async () => {
		const pi = new FakePi();
		await call("comms_ack", { messageIds: ["m1"] }, pi);
		assertEq(pi.session.entries.length, 0, "no mail, no record");
	});
});

test("comms_ack: message ids are positional", async () => {
	const env = invocation({ status: "ok", acknowledged: 2 });
	await withEnv(env, async () => {
		const result = await call("comms_ack", { messageIds: ["m1", "m2"] });
		assert(!result.isError, "not an error");
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test ack m1 m2", "argv");
	});
});

test("comms_spawn: agent id, request id, absolute directory, then argv", async () => {
	const env = invocation({ status: "spawned", requestId: "r3" });
	await withEnv(env, async () => {
		const result = await call("comms_spawn", {
			agentId: "aid", requestId: "r3", dir: "sub", argv: ["pi", "--version"],
		});
		assert(!result.isError, "not an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(0, 5).join(" "),
			"--format json --agent pi-test spawn", "prefix");
		assertEq(argv[5], "--agent-id=aid", "agent id");
		assertEq(argv[6], "--request-id=r3", "request id");
		assertEq(argv[7], join(scratchDir(), "sub"), "resolved directory");
		assertEq(argv.slice(8).join(" "), "-- pi --version", "argv after --");
		assertEq(result.details.requestId, "r3", "the id is returned");
	});
});

test("comms_spawn: a generated request id is returned so a retry reuses it", async () => {
	const env = invocation({ status: "spawned" });
	await withEnv(env, async () => {
		const result = await call("comms_spawn", {
			dir: "sub", argv: ["pi", "--version"],
		});
		assert(!result.isError, "not an error");
		const id = result.details.requestId;
		assert(typeof id === "string" && id.length > 0, "an id is returned");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv[5], `--request-id=${id}`, "the returned id is the one sent");
	});
});

test("comms_spawn: one task is sent as --task before the directory, with no argv", async () => {
	const env = invocation({ status: "spawned" });
	await withEnv(env, async () => {
		const result = await call("comms_spawn", {
			dir: "sub", task: "audit this", requestId: "r5",
		});
		assert(!result.isError, "not an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(5).join(" "),
			`--request-id=r5 --task=audit this ${join(scratchDir(), "sub")}`,
			"the task, then the directory, and no argv");
	});
});

test("comms_spawn: an argv and a task together are refused before any CLI runs", async () => {
	const env = invocation({ status: "spawned" });
	await withEnv(env, async () => {
		for (const params of [
			{ dir: "sub", argv: ["pi"], task: "audit this" },
			{ dir: "sub" },
		]) {
			const result = await call("comms_spawn", params);
			assertEq(result.isError, true, "is an error");
			assert(result.content[0].text.includes("exactly one of argv or task"),
				"the refusal names the choice");
		}
		assertEq(recorded(env.STUB_LOG).length, 0, "the CLI never ran");
	});
});

test("comms_spawn: an empty argv is no command, so a task is still one task", async () => {
	const env = invocation({ status: "spawned" });
	await withEnv(env, async () => {
		const result = await call("comms_spawn", {
			dir: "sub", argv: [], task: "audit this", requestId: "r6",
		});
		assert(!result.isError, "not an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(5).join(" "),
			`--request-id=r6 --task=audit this ${join(scratchDir(), "sub")}`,
			"the task reached the CLI and no empty argv followed it");
	});
});

test("comms_spawn: a launch nobody can confirm still returns the request id", async () => {
	const env = invocation({ status: "unknown" }, 7, "the connection closed");
	await withEnv(env, async () => {
		const result = await call("comms_spawn", {
			dir: "sub", task: "audit this",
		});
		assertEq(result.isError, true, "is an error");
		const id = result.details.requestId;
		assert(typeof id === "string" && id.length > 0,
			"the id is returned so a retry reuses it");
		assertEq(recorded(env.STUB_LOG)[0][5], `--request-id=${id}`,
			"the returned id is the one it sent");
	});
});

test("comms_send: the kind reaches the CLI", async () => {
	const env = invocation({ status: "accepted" });
	await withEnv(env, async () => {
		await call("comms_send", {
			to: "bob", payload: "hi", kind: "report", requestId: "r7",
		});
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test send bob --kind=report " +
			"--request-id=r7 --text=hi", "argv");
	});
});

test("comms_send: a hosted session names itself when no launch identity was set", async () => {
	const env = invocation({ status: "accepted" });
	env.PI_COMMS_AGENT_ID = "";
	env.PI_HOSTED_SESSION = "pi-hosted";
	await withEnv(env, async () => {
		const result = await call("comms_send", { to: "bob", payload: "hi" });
		assert(!result.isError, "not an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(0, 4).join(" "), "--format json --agent pi-hosted",
			"the hosted name is the identity");
	});
});

test("comms_send: the daemon registry names a session that carries no launch id", async () => {
	const state = join(scratchDir(), "state");
	const session = join(scratchDir(), "session.jsonl");
	mkdirSync(join(state, "sessions"), { recursive: true });
	writeFileSync(join(state, "sessions", "sessions.json"), JSON.stringify({
		files: { other: join(scratchDir(), "other.jsonl"), "pi-registered": session },
	}));
	const env = invocation({ status: "accepted" });
	env.PI_COMMS_AGENT_ID = "";
	env.PI_HOSTED_SESSION = "";
	env.PI_DAEMON_STATE_DIR = state;
	env.PI_SESSION_FILE = session;
	await withEnv(env, async () => {
		const result = await call("comms_send", { to: "bob", payload: "hi" });
		assert(!result.isError, "not an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(0, 4).join(" "), "--format json --agent pi-registered",
			"the registry name is the identity");
	});
});

test("comms_send: no launch id and no registry entry leaves the CLI to refuse", async () => {
	const env = invocation(undefined, 1,
		"pi-comms: an agent id is required: --agent or PI_COMMS_AGENT_ID");
	env.PI_COMMS_AGENT_ID = "";
	env.PI_HOSTED_SESSION = "";
	env.PI_DAEMON_STATE_DIR = "";
	env.PI_SESSION_FILE = "";
	await withEnv(env, async () => {
		const result = await call("comms_send", { to: "bob", payload: "hi" });
		assertEq(result.isError, true, "is an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(0, 4).join(" "), "--format json send bob",
			"the command is sent with no identity");
		assert(!argv.includes("--agent"), "no --agent is invented");
	});
});

test("peers: lists the configured peers", async () => {
	const env = invocation({ status: "ok", peers: [] });
	await withEnv(env, async () => {
		const result = await call("peers", {});
		assert(!result.isError, "not an error");
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json --agent pi-test peer list", "argv");
	});
});

test("failure: a declined send (exit 2) carries stderr and the answer", async () => {
	const answer = { status: "undeliverable", reason: "no-such-peer" };
	const env = invocation(answer, 2, "pi-comms: declined: no-such-peer");
	await withEnv(env, async () => {
		const result = await call("comms_send", {
			to: "bob", payload: "hi", requestId: "r4",
		});
		assertEq(result.isError, true, "is an error");
		assertEq(result.details.exitCode, 2, "exit code kept");
		assert(result.details.stderr.includes("declined"), "stderr kept");
		sameJson(result.details.answer, answer, "answer kept");
		assert(result.content[0].text.includes("exit 2"), "text names the exit");
	});
});

test("failure: no service (exit 4) is a tool error", async () => {
	const env = invocation(undefined, 4, "pi-comms: no service: no live entry");
	await withEnv(env, async () => {
		const result = await call("peers", {});
		assertEq(result.isError, true, "is an error");
		assertEq(result.details.exitCode, 4, "exit code kept");
		assert(result.content[0].text.includes("exit 4"), "text names the exit");
		assert(result.content[0].text.includes("no service"), "text names the code");
	});
});

test("failure: a usage error (exit 1) is a tool error", async () => {
	const env = invocation(undefined, 1,
		"pi-comms: an agent id is required: --agent or PI_COMMS_AGENT_ID");
	await withEnv(env, async () => {
		const result = await call("comms_wait", {});
		assertEq(result.isError, true, "is an error");
		assertEq(result.details.exitCode, 1, "exit code kept");
		assert(result.content[0].text.includes("agent id is required"),
			"stderr carried");
	});
});

test("failure: a missing binary is a tool error, not a throw", async () => {
	await withEnv({
		PI_COMMS_BIN: join(scratchDir(), "absent-pi-comms"),
	}, async () => {
		const result = await call("peers", {});
		assertEq(result.isError, true, "is an error");
		assert(result.content[0].text.includes("not found"), "text names the path");
	});
});

test("argv: send omits absent options and keeps a string payload plain", () => {
	assertEq(CommsArgv.send({ to: "bob", payload: "hi", requestId: "r" }).join(" "),
		"send bob --request-id=r --text=hi", "minimal send argv");
});
