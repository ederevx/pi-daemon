/**
 * The one run-state publisher: seq ordering, the abort flag, and the
 * stop-at-a-quit rule.
 *
 * The counter and the stop flag are process-wide on purpose (pi loads each
 * extension as its own module instance), so one test here drives two copies
 * of this module at once; the rest build their own SharedState.
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
	assertMatches(calls[0].join(" "), /^state pi-a busy --seq \d+ --pid \d+$/,
		"state, session, state, seq, the publishing process");
});

test("session-state: two publishers share one counter", async () => {
	// pi loads daemon.ts and offload.ts as separate module instances, so a
	// module-level singleton would give each its own counter and one
	// publisher's stamps would sink under the other's for good. A
	// cache-busting specifier is that same duplication inside one process.
	const copy = await import(
		"../../pi/extensions/session-state/publisher.ts?copy=2");
	assert(copy.RunStatePublisher !== RunStatePublisher, "two module copies");
	const { calls, exec } = recorder();
	// One SharedState stands for the one globalThis object both module
	// copies reach through the registered symbol.
	await copy.RunStatePublisher.shared.publish(exec, "pi-b", "busy");
	await RunStatePublisher.shared.publish(exec, "pi-b", "idle", true);
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
	// The copy is the other extension's instance of this module: the stop
	// must reach it too, or one extension keeps publishing a dying run as
	// idle. Lifted afterwards, so the flag guards the session that quit and
	// not the process it lived in.
	const copy = await import(
		"../../pi/extensions/session-state/publisher.ts?copy=2");
	const { calls, exec } = recorder();
	copy.RunStatePublisher.shared.stop("quit");
	await RunStatePublisher.shared.publish(exec, "pi-e", "idle");
	assertEq(calls.length, 0, "a dying runtime publishes nothing");
	await RunStatePublisher.shared.publish(exec, "pi-e", "idle", true);
	assertEq(calls.length, 0, "nor does an aborted one");
	RunStatePublisher.shared.resume();
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
