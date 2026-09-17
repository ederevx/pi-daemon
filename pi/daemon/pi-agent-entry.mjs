#!/usr/bin/env node
// pi-daemon's pi-compatible front entry.
//
// The daemon spawns hosted pi sessions and offloaded delegation children
// through this file instead of pi's own entry, so that any process whose
// argv[1] is a pi entry resolves to daemon-owned code. To its caller it
// is indistinguishable from pi:
//
// - Delegation-shaped headless calls (--mode json -p --no-session, the
//   exact signature agent delegations produce) are handed to pi-daemon
//   as agent tickets: the daemon spawns and owns the child, this front
//   relays the child's stdout/stderr verbatim, and exits with the
//   child's exit code. A parent kill becomes a daemon-side cancel.
//   Service down or capacity exhausted -> load pi directly: the call
//   proceeds exactly as an unoffloaded one would.
// - Every other invocation BECOMES pi: the real entry is imported
//   in-process, keeping argv[1] pointed here so delegation children of
//   this process resolve back through the front. argv.slice(2), stdio,
//   tty, signals and env are untouched.
//
// PI_DAEMON_AGENT_CHILD=1 marks a daemon-spawned delegation child: the
// front strips the marker (so its own delegation children offload
// again) and runs pi directly, instead of re-submitting the same task.

import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const REAL_ENTRY = process.env.PI_PTYD_REAL_ENTRY || "@REAL_ENTRY@";
const PI_RC = `${process.env.HOME || ""}/.local/bin/pi-rc`;
const SELF = fileURLToPath(import.meta.url);

const args = process.argv.slice(2);

function isDelegation(a) {
	if (a[0] !== "--mode" || a[1] !== "json") return false;
	if (!a.includes("-p") && !a.includes("--print")) return false;
	if (!a.includes("--no-session")) return false;
	return !a.some((x) =>
		["--resume", "--continue", "-r", "-c", "--session", "--new"].includes(x));
}

/** Runs the real pi entry in-process: this process IS pi from here on. */
async function runPi() {
	delete process.env.PI_DAEMON_AGENT_CHILD;
	try {
		await import(REAL_ENTRY);
		return;
	} catch (err) {
		// The in-process load failed (bad entry bake, broken upgrade):
		// fall back to spawning the real entry as a child so pi stays
		// reachable. A load failure happens before pi runs anything, so
		// no double execution is possible.
		console.error("pi-agent-entry: front load failed, falling back to the real pi:",
			err?.message || err);
		if (!REAL_ENTRY || REAL_ENTRY.startsWith("@") || !existsSync(REAL_ENTRY)) {
			throw err;
		}
		const child = spawn(process.execPath, [REAL_ENTRY, ...args],
			{ stdio: "inherit" });
		for (const sig of ["SIGTERM", "SIGHUP"]) {
			process.on(sig, () => child.kill(sig));
		}
		child.on("exit", (code, signal) =>
			process.exit(signal ? 143 : (code ?? 1)));
		child.on("error", () => process.exit(1));
	}
}

/** Hands a delegation to the daemon and bridges it. Returns null to fall
 *  back to running pi directly when the daemon is unavailable. */
function offload() {
	let submitted;
	try {
		submitted = spawnSync(
			PI_RC,
			["agent-submit", "--session", process.env.PI_HOSTED_SESSION || "",
				"--cwd", process.cwd(), "--pi", SELF, "--", ...args],
			{ encoding: "utf8" },
		);
	} catch {
		return null; // pi-rc vanished
	}
	const id = /^ticket (\S+)$/m.exec((submitted.stdout || "").trim());
	if (submitted.status !== 0 || !id) return null;
	const ticket = id[1];
	process.on("SIGTERM", onSignal);
	process.on("SIGINT", onSignal);
	process.on("SIGHUP", onSignal);
	function onSignal() {
		try {
			spawnSync(PI_RC, ["agent-cancel", ticket], { stdio: "ignore" });
		} catch {
			// daemon unreachable: the ticket is orphaned by design
		}
		process.exit(143);
	}
	// The bridge relays the ticket's stdout/stderr to ours and exits with
	// the child's exit code; caller-visible behavior is byte-identical to
	// a direct `pi --mode json -p` child.
	const bridge = spawn(PI_RC, ["agent-bridge", ticket], { stdio: "inherit" });
	bridge.on("error", () => process.exit(1));
	bridge.on("exit", (code, signal) => {
		process.exit(signal ? 143 : (code ?? 1));
	});
	return undefined; // exiting happens via the bridge handlers
}

(async () => {
	const daemonChild = process.env.PI_DAEMON_AGENT_CHILD === "1";
	if (!daemonChild && isDelegation(args) && existsSync(PI_RC)) {
		const code = offload();
		if (code === null) {
			// Daemon unavailable: run pi directly, unchanged.
			await runPi();
			return;
		}
		if (code !== undefined) process.exit(code);
		return; // the bridge owns the exit from here
	}
	await runPi();
})().catch((err) => {
	// A front-layer failure must not look like anything but a broken pi:
	// surface it on stderr and exit nonzero.
	console.error("pi-agent-entry:", err?.message || err);
	process.exit(1);
});
