/**
 * The one run-state publisher: seq ordering, the aborted flag, and the
 * stop-at-session_shutdown rule.
 *
 * The counter and the stop flag are process-wide on purpose (pi loads
 * each extension as its own module instance), so this file runs last in
 * the suite and its stop test runs last in the file: once stopped, a
 * publish stays silent for the rest of the process.
 */

import { test, assert, assertEq, assertMatches } from "./harness.ts";
import { RunStatePublisher } from "../../pi/extensions/session-state/publisher.ts";

function recorder(): {
	calls: string[][];
	exec: (a: string[]) => Promise<void>;
} {
	const calls: string[][] = [];
	return {
		calls,
		exec: async (args: string[]) => {
			calls.push(args);
		},
	};
}

test("session-state: a publish carries the state, a seq and no abort", async () => {
	const { calls, exec } = recorder();
	await new RunStatePublisher().publish(exec, "pi-a", "busy");
	assertEq(calls.length, 1, "one call");
	assertMatches(calls[0].join(" "), /^state pi-a busy --seq \d+$/,
		"state, session, state, seq");
});

test("session-state: two publishers share one counter", async () => {
	// pi loads daemon.ts and offload.ts as separate module instances, so a
	// module-level singleton would give each its own counter and one
	// publisher's stamps would sink under the other's for good.
	const { calls, exec } = recorder();
	await new RunStatePublisher().publish(exec, "pi-b", "busy");
	await new RunStatePublisher().publish(exec, "pi-b", "idle", true);
	assertEq(Number(calls[1][4]), Number(calls[0][4]) + 1,
		"one counter for the process");
});

test("session-state: an aborted idle publish says so", async () => {
	const { calls, exec } = recorder();
	await new RunStatePublisher().publish(exec, "pi-c", "idle", true);
	assertMatches(calls[0].join(" "), / --aborted$/, "abort flag rides along");
});

test("session-state: a failing publish never throws", async () => {
	await new RunStatePublisher().publish(async () => {
		throw new Error("daemon down");
	}, "pi-d", "idle");
	// A host that throws before it has a promise to reject: the caller is a
	// turn about to work, so the publish must stay silent, not abort it.
	await new RunStatePublisher().publish(() => {
		throw new Error("no such command");
	}, "pi-d", "idle");
});

// Last: the stop is process-wide on purpose.
test("session-state: nothing is published after stop (shutdown)", async () => {
	const { calls, exec } = recorder();
	const first = new RunStatePublisher();
	first.stop();
	// A second instance is the other extension's copy of this module: the
	// stop reaches it too, or one extension would keep publishing a dying
	// run as idle.
	await new RunStatePublisher().publish(exec, "pi-e", "idle");
	await first.publish(exec, "pi-e", "idle");
	assertEq(calls.length, 0, "a dying runtime publishes nothing");
	assert(true, "silent");
});
