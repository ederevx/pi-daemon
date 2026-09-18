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

/** How many ms the bridge keeps the spawner waiting on a ticket before
 *  handing it off to the daemon. 0 disables the hand-off (always bridge).
 *  The env name mirrors PI_OFFLOAD_WAIT used for shell offloading. */
const AGENT_BRIDGE_WAIT_MS = (() => {
	const raw = Number(process.env.PI_AGENT_OFFLOAD_WAIT || 20000);
	return Number.isFinite(raw) && raw >= 0 ? raw : 20000;
})();

/** Bridges a ticket: relays its stdout/stderr to ours and exits with the
 *  child's exit code. Caller-visible behavior is byte-identical to a
 *  direct `pi --mode json -p` child. */
function bridgeTicket(ticket) {
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
	const bridge = spawn(PI_RC, ["agent-bridge", ticket], { stdio: "inherit" });
	let handedOff = false;
	let bridgeExited = false;
	bridge.on("error", () => process.exit(1));
	bridge.on("exit", (code, signal) => {
		bridgeExited = true;
		if (handedOff) return;
		process.exit(signal ? 143 : (code ?? 1));
	});
	if (AGENT_BRIDGE_WAIT_MS > 0) {
		// Delegate completion to the ticket side of the hand-off: wait up
		// to the bound for the run to finish inline; if it is still going,
		// hand it off to the daemon and return now. The spawner (ADP) only
		// ever observes a child that completed, so it stays agnostic.
		const detachAt = setTimeout(() => {
			if (bridgeExited) return;
			handedOff = true;
			try {
				spawnSync(PI_RC, ["agent-detach", ticket], { stdio: "ignore" });
			} catch {
				// daemon unreachable: nothing left to mark
			}
			process.stdout.write(
				`{"type":"agent_handoff","ticket":"${ticket}"}\n`);
			try {
				bridge.kill("SIGTERM");
			} catch {
				// already gone
			}
			process.exit(0);
		}, AGENT_BRIDGE_WAIT_MS);
		// The happy path must clear the timer so a run that finishes right
		// at the bound does not double-exit.
		bridge.on("exit", () => clearTimeout(detachAt));
	}
	return undefined; // exiting happens via the bridge handlers
}

/** Ambiguous agent-submit recovery: find an agent ticket created recently
 *  whose command ends with our exact pi args, so a lost submit reply
 *  never leads to re-running the delegation. Scoped to the owning
 *  session so a parallel sibling can never be mistaken for ours. */
function adoptRecentAgent(sessKey) {
	try {
		const listArgs = ["agent-list"];
		if (sessKey) listArgs.push(sessKey);
		const listed = spawnSync(PI_RC, listArgs, { encoding: "utf8" });
		if (listed.status !== 0) return null;
		const cutoff = Date.now() / 1000 - 60;
		const suffix = " " + args.join(" ");
		const matches = (listed.stdout || "").trim().split("\n").filter(Boolean)
			.map((line) => JSON.parse(line))
			.filter((t) => typeof t.command === "string"
				&& t.command.endsWith(suffix) && (t.created ?? 0) >= cutoff);
		return matches.length ? matches[matches.length - 1].id : null;
	} catch {
		return null;
	}
}

/** Hands a delegation to the daemon and bridges it. Returns null to fall
 *  back to running pi directly when the daemon is unavailable or the
 *  submit was deterministically refused. Exit 7 (ambiguous - the request
 *  was sent but the outcome is unknown) adopts a matching recent ticket
 *  instead of ever re-running the delegation. */
function offload() {
	let submitted;
	try {
		// The ticket's owning session for completion routing: the hosted
		// session key survives ADP's PI_HOSTED_* stripping because the
		// session holds it under PI_PTYD_SESSKEY, then PI_HOSTED_SESSION,
		// then the session-file stem for plain runs.
		const sessKey = process.env.PI_PTYD_SESSKEY
			|| process.env.PI_HOSTED_SESSION
			|| (process.env.PI_SESSION_FILE || "").split("/").pop()
				?.replace(/\.jsonl$/, "") || "";
		submitted = spawnSync(
			PI_RC,
			["agent-submit", "--session", sessKey,
				"--cwd", process.cwd(), "--pi", SELF, "--", ...args],
			{ encoding: "utf8" },
		);
	} catch {
		return null; // pi-rc vanished
	}
	const id = /^ticket (\S+)$/m.exec((submitted.stdout || "").trim());
	if (submitted.status === 0 && id) return bridgeTicket(id[1]);
	if (submitted.status === 7) {
		const adopted = adoptRecentAgent(sessKey);
		if (adopted) return bridgeTicket(adopted);
		// The submit's outcome is unknowable: the daemon may already be
		// running this delegation. Running pi locally would duplicate it,
		// so refuse instead - the ticket (if it landed) completes in the
		// daemon and the parent session can see it via daemon-subagents;
		// if it never started, the caller simply re-invokes.
		console.error(
			"pi-agent-entry: submit outcome unknown (connection lost); " +
			"NOT running the delegation locally to avoid duplication. " +
			"The daemon may already hold it (see daemon-subagents).");
		process.exit(3);
	}
	return null; // unreachable or deterministically refused: safe to run pi
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
