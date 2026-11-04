/**
 * The one run-state publisher: seq ordering, the aborted flag, and the
 * stop-at-session_shutdown rule.
 *
 * The counter and the stop flag are process-wide on purpose (pi loads
 * each extension as its own module instance), so this file runs last in
 * the suite and its stop test runs last in the file.
 */

import { test, assert, assertEq, assertMatches } from "./harness.ts";
import {
	RunStatePublisher,
	SharedState,
} from "../../pi/extensions/session-state/publisher.ts";

/** An isolated counter and stop flag: the shared one is process-wide. */
function publisher(): RunStatePublisher {
	return new RunStatePublisher(new SharedState());
}

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
	await publisher().publish(exec, "pi-a", "busy");
	assertEq(calls.length, 1, "one call");
	assertMatches(calls[0].join(" "), /^state pi-a busy --seq \d+$/,
		"state, session, state, seq");
});

test("session-state: two publishers share one counter", async () => {
	// pi loads daemon.ts and offload.ts as separate module instances, so a
	// module-level singleton would give each its own counter and one
	// publisher's stamps would sink under the other's for good.
	const { calls, exec } = recorder();
	// One SharedState stands for the one globalThis object both module
	// copies reach through the registered symbol.
	const state = new SharedState();
	await new RunStatePublisher(state).publish(exec, "pi-b", "busy");
	await new RunStatePublisher(state).publish(exec, "pi-b", "idle", true);
	assertEq(Number(calls[1][4]), Number(calls[0][4]) + 1,
		"one counter for the process");
});

test("session-state: an aborted idle publish says so", async () => {
	const { calls, exec } = recorder();
	await publisher().publish(exec, "pi-c", "idle", true);
	assertMatches(calls[0].join(" "), / --aborted$/, "abort flag rides along");
});

test("session-state: a failing publish never throws", async () => {
	await publisher().publish(async () => {
		throw new Error("daemon down");
	}, "pi-d", "idle");
	// A host that throws before it has a promise to reject: the caller is a
	// turn about to work, so the publish must stay silent, not abort it.
	await publisher().publish(() => {
		throw new Error("no such command");
	}, "pi-d", "idle");
});

test("session-state: a quit stops every publisher", async () => {
	const { calls, exec } = recorder();
	const state = new SharedState();
	const first = new RunStatePublisher(state);
	first.stop("quit");
	// A second instance is the other extension's copy of this module: the
	// stop reaches it too, or one extension would keep publishing a dying
	// run as idle.
	await new RunStatePublisher(state).publish(exec, "pi-e", "idle");
	await first.publish(exec, "pi-e", "idle");
	assertEq(calls.length, 0, "a dying runtime publishes nothing");
});

test("session-state: a reload keeps publishing, with one climbing seq", async () => {
	const { calls, exec } = recorder();
	const state = new SharedState();
	// A reload or a session switch replaces the runtime inside the same
	// process. Stopping there would silence every later turn for the life
	// of the process, and an idle record is what a death then reads as.
	for (const reason of ["reload", "new", "resume", "fork"]) {
		new RunStatePublisher(state).stop(reason);
		await new RunStatePublisher(state).publish(exec, "pi-f", "busy");
	}
	assertEq(calls.length, 4, "the replacement runtime publishes");
	assertEq(calls.map((c) => Number(c[4])).join(","), "0,1,2,3",
		"seq keeps climbing, so no publisher is dropped as stale");
	assertEq(state.stopped, false, "a reload never silences the process");
});
