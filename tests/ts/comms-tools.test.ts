/**
 * pi-daemon — agent-comms tool tests.
 *
 * The tools are a thin argv surface over `pi/bin/pi-comms`, so the tests
 * drive them against a stub CLI script: it records its argv and prints a
 * canned JSON answer with a canned exit code. Each tool gets one success
 * case, and the CLI's failure vocabulary is covered as tool errors.
 */

import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExecOptions, ExecResult } from "@earendil-works/pi-coding-agent";

import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import { ProcessRunner } from "../../pi/extensions/daemon.ts";
import { CommsCli } from "../../pi/extensions/comms/client.ts";
import { CommsArgv } from "../../pi/extensions/comms/protocol.ts";
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

/** Minimal ExtensionAPI stand-in, as the other extension tests use. */
class FakePi {
	readonly tools = new Map<string, any>();

	registerTool(definition: any): void {
		this.tools.set(definition.name, definition);
	}
}

/** Run one tool handler through the stub CLI. */
async function call(
	name: string,
	params: Record<string, unknown>,
): Promise<any> {
	const pi = new FakePi();
	new CommsTools(new CommsCli(new ProcessRunner(execProcess))).register(pi as never);
	const tool = pi.tools.get(name);
	return tool.execute("call1", params, undefined, undefined, {
		cwd: scratchDir(),
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
			"--format json send bob --node=n2 --request-id=r1 --text=hello",
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
			'--format json send bob --request-id=r2 {"n":1}',
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
			"--format json wait --after=4 --limit=2 --timeout-ms=1000",
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
			"--format json replay --after=8 --limit=1", "after argv");
		assertEq(lines[1].join(" "), "--format json replay --all", "all argv");
	});
});

test("comms_ack: message ids are positional", async () => {
	const env = invocation({ status: "ok", acknowledged: 2 });
	await withEnv(env, async () => {
		const result = await call("comms_ack", { messageIds: ["m1", "m2"] });
		assert(!result.isError, "not an error");
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json ack m1 m2", "argv");
	});
});

test("comms_spawn: agent id, absolute directory, then argv", async () => {
	const env = invocation({ status: "spawned", requestId: "r3" });
	await withEnv(env, async () => {
		const result = await call("comms_spawn", {
			agentId: "aid", dir: "sub", argv: ["pi", "--version"],
		});
		assert(!result.isError, "not an error");
		const argv = recorded(env.STUB_LOG)[0];
		assertEq(argv.slice(0, 3).join(" "),
			"--format json spawn", "prefix");
		assertEq(argv[3], "--agent-id=aid", "agent id");
		assertEq(argv[4], join(scratchDir(), "sub"), "resolved directory");
		assertEq(argv.slice(5).join(" "), "-- pi --version", "argv after --");
	});
});

test("peers: lists the configured peers", async () => {
	const env = invocation({ status: "ok", peers: [] });
	await withEnv(env, async () => {
		const result = await call("peers", {});
		assert(!result.isError, "not an error");
		assertEq(recorded(env.STUB_LOG)[0].join(" "),
			"--format json peer list", "argv");
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
