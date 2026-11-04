/**
 * The one run-state publisher: seq ordering, the aborted flag, and the
 * stop-at-session_shutdown rule. Each test takes its own instance; the
 * process's shared one is exercised through the extensions.
 */

import { test, assertEq, assertMatches } from "./harness.ts";
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

test("session-state: a publish carries the state, a rising seq and no abort", async () => {
	const { calls, exec } = recorder();
	await new RunStatePublisher().publish(exec, "pi-a", "busy");
	assertEq(calls.length, 1, "one call");
	assertMatches(calls[0].join(" "), /^state pi-a busy --seq 0$/,
		"state, session, state, seq");
});

test("session-state: seq rises across publishes on one publisher", async () => {
	const { calls, exec } = recorder();
	const pub = new RunStatePublisher();
	await pub.publish(exec, "pi-b", "busy");
	await pub.publish(exec, "pi-b", "idle", true);
	assertEq(Number(calls[1][4]), Number(calls[0][4]) + 1, "monotonic");
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
});

test("session-state: nothing is published after stop (shutdown)", async () => {
	const { calls, exec } = recorder();
	const pub = new RunStatePublisher();
	pub.stop();
	await pub.publish(exec, "pi-e", "idle");
	assertEq(calls.length, 0, "a dying runtime publishes nothing");
});
