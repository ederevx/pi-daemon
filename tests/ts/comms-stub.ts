/**
 * pi-daemon — the stub `pi-comms` CLI and the child-process seam the
 * comms tool tests share.
 *
 * The stub records its whole argv and prints a canned JSON answer with
 * a canned exit code. A per-command script (STUB_ANSWERS) carries a
 * whole scenario: each command's entry list is consumed in order, the
 * last repeating, and STUB_STATE counts the calls per command. A delay
 * (STUB_DELAY) holds each invocation, so waits and aborts are exact.
 */

import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExecOptions, ExecResult } from "@earendil-works/pi-coding-agent";
import { scratchDir } from "./harness.ts";

/** A stub `pi-comms`: log argv, run the script, exit as told. */
export function stub(): string {
	const path = join(scratchDir(), "pi-comms-stub");
	writeFileSync(path, `#!/usr/bin/env python3
import json, os, sys, time
delay = os.environ.get("STUB_DELAY")
if delay:
    time.sleep(float(delay))
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
commands = {"send", "wait", "replay", "ack", "spawn", "peer", "session"}
cmd = None
for arg in sys.argv[1:]:
    if arg in commands:
        cmd = arg
        break
answers = os.environ.get("STUB_ANSWERS")
if answers:
    state_path = os.environ.get("STUB_STATE")
    seen = {}
    if state_path:
        try:
            with open(state_path, encoding="utf-8") as handle:
                seen = json.load(handle)
        except (OSError, ValueError):
            seen = {}
    order = seen.get(cmd or "", 0)
    seen[cmd or ""] = order + 1
    if state_path:
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(seen, handle)
    spec = json.loads(answers).get(cmd or "") or []
    entry = spec[min(order, len(spec) - 1)] if spec else {}
    if "stdout" in entry:
        sys.stdout.write(json.dumps(entry["stdout"]))
    sys.stderr.write(entry.get("stderr", ""))
    sys.exit(entry.get("exit", 0))
sys.stdout.write(os.environ.get("STUB_STDOUT", ""))
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
`, { mode: 0o755 });
	return path;
}

/** The real child-process seam, shaped like pi's `exec`. */
export function execProcess(
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

/** The argv lines the stub recorded, in call order. */
export function recorded(log: string): string[][] {
	let raw = "";
	try {
		raw = readFileSync(log, "utf8");
	} catch {
		return [];
	}
	return raw.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

export const sleep = (ms: number) =>
	new Promise<void>((resolve) => setTimeout(resolve, ms));
