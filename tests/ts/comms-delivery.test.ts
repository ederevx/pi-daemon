/**
 * pi-daemon � the background receiver and the acknowledgement ledger.
 *
 * The receiver is the loop over `pi-comms wait` that turns every answer
 * into session entries, an armed acknowledgement, and the durable
 * receive cursor. The ledger owns the unacknowledged-mail gate and the
 * reply-then-acknowledge turn. Both run over the scripted stub CLI, so
 * the answers are exact and every hold's argv is observable.
 */

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import { ProcessRunner } from "../../pi/extensions/daemon.ts";
import { CommsCli } from "../../pi/extensions/comms/client.ts";
import {
	ACK_KIND,
	ACK_TYPE,
	ACK_VERSION,
	CommsExit,
} from "../../pi/extensions/comms/protocol.ts";
import {
	COMMS_CURSOR,
	COMMS_DELIVERY,
	CommsDelivery,
	type DeliveryPolicy,
} from "../../pi/extensions/comms/delivery.ts";
import {
	COMMS_ACK_STATE,
	CommsAcknowledgements,
} from "../../pi/extensions/comms/acknowledgements.ts";
import { COMMS_LOG, CommsMessageLog } from "../../pi/extensions/comms/message-log.ts";
import { ToolGateEscapes } from "../../pi/extensions/guards/tool-gate-escapes.ts";
import { execProcess, recorded, sleep, stub } from "./comms-stub.ts";

/** The receiver's timing: short retries, a long hold the tests cancel. */
const policy: DeliveryPolicy = {
	holdMs: 60_000,
	retryInitialMs: 20,
	retryMaxMs: 40,
};

/** The session stand-in: entries on one branch, appends the branch can
 *  refuse by entry type, and the messages the receiver steers. */
class FakePi {
	readonly session = { entries: [] as Array<Record<string, unknown>> };
	/** Entry types the branch refuses before taking, as a full store. */
	readonly failOn = new Set<string>();
	readonly messages: Array<Record<string, unknown>> = [];
	readonly handlers = new Map<
		string,
		Array<(event: unknown, ctx: unknown) => unknown>
	>();
	/** The tools registered on this fake API, by name, with the metadata a
	 *  gate reads back through `getAllTools`. */
	readonly tools = new Map<string, {
		name: string;
		annotations?: Record<string, unknown>;
		execute: (...args: unknown[]) => Promise<unknown>;
	}>();

	registerTool(spec: {
		name: string;
		annotations?: Record<string, unknown>;
		execute: (...args: unknown[]) => Promise<unknown>;
	}): void {
		this.tools.set(spec.name, spec);
	}

	/** What pi hands a gate: every registered tool and its annotations. */
	getAllTools(): Array<{ name: string; annotations?: Record<string, unknown> }> {
		return [...this.tools.values()].map((tool) => ({
			name: tool.name,
			...(tool.annotations ? { annotations: tool.annotations } : {}),
		}));
	}

	appendEntry(customType: string, data: unknown): void {
		if (this.failOn.has(customType)) {
			throw new Error("the branch refused the entry");
		}
		this.session.entries.push({ type: "custom", customType, data });
	}

	sendMessage(message: Record<string, unknown>, options: Record<string, unknown>): void {
		this.messages.push({ ...message, ...options });
	}

	on(name: string, handler: (event: unknown, ctx: unknown) => unknown): void {
		const list = this.handlers.get(name) ?? [];
		list.push(handler);
		this.handlers.set(name, list);
	}

	/** pi awaits every handler in registration order and stops at the first
	 *  one that blocks, with the session context. */
	async fire(name: string, event: unknown): Promise<unknown> {
		let answer: unknown;
		for (const handler of this.handlers.get(name) ?? []) {
			const returned = await handler(event, this.ctx());
			if (returned === undefined || returned === null) continue;
			answer = returned;
			if ((returned as { block?: unknown }).block) return returned;
		}
		return answer;
	}

	/** The session context the delivery attaches to. */
	ctx = () => ({
		sessionManager: {
			getEntries: () => this.session.entries,
			getBranch: () => this.session.entries,
		},
	});
}

/** One wired receiver against one branch, with its ledger. The attach
 *  order matches the extension's session start: log, ledger, then
 *  delivery, so restored obligations steer before the tools run. */
function harness(pi: FakePi): {
	delivery: CommsDelivery;
	ledger: CommsAcknowledgements;
} {
	const cli = new CommsCli(new ProcessRunner(execProcess));
	const log = new CommsMessageLog(pi as never);
	const ledger = new CommsAcknowledgements(cli, log, pi as never);
	const delivery = new CommsDelivery(
		cli, log, pi as never, ledger, policy);
	const ctx = pi.ctx();
	log.attach(ctx);
	ledger.attach(ctx);
	delivery.attach(ctx);
	return { delivery, ledger };
}

/** A script for the stub: per command, entries consumed in order, the
 *  last repeating. */
function scripted(spec: Record<string, Array<Record<string, unknown>>>): {
	env: Record<string, string>;
	script: (over: Record<string, string>) => Record<string, string>;
} {
	const env: Record<string, string> = {
		PI_COMMS_AGENT_ID: "pi-test",
		PI_COMMS_BIN: stub(),
		STUB_LOG: join(scratchDir(), `stub-${Math.random().toString(16).slice(2)}.log`),
		STUB_ANSWERS: JSON.stringify(spec),
		STUB_STATE: join(scratchDir(), `state-${Math.random().toString(16).slice(2)}.json`),
	};
	return {
		env,
		script: (over) => ({ ...env, ...over }),
	};
}

const envelope = (id: string, sequence: number, payload: unknown, kind = "message") => ({
	messageId: id,
	from: { agentId: "ann" },
	to: { agentId: "pi-test" },
	sequence,
	kind,
	payload,
});

function sameJson(actual: unknown, expected: unknown, message: string): void {
	assertEq(JSON.stringify(actual), JSON.stringify(expected), message);
}

// -- ingestion ------------------------------------------------------------

test("delivery: a page presents each new envelope once and moves the cursor when recorded", async () => {
	const pi = new FakePi();
	const { delivery, ledger } = harness(pi);
	const page = {
		status: "ok",
		messages: [envelope("m1", 1, "one"), envelope("m2", 2, { n: 1 })],
		cursor: { sequence: 2 },
	};
	const report = await delivery.ingest(page, { steer: false });
	assertEq(report.presented.join(","), "m1,m2", "both presented once");
	assertEq(report.cursor, 2, "the cursor the page reached");
	assertEq(ledger.pending().map((record) => record.messageId).join(","),
		"m1,m2", "each envelope armed the gate");
	const again = await delivery.ingest(page, { steer: false });
	assertEq(again.presented.length, 0, "a repeat presents nothing");
	assertEq(again.cursor, 2, "an already-recorded page keeps its cursor");
	assertEq(pi.session.entries.filter((entry) =>
		entry.customType === COMMS_LOG).length, 2, "each envelope logged once");
});

test("delivery: a receipt the branch cannot persist holds the cursor back", async () => {
	const pi = new FakePi();
	const { delivery } = harness(pi);
	pi.failOn.add(COMMS_ACK_STATE);
	const page = {
		status: "ok",
		messages: [envelope("m1", 1, "one"), envelope("m2", 2, "two")],
		cursor: { sequence: 2 },
	};
	const report = await delivery.ingest(page, { steer: false });
	assertEq(report.presented.join(","), "m1,m2", "the page itself was seen");
	assertEq(report.cursor, undefined, "no cursor advance");
	assertEq(delivery.latched(), true, "the fault latched");
	assertEq(ledgerPendingIds(pi).length, 0, "nothing was recorded durably");
});

test("delivery: a protocol acknowledgement is recorded and service-acked, never gated", async () => {
	const spec = scripted({ ack: [{ stdout: { status: "ok", acknowledged: 1 } }] });
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { delivery, ledger } = harness(pi);
		const ackEnvelope = envelope("ma", 5, {
			type: ACK_TYPE, version: ACK_VERSION, messageId: "m0",
		}, ACK_KIND);
		const page = {
			status: "ok",
			messages: [ackEnvelope],
			cursor: { sequence: 5 },
		};
		const report = await delivery.ingest(page, { steer: true });
		assertEq(report.presented.length, 0, "no presentation, no gate");
		assertEq(ledger.owing(), false, "nothing owed");
		assertEq(recorded(spec.env.STUB_LOG)[0].slice(-2).join(" "),
			"ack ma", "the service acknowledgement ran");
		assertEq(pi.messages.length, 0, "no steer for bookkeeping");
	});
});

function ledgerPendingIds(pi: FakePi): string[] {
	return pi.session.entries.filter((entry) =>
		entry.customType === COMMS_ACK_STATE)
		.map((entry) => (entry.data as Record<string, unknown>).messageId as string);
}

// -- the receiver loop ----------------------------------------------------

test("delivery: the loop holds from the restore cursor, delivers, and advances", async () => {
	const spec = scripted({
		wait: [
			{
				stdout: {
					status: "ok",
					messages: [envelope("m2", 2, "two"), envelope("m3", 3, "three")],
					cursor: { sequence: 3 },
				},
			},
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		pi.appendEntry(COMMS_CURSOR, { mailbox: "@pi-test", sequence: 1 });
		const { delivery, ledger } = harness(pi);
		assertEq(delivery.start(), true, "the receiver started");
		// The first hold answers, the second stops the loop for good: wait
		// for its log line instead of a fixed interval, which flakes under
		// full-suite load.
		let second = false;
		for (let i = 0; i < 100 && !second; i++) {
			await sleep(50);
			second = recorded(spec.env.STUB_LOG)
				.some((line) => line.includes("--after=3"));
		}
		assert(second, "a later hold started from the delivered page");
		await delivery.stop();
		assertEq(delivery.active(), false, "the receiver stopped on the failure");
		const lines = recorded(spec.env.STUB_LOG);
		assert(lines[0].includes("--after=1"),
			"the hold continued from the session's own cursor");
		assert(lines.some((line) => line.includes("--after=3")),
			"a later hold continued from the delivered page");
		const received = pi.session.entries.filter((entry) =>
			entry.customType === COMMS_LOG);
		assertEq(received.map((entry) =>
			(entry.data as Record<string, unknown>).messageId).join(","),
			"m2,m3", "both envelopes recorded");
		assertEq(new Set(ledgerPendingIds(pi)).size, 2, "both armed");
		const steered = pi.messages.filter((message) =>
			message.customType === COMMS_DELIVERY && message.triggerTurn === true);
		assertEq(steered.length, 2, "one steer per new message");
		const first = steered[0];
		assertEq(first.triggerTurn, true, "the steer starts a turn");
		assert((first.content as string).includes('comms_ack { messageIds: ["m2"] }'),
			"the steer names the exact acknowledgement");
		const cursor = pi.session.entries.filter((entry) =>
			entry.customType === COMMS_CURSOR);
		assertEq(cursor.length, 2, "the restored cursor and one advance");
		sameJson(cursor[1].data, { mailbox: "@pi-test", sequence: 3 },
			"the cursor is durable per mailbox");
		assertEq(ledger.owing(), true, "the delivered mail still owes its ack");
	});
});

test("delivery: an expired cursor restarts from the oldest surviving sequence", async () => {
	const spec = scripted({
		wait: [
			{
				stdout: { status: "cursor-expired", oldestAvailableSequence: 40 },
				exit: 2,
			},
			{
				stdout: {
					status: "ok",
					messages: [envelope("m10", 42, "fresh")],
					cursor: { sequence: 42 },
				},
			},
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		pi.appendEntry(COMMS_CURSOR, { mailbox: "@pi-test", sequence: 45 });
		const { delivery } = harness(pi);
		delivery.start();
		await sleep(400);
		await delivery.stop();
		const lines = recorded(spec.env.STUB_LOG);
		assert(lines[0].includes("--after=45"), "the first hold used the cursor");
		assert(lines.some((line) => line.includes("--after=39")),
			"the next hold restarted at the oldest survivor minus one");
		const gap = pi.messages.filter((message) =>
			message.customType === COMMS_DELIVERY &&
			String(message.content).includes("no longer retained"));
		assertEq(gap.length, 1, "the gap is said once");
	});
});

test("delivery: a permanent failure stops the receiver once, with the reason", async () => {
	const spec = scripted({
		wait: [{ stderr: "pi-comms: unauthorized: agent identity rejected", exit: 6 }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { delivery } = harness(pi);
		delivery.start();
		await sleep(150);
		await delivery.stop();
		assertEq(delivery.active(), false, "stopped for good");
		const stopped = pi.messages.filter((message) =>
			message.customType === COMMS_DELIVERY &&
			String(message.content).includes("reception stopped"));
		assertEq(stopped.length, 1, "said once");
		assert(String(stopped[0].content).includes("comms_wait still works"),
			"the reason says what still works");
		await sleep(200);
		assertEq(pi.messages.length, 1, "no repeat after the stop");
	});
});

// -- the ledger -----------------------------------------------------------

function arm(pi: FakePi, ledger: CommsAcknowledgements, id: string): void {
	ledger.markPending(envelope(id, 1, "hello"));
}

/** One delivered envelope the branch recorded and the ledger armed, both
 *  naming the mailbox it arrived in. */
function receiveAt(
	pi: FakePi,
	ledger: CommsAcknowledgements,
	id: string,
	nodeId: string,
): void {
	const delivered = { ...envelope(id, 1, "hello"),
		to: { agentId: "pi-test", nodeId } };
	pi.appendEntry(COMMS_LOG, { ...delivered, direction: "received" });
	ledger.markPending(delivered);
}

test("acknowledgements: the armed gate admits only live valid escapes", async () => {
	const pi = new FakePi();
	const { ledger } = harness(pi);
	arm(pi, ledger, "m1");
	ledger.register(pi as never);
	const answer = (name: string) => pi.fire("tool_call", { toolName: name }) as
		Promise<{ block?: boolean; reason?: string } | undefined>;

	// Baseline: nothing declares an escape, so only the ack and the
	// daemon's own calls run, and the denial names the id and the call.
	const blocked = await answer("bash");
	assertEq(blocked?.block, true, "an ordinary tool is blocked");
	assert(blocked?.reason?.includes("m1") === true,
		"the denial names the owed ids");
	assert(blocked?.reason?.includes("comms_ack { messageIds: [\"m1\"] }"),
		"the denial names the exact call");
	assertEq(await answer("comms_ack"), undefined, "the ack itself runs");
	assertEq(await answer("comms_replay"), undefined,
		"the replay its report names runs");
	assertEq((await answer("comms_wait"))?.block, true,
		"the foreground hold stays blocked");
	assertEq(await answer("pre_daemon"), undefined,
		"the daemon's own calls run");

	// One registration declares the acquisition its own prerequisite
	// needs; every other tool stays blocked behind the owed id.
	declare(pi, ["pre_memory", "read", "rm_tree_exec"]);
	for (const name of ["pre_memory", "read", "rm_tree_exec"]) {
		assertEq(await answer(name), undefined,
			`${name} is admitted while the acknowledgement is owed`);
	}
	for (const name of ["write", "bash", "comms_send", "memory_update",
		"unknown_tool"]) {
		const denial = await answer(name);
		assertEq(denial?.block, true, `${name} stays blocked`);
		assert(denial?.reason?.includes("m1") === true,
			`${name}'s denial names the owed id`);
	}
	assertEq(ledger.owing(), true, "an admission settles nothing by itself");

	// The union follows the live registrations: a second declaring tool
	// adds its own admission, and removing either closes only its own.
	declare(pi, ["pre_consult"], "pre_consult");
	assertEq(await answer("pre_consult"), undefined,
		"a second declaration admits its own tool");
	assertEq(await answer("read"), undefined,
		"the first declaration still admits");
	pi.tools.delete("pre_consult");
	assertEq((await answer("pre_consult"))?.block, true,
		"removing that registration closes its admission");
	assertEq(await answer("read"), undefined,
		"the other declaration is untouched");
	pi.tools.delete("pre_memory");
	assertEq((await answer("read"))?.block, true,
		"removing the declaring registration closes its admissions");

	// A malformed declaration admits nothing, and no other declaration in
	// the session may rescue the probed tool.
	const key = ToolGateEscapes.KEY;
	const version = ToolGateEscapes.VERSION;
	const inheritedVersion = Object.assign(
		Object.create({ version }), { admits: ["read"] });
	const inheritedAdmits = Object.assign(
		Object.create({ admits: ["read"] }), { version });
	const cases: Array<[string, Record<string, unknown> | undefined]> = [
		["a future version", { [key]: { version: version + 1, admits: ["read"] } }],
		["no version", { [key]: { admits: ["read"] } }],
		["null", { [key]: null }],
		["an array", { [key]: ["read"] }],
		["false", { [key]: false }],
		["a string", { [key]: "true" }],
		["no admits", { [key]: { version } }],
		["admits that is not a list", { [key]: { version, admits: "read" } }],
		["an inherited declaration", Object.create({
			[key]: { version, admits: ["read"] },
		}) as Record<string, unknown>],
		["an inherited version", { [key]: inheritedVersion }],
		["an inherited admits", { [key]: inheritedAdmits }],
	];
	for (const [label, annotations] of cases) {
		pi.registerTool({ name: "pre_memory", annotations, execute: async () => ({}) });
		assertEq((await answer("read"))?.block, true,
			`${label} admits nothing`);
	}

	// A registry the gate cannot read admits nothing, and the declaration
	// admits again as soon as the registry answers.
	declare(pi, ["read"]);
	const registry = pi.getAllTools.bind(pi);
	pi.getAllTools = () => {
		throw new Error("the registry is unavailable");
	};
	try {
		assertEq((await answer("read"))?.block, true,
			"an unreadable registry admits nothing");
	} finally {
		pi.getAllTools = registry;
	}
	assertEq(await answer("read"), undefined,
		"the declaration admits once the registry reads again");
	assertEq(ledger.owing(), true, "the mail is still owed through every phase");
});

/** Register a tool whose extension declares the escape a blocked session
 *  may still take, as agent-mem-struct's and pi-consult's will. */
function declare(
	pi: FakePi,
	admits: string[],
	name = "pre_memory",
): void {
	pi.registerTool({
		name,
		annotations: {
			[ToolGateEscapes.KEY]: {
				version: ToolGateEscapes.VERSION,
				admits,
			},
		},
		execute: async () => ({ content: [] }),
	});
}

test("acknowledgements: one ack answers the sender, records, and settles the gate", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted", sequence: 2 } }],
		ack: [{ stdout: { status: "ok", acknowledged: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		arm(pi, ledger, "m1");
		const report = await ledger.acknowledge(["m1"], {}, undefined);
		sameJson({
			acknowledged: report.acknowledged,
			alreadyAcknowledged: report.alreadyAcknowledged,
			unknown: report.unknown,
			replyFailed: report.replyFailed,
			serviceAckFailed: report.serviceAckFailed,
		}, {
			acknowledged: ["m1"],
			alreadyAcknowledged: [],
			unknown: [],
			replyFailed: [],
			serviceAckFailed: [],
		}, "the whole id settled in one turn");
		assertEq(ledger.owing(), false, "the gate opened");
		const lines = recorded(spec.env.STUB_LOG);
		assertEq(lines.length, 2, "one reply, one service acknowledgement");
		const joined = lines[0].join(" ");
		assert(joined.includes("send ann"), "the reply went to the sender");
		assert(joined.includes("--kind=ack"), "the reply is of the ack kind");
		assert(lines[1].join(" ").endsWith("ack m1"),
			"the service acknowledgement is positional");
		const entries = ledgerPendingIds(pi);
		assertEq(entries.length, 3,
			"the arm, the reply progress, and the completion, all durable");
	});
});

test("acknowledgements: an id this session never received is never just acked", async () => {
	const spec = scripted({ send: [], ack: [] });
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		arm(pi, ledger, "m1");
		const report = await ledger.acknowledge(["mx"], {}, undefined);
		assertEq(report.unknown.join(","), "mx", "one unknown id");
		assertEq(recorded(spec.env.STUB_LOG).length, 0, "no CLI ran");
		assertEq(ledger.owing(), true, "nothing changed");
	});
});

test("acknowledgements: a refused reply is indicated and the service ack still settles", async () => {
	const spec = scripted({
		send: [
			{ stderr: "pi-comms: declined: no-such-peer", exit: 2 },
			{ stdout: { status: "accepted" } },
		],
		ack: [{ stdout: { status: "ok", acknowledged: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		arm(pi, ledger, "m1");
		const first = await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(first.replyFailed.join(","), "m1",
			"the reply was refused, and said so");
		assertEq(first.acknowledged.join(","), "m1",
			"the service acknowledgement settled it anyway");
		assertEq(first.serviceAckFailed.join(","), "", "nothing held the gate");
		assertEq(ledger.owing(), false,
			"the gate opened on the service acknowledgement");
		const second = await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(second.replyFailed.join(","), "",
			"the retry delivered the reply");
		const lines = recorded(spec.env.STUB_LOG);
		const idOf = (line: string[]) =>
			line.find((part) => part.startsWith("--request-id="));
		assertEq(idOf(lines[0]), idOf(lines[2]),
			"the retried reply reclaimed the same send receipt");
	});
});

test("acknowledgements: a reply that records but the service will not keeps the gate", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }],
		ack: [{ stderr: "pi-comms: no service: no live entry", exit: 4 }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		arm(pi, ledger, "m1");
		const report = await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(report.serviceAckFailed.join(","), "m1",
			"the reply went out, the marker did not record");
		assertEq(ledger.owing(), true, "the gate stays for the owed id");
		assertEq(new Set(ledgerPendingIds(pi)).size, 1,
			"one durable record, with its reply progress, to retry");
	});
});

test("acknowledgements: mail the mailbox no longer holds settles as lapsed", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }],
		ack: [{ stdout: { status: "ok", acknowledged: [], unknown: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		receiveAt(pi, ledger, "m1", "n1");
		const report = await ledger.acknowledge(["m1"], {}, undefined, pi.ctx());
		assertEq(report.lapsed.join(","), "m1", "settled locally");
		assertEq(report.unknown.join(","), "", "the session received it");
		assertEq(report.serviceAckFailed.join(","), "", "nothing held");
		assertEq(ledger.owing(), false, "the gate opened");
		const completion = pi.session.entries
			.filter((entry) => entry.customType === COMMS_ACK_STATE)
			.map((entry) => entry.data as Record<string, unknown>)
			.find((data) => data.acknowledged === true);
		assertEq(completion?.settlement, "lapsed",
			"the durable record names what settled it");
		const lines = recorded(spec.env.STUB_LOG);
		assertEq(lines.length, 2, "one reply, one service acknowledgement");
		assert(lines[1].includes("--at=n1"),
			"the acknowledgement ran in the mailbox the mail arrived in");
		const again = await ledger.acknowledge(["m1"], {}, undefined, pi.ctx());
		assertEq(again.lapsed.join(","), "m1",
			"a retry reports the same settlement");
		assertEq(recorded(spec.env.STUB_LOG).length, 3,
			"and sends no second reply");
		ledger.attach(pi.ctx());
		assertEq(ledger.owing(), false, "a reload keeps it settled");
	});
});

test("acknowledgements: an unproven id never settles on a mailbox's silence", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }],
		ack: [{ stdout: { status: "ok", acknowledged: [], unknown: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		// Nothing in the branch says this session ever received m1.
		const plain = new FakePi();
		const first = harness(plain);
		arm(plain, first.ledger, "m1");
		const unproven = await first.ledger.acknowledge(
			["m1"], {}, undefined, plain.ctx());
		assertEq(unproven.lapsed.join(","), "", "no record, no settlement");
		assertEq(first.ledger.owing(), true, "the gate stays");

		// The receipt is real, but the caller asked another mailbox.
		const elsewhere = new FakePi();
		const second = harness(elsewhere);
		receiveAt(elsewhere, second.ledger, "m1", "n1");
		const conflicted = await second.ledger.acknowledge(
			["m1"], { node: "n2" }, undefined, elsewhere.ctx());
		assertEq(conflicted.lapsed.join(","), "",
			"a caller's own node proves nothing");
		assertEq(second.ledger.owing(), true, "the gate stays");

		// The receipt sits on a sibling branch, which this one never saw.
		const sibling = new FakePi();
		const third = harness(sibling);
		receiveAt(sibling, third.ledger, "m1", "n1");
		const ctx = sibling.ctx();
		const ownBranch = { sessionManager: {
			getEntries: ctx.sessionManager.getEntries,
			getBranch: () => [],
		} };
		const branched = await third.ledger.acknowledge(
			["m1"], {}, undefined, ownBranch as never);
		assertEq(branched.lapsed.join(","), "",
			"another branch's receipt is not this one's");
		assertEq(third.ledger.owing(), true, "the gate stays");
	});
});

test("acknowledgements: one service call per mailbox, each in its own", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }],
		ack: [
			{ stdout: { status: "ok", acknowledged: ["m1"], unknown: [] } },
			{ stdout: { status: "ok", acknowledged: [], unknown: ["m2"] } },
		],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		receiveAt(pi, ledger, "m1", "n1");
		receiveAt(pi, ledger, "m2", "n2");
		const report = await ledger.acknowledge(
			["m1", "m2"], {}, undefined, pi.ctx());
		assertEq(report.acknowledged.join(","), "m1", "the marked one settled");
		assertEq(report.lapsed.join(","), "m2", "the dropped one lapsed");
		assertEq(ledger.owing(), false, "both settled");
		const acknowledgements = recorded(spec.env.STUB_LOG)
			.filter((line) => line.some((arg) => arg === "ack"));
		assertEq(acknowledgements.length, 2, "one call per mailbox");
		assertEq(acknowledgements[0].slice(-3).join(" "), "--at=n1 ack m1",
			"the first mailbox got only its own id");
		assertEq(acknowledgements[1].slice(-3).join(" "), "--at=n2 ack m2",
			"and the second its own");
	});
});

test("acknowledgements: the pending set is rebuilt from the session's own entries", () => {
	const pi = new FakePi();
	const { ledger } = harness(pi);
	pi.appendEntry(COMMS_ACK_STATE, {
		messageId: "m5",
		replyRequestId: "rr",
		replySent: false,
		acknowledged: false,
	});
	pi.appendEntry(COMMS_ACK_STATE, {
		messageId: "m6",
		replyRequestId: "rs",
		replySent: true,
		acknowledged: false,
	});
	pi.appendEntry(COMMS_ACK_STATE, {
		messageId: "m7",
		replyRequestId: "rt",
		acknowledged: true,
	});
	ledger.attach(pi.ctx());
	assertEq(ledger.pending().map((record) => record.messageId).join(","),
		"m5,m6", "the unresolved obligations return; the settled one does not");
	const restored = ledger.pending().find((record) =>
		record.messageId === "m5") as Record<string, unknown>;
	assertEq(restored.replyRequestId, "rr",
		"the reply's request id survives, so a retry reclaims its receipt");
});

// -- the alignment, end to end --------------------------------------------

import { CommsTools } from "../../pi/extensions/comms/tools.ts";

test("delivery: the foreground wait parks the receiver and delivers through it", async () => {
	const spec = scripted({
		wait: [
			{ stdout: { status: "timed-out", messages: [], cursor: { sequence: 1 } } },
			{
				stdout: {
					status: "ok",
					messages: [envelope("mw", 2, "held")],
					cursor: { sequence: 2 },
				},
			},
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	const env = { ...spec.env, STUB_DELAY: "0.3" };
	await withEnv(env, async () => {
		writeFileSync(env.STUB_STATE, JSON.stringify({}));
		const pi = new FakePi();
		const { delivery, ledger } = harness(pi);
		delivery.start();
		// The receiver's first hold ends; the next one is mid-flight — its
		// stub invocation has not consumed its script entry — when the
		// foreground wait parks it.
		await sleep(450);
		assertEq(delivery.active(), true, "the receiver is holding");
		new CommsTools(
			new CommsCli(new ProcessRunner(execProcess)),
			new CommsMessageLog(pi as never),
			{ delivery, waitUpdateMs: 100 } as never,
		).register(pi as never);
		const tool = pi.tools.get("comms_wait");
		const result = await tool.execute("call1", { timeoutMs: 5000 },
			undefined, undefined, {
				cwd: scratchDir(),
				sessionManager: pi.session,
				hasPendingMessages: () => false,
			});
		assertEq(result.details.stoppedBy, "message", "the hold delivered");
		assertEq(ledger.owing(), true, "the delivered mail owes its ack");
		const states = pi.session.entries.filter((entry) =>
			entry.customType === COMMS_ACK_STATE)
			.map((entry) => entry.data as Record<string, unknown>);
		assertEq(new Set(states.map((data) =>
			data.messageId)).size, 1, "armed once");
		assertEq(states.length, 2, "the arm and its presentation are recorded");
		assert(states[0].presented !== true,
			"the arm precedes the presentation");
		assert(states[1].presented === true,
			"the foreground delivery recorded the durable presentation");
		const received = pi.session.entries.filter((entry) =>
			entry.customType === COMMS_LOG).map((entry) =>
			(entry.data as Record<string, unknown>).messageId);
		sameJson(received, ["mw"], "recorded through the receiver");
		assertEq(pi.messages.filter((message) =>
			message.customType === COMMS_DELIVERY).length, 0,
			"the tool result was the presentation, so nothing was steered");
		// The receiver resumes after the foreground hold and stops for
		// good on the scripted failure.
		await sleep(700);
		assertEq(delivery.active(), false,
			"the following hold failed, so the receiver stopped itself");
	});
});

// -- the regressions: durability, reload, cursors, cancellation ------------

test("acknowledgements: a completed obligation survives reload without re-arming, and a new session starts empty", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }],
		ack: [{ stdout: { status: "ok", acknowledged: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const sent = new FakePi();
		const { ledger } = harness(sent);
		arm(sent, ledger, "m1");
		await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(ledger.owing(), false, "the session settled its own obligation");
		// The same branch, replayed into a new runtime: the completed
		// record returns, so a repeated envelope cannot arm a fresh reply.
		const reloaded = new FakePi();
		reloaded.session.entries = sent.session.entries;
		const { ledger: reloadedLedger } = harness(reloaded);
		reloadedLedger.attach(reloaded.ctx());
		assertEq(reloadedLedger.owing(), false,
			"the completed obligation is not owed again");
		assertEq(reloadedLedger.markPending(envelope("m1", 1, "hello")), true,
			"a repeat answers its existing durability");
		assertEq(reloadedLedger.owing(), false,
			"and a repeat opens no gate");
		// A different session starts from its own record alone: its ledger
		// drops the previous session's state on attach.
		const fresh = new FakePi();
		reloadedLedger.attach(fresh.ctx());
		assertEq(reloadedLedger.pending().length, 0,
			"a different session carries no inherited obligations");
	});
});

test("acknowledgements: appends the branch refuses keep the gate, and the retries settle from the reply progress", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }, { stdout: { status: "accepted" } }],
		ack: [
			{ stdout: { status: "ok", acknowledged: ["m1"] } },
			{ stdout: { status: "ok", acknowledged: ["m1"] } },
		],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { ledger } = harness(pi);
		arm(pi, ledger, "m1");
		// From here every append refuses: the reply's progress cannot be
		// recorded, so the first attempt reports the reply failed — even
		// though the mail went out — and the marker the service recorded
		// cannot complete durably, so the gate stays for both reasons.
		pi.failOn.add(COMMS_ACK_STATE);
		const first = await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(first.replyFailed.join(","), "m1",
			"a reply without its durable progress is not credited");
		assertEq(first.serviceAckFailed.join(","), "m1",
			"a service acknowledgement the branch cannot settle keeps the gate");
		assertEq(ledger.owing(), true, "the gate stays");
		// Once the branch accepts again, the same request id reclaims the
		// receipt and the completion lands: the gate opens.
		pi.failOn.delete(COMMS_ACK_STATE);
		const second = await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(second.acknowledged.join(","), "m1", "the retry settled it");
		assertEq(ledger.owing(), false, "the gate opened");
		const lines = recorded(spec.env.STUB_LOG);
		assertEq(lines.length, 4,
			"two reply attempts and two service acknowledgements");
		assert(lines[1].join(" ").endsWith("ack m1") &&
			lines[3].join(" ").endsWith("ack m1"),
			"the service acknowledgement ran while the reply was stuck");
		const idOf = (line: string[]) =>
			line.find((part) => part.startsWith("--request-id="));
		assertEq(idOf(lines[2]), idOf(lines[0]),
			"the retried reply reclaimed the same send receipt");
		// The settled state reloads as settled.
		const reloaded = new FakePi();
		reloaded.session.entries = pi.session.entries;
		const { ledger: reloadedLedger } = harness(reloaded);
		reloadedLedger.attach(reloaded.ctx());
		assertEq(reloadedLedger.owing(), false,
			"the completed obligation reloads settled");
	});
});

/** The cursor entries one branch holds, in order. */
const cursorEntries = (pi: FakePi) =>
	pi.session.entries.filter((entry) => entry.customType === COMMS_CURSOR);

test("delivery: equal cursor advances are written once, and a refused advance retries", async () => {
	const pi = new FakePi();
	const { delivery } = harness(pi);
	delivery.acceptCursor(3);
	delivery.acceptCursor(3);
	assertEq(cursorEntries(pi).length, 1,
		"the equal advance is not recorded twice");
	// A refused advance writes nothing; the equal value retries it.
	pi.failOn.add(COMMS_CURSOR);
	delivery.acceptCursor(4);
	assertEq(cursorEntries(pi).length, 1, "a refused advance writes nothing");
	pi.failOn.delete(COMMS_CURSOR);
	delivery.acceptCursor(4);
	assertEq(cursorEntries(pi).length, 2,
		"the equal advance retries after its refusal");
	delivery.acceptCursor(4);
	assertEq(cursorEntries(pi).length, 2,
		"and it is not recorded twice once durable");
	await delivery.stop();
});

test("delivery: the latest cursor record stands, including a restart to a lower sequence", async () => {
	const spec = scripted({
		wait: [
			{ stdout: { status: "timed-out", messages: [], cursor: { sequence: 2 } } },
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		pi.session.entries.push({
			type: "custom", customType: COMMS_CURSOR,
			data: { mailbox: "@pi-test", sequence: 5 },
		});
		// A restart from a later runtime tracked the mailbox's surviving
		// history down: the lower record is the later one, and it stands.
		pi.session.entries.push({
			type: "custom", customType: COMMS_CURSOR,
			data: { mailbox: "@pi-test", sequence: 2 },
		});
		const { delivery } = harness(pi);
		delivery.start();
		await sleep(500);
		assertEq(delivery.active(), false, "the resumed hold stopped the receiver");
		const lines = recorded(spec.env.STUB_LOG).filter((line) =>
			line[4] === "wait");
		assert(lines[0] && lines[0].includes("--after=2"),
			"the restart record stands over the higher one");
		assertEq(cursorEntries(pi).length, 2,
			"the equal advance of the restored cursor adds nothing");
	});
});

test("acknowledgements: the branch's own record rewrites a reply's lost traffic entry", async () => {
	const spec = scripted({
		ack: [{ stdout: { status: "ok", acknowledged: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		// Progress from a runtime whose reply went out before the write
		// order carried its traffic entry along.
		pi.session.entries.push({
			type: "custom", customType: COMMS_ACK_STATE,
			data: {
				messageId: "m1", toAgentId: "pi-test", toNodeId: "",
				from: { agentId: "ann" }, kind: "message", sequence: 1,
				replyRequestId: "req-1", replySent: true,
				acknowledged: false, presented: true,
			},
		});
		const { ledger, delivery } = harness(pi);
		assertEq(ledger.owing(), true, "the armed obligation reloads owed");
		const report = await ledger.acknowledge(["m1"], {}, undefined);
		assertEq(report.acknowledged.join(","), "m1", "settled without re-sending");
		const lines = recorded(spec.env.STUB_LOG);
		assertEq(lines.length, 1, "only the service acknowledgement ran");
		assert(lines[0].join(" ").endsWith("ack m1"), "no reply was sent again");
		const traffic = pi.session.entries.filter((entry) =>
			entry.customType === COMMS_LOG);
		assertEq(traffic.length, 1, "the reply's traffic entry is rewritten");
		sameJson((traffic[0].data as Record<string, unknown>).requestId, "req-1",
			"under the preserved request id");
		await delivery.stop();
	});
});

test("acknowledgements: an envelope recorded without its obligation re-arms from the branch", async () => {
	const spec = scripted({
		send: [
			{ stdout: { status: "accepted" } },
			{ stdout: { status: "accepted" } },
		],
		ack: [{ stdout: { status: "ok", acknowledged: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { delivery, ledger } = harness(pi);
		// Received, recorded, but the pending append was refused.
		pi.failOn.add(COMMS_ACK_STATE);
		await delivery.ingest(envelope("m1", 7, "urgent"), { steer: false });
		pi.failOn.delete(COMMS_ACK_STATE);
		assertEq(ledger.owing(), true, "the in-memory gate held");
		// A fresh runtime restores from the branch alone.
		const reloaded = new FakePi();
		reloaded.session.entries = pi.session.entries;
		const reloadedRun = harness(reloaded);
		assertEq(reloadedRun.ledger.owing(), true,
			"the branch re-arms the obligation");
		assertEq(reloaded.messages.length, 1,
			"and the model is shown the mail it never saw");
		assert(String(reloaded.messages[0].content).includes("urgent"),
			"the steered presentation carries the recorded envelope payload");
		const report = await reloadedRun.ledger.acknowledge(["m1"], {}, undefined);
		assertEq(report.acknowledged.join(","), "m1",
			"the re-armed obligation settles");
		await delivery.stop();
		await reloadedRun.delivery.stop();
	});
});

test("delivery: a cancelled queued acquire does not pass the turn to its successor", async () => {
	const spec = scripted({
		wait: [
			{ stdout: { status: "timed-out", messages: [], cursor: { sequence: 1 } } },
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	const env = { ...spec.env, STUB_DELAY: "0.4" };
	await withEnv(env, async () => {
		const pi = new FakePi();
		const { delivery } = harness(pi);
		delivery.start();
		await sleep(600);
		const first = await delivery.acquireMailbox();
		// A caller that cancels while queued stands aside: the successor
		// behind it still waits for the owner's release.
		const controller = new AbortController();
		const b = delivery.acquireMailbox(controller.signal);
		await sleep(30);
		controller.abort();
		await b.catch(() => {});
		let turnedC = false;
		const c = delivery.acquireMailbox().then((release) => {
			turnedC = true;
			return release;
		});
		await sleep(50);
		assertEq(turnedC, false, "the successor waits behind the queue");
		first();
		const cRelease = await c;
		assert(turnedC, "the turn reaches the successor only after the release");
		cRelease();
		await sleep(900);
		assertEq(delivery.active(), false, "the resumed receiver stopped itself");
	});
});

test("delivery: an acquire without a signal waits out the receiver's safe point", async () => {
	// A CLI whose holds never settle on their own: the receiver's park
	// cannot finish until the hold's outcome is produced by hand.
	class HoldCli {
		private resolvers: Array<(outcome: never) => void> = [];
		identity(): string {
			return "pi-test";
		}
		run(): Promise<never> {
			return new Promise((resolve) => { this.resolvers.push(resolve); });
		}
		settle(outcome: never): void {
			const resolve = this.resolvers.shift();
			if (resolve) resolve(outcome);
		}
	}
	const timedOut = {
		command: "wait", argv: [], exitCode: CommsExit.TIMED_OUT,
		stdout: "", stderr: "", answer: null,
	};
	const usage = {
		command: "wait", argv: [], exitCode: CommsExit.USAGE,
		stdout: "", stderr: "pi-comms: usage", answer: null,
	};
	await withEnv({}, async () => {
		const pi = new FakePi();
		const cli = new HoldCli();
		const log = new CommsMessageLog(pi as never);
		const ledger = new CommsAcknowledgements(cli as never, log, pi as never);
		const delivery = new CommsDelivery(
			cli as never, log, pi as never, ledger, policy);
		const ctx = pi.ctx();
		log.attach(ctx);
		ledger.attach(ctx);
		delivery.attach(ctx);
		delivery.start();
		await sleep(50);
		assertEq(delivery.active(), true, "the receiver holds");
		// The acquire parks the receiver, but the killed hold may still be
		// exiting: the grant cannot happen before the loop tops out.
		let granted: (() => void) | undefined;
		const acquire = delivery.acquireMailbox().then((release) => {
			granted = release;
			return release;
		});
		await sleep(50);
		assertEq(granted, undefined, "the acquire does not return over a live hold");
		cli.settle(timedOut as never);
		await acquire;
		assert(granted, "the acquire resolves at the safe point");
		granted();
		// The resumed hold stops the receiver on the scripted usage failure.
		await sleep(50);
		cli.settle(usage as never);
		await sleep(100);
		assertEq(delivery.active(), false, "the resumed receiver stopped itself");
	});
});

test("delivery: consecutive foreground waits continue from the committed cursor and hand it to the resumed receiver", async () => {
	const spec = scripted({
		wait: [
			// The receiver's first hold: an empty page, cursor 5.
			{ stdout: { status: "timed-out", messages: [], cursor: { sequence: 5 } } },
			// The receiver's next hold is aborted mid-delay — its invocation
			// never consumed this entry — so foreground wait one reads it.
			{ stdout: { status: "ok", messages: [envelope("mw", 6, "one")], cursor: { sequence: 6 } } },
			// Foreground wait two is empty.
			{ stdout: { status: "timed-out", messages: [], cursor: { sequence: 6 } } },
			// The resumed receiver stops for good on the scripted failure.
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	const env = { ...spec.env, STUB_DELAY: "0.4" };
	await withEnv(env, async () => {
		const pi = new FakePi();
		const { delivery, ledger } = harness(pi);
		delivery.start();
		await sleep(600);
		assertEq(delivery.active(), true, "the receiver is holding");
		new CommsTools(
			new CommsCli(new ProcessRunner(execProcess)),
			new CommsMessageLog(pi as never),
			{ delivery, waitUpdateMs: 100 } as never,
		).register(pi as never);
		const tool = pi.tools.get("comms_wait");
		const ctx = () => ({
			cwd: scratchDir(),
			sessionManager: pi.session,
			hasPendingMessages: () => false,
		});
		const first = await tool.execute("w1", { timeoutMs: 5000 },
			undefined, undefined, ctx());
		assertEq(first.details.stoppedBy, "message", "the first wait delivered");
		sameJson(first.details.presented, ["mw"], "the mail was presented");
		assertEq(ledger.owing(), true, "the delivered mail owes its ack");
		const second = await tool.execute("w2", { timeoutMs: 5000 },
			undefined, undefined, ctx());
		assertEq(second.details.stoppedBy, "timeout",
			"the second wait was empty, not a replay");
		// The foreground holds continued from the committed cursor; the
		// resumed receiver's line needs its own hold, so it waits.
		const waits = recorded(spec.env.STUB_LOG).filter((line) =>
			line[4] === "wait");
		assert(waits[1].includes("--after=5"),
			"the first foreground hold continued from the restored cursor");
		assert(waits[2].includes("--after=6"),
			"the second foreground hold continued from the first's committed cursor");
		// The resumed receiver takes the same position.
		await sleep(800);
		assertEq(delivery.active(), false, "the receiver stopped itself");
		const resumed = recorded(spec.env.STUB_LOG).filter((line) =>
			line[4] === "wait");
		assert(resumed.length >= 4 && resumed[3].includes("--after=6"),
			"the resumed receiver held from the committed cursor too");
		const cursor = pi.session.entries.filter((entry) =>
			entry.customType === COMMS_CURSOR);
		sameJson(cursor.at(-1)?.data,
			{ mailbox: "@pi-test", sequence: 6 },
			"the committed cursor is durable");
	});
});

test("delivery: concurrent mailbox leases serialize, and a cancel frees only its own turn", async () => {
	const spec = scripted({
		wait: [
			{ stdout: { status: "timed-out", messages: [], cursor: { sequence: 1 } } },
			// The hold in flight when the first lease parks the receiver;
			// its slot is re-read by the resumed hold, which stops on it.
			{ stderr: "pi-comms: usage: bad flag", exit: 1 },
		],
	});
	const env = { ...spec.env, STUB_DELAY: "0.4" };
	await withEnv(env, async () => {
		const pi = new FakePi();
		const { delivery } = harness(pi);
		delivery.start();
		await sleep(600);
		// The first lease parks the receiver at its safe point; the
		// second waits behind it, because the mailbox serves one hold.
		const first = await delivery.acquireMailbox();
		assertEq(delivery.active(), true, "the receiver stays parked for the owner");
		let secondTurn = false;
		let secondRelease: (() => void) | undefined;
		const second = delivery.acquireMailbox().then((release) => {
			secondTurn = true;
			secondRelease = release;
		});
		await sleep(50);
		assertEq(secondTurn, false, "the second lease waits behind the first");
		// A cancel that never took the turn leaves the queue free: its
		// refusal is immediate, and the next caller is not stranded.
		const controller = new AbortController();
		controller.abort();
		const t0 = Date.now();
		let refused: unknown;
		try {
			await delivery.acquireMailbox(controller.signal);
		} catch (error) {
			refused = error;
		}
		assert(refused instanceof Error, "a cancelled caller is refused, not granted");
		assert(Date.now() - t0 < 100, "a cancelled caller does not wait out a turn");
		first();
		await second;
		assert(secondTurn, "a release hands the mailbox to the next caller");
		// The owner's release stands the receiver up: the resumed hold
		// re-reads the unconsumed slot and stops on the scripted failure.
		(secondRelease ?? (() => {}))();
		await sleep(900);
		assertEq(delivery.active(), false, "the resumed receiver stopped itself");
		await delivery.stop();
	});
});

test("acknowledgements: the tool reports a lapsed settlement as settled", async () => {
	const spec = scripted({
		send: [{ stdout: { status: "accepted" } }],
		ack: [{ stdout: { status: "ok", acknowledged: [], unknown: ["m1"] } }],
	});
	await withEnv(spec.env, async () => {
		const pi = new FakePi();
		const { delivery, ledger } = harness(pi);
		new CommsTools(
			new CommsCli(new ProcessRunner(execProcess)),
			new CommsMessageLog(pi as never),
			{ delivery, acks: ledger, waitUpdateMs: 100 } as never,
		).register(pi as never);
		receiveAt(pi, ledger, "m1", "n1");
		const result = await pi.tools.get("comms_ack")?.execute(
			"call1", { messageIds: ["m1"] }, undefined, undefined,
			{ cwd: scratchDir(), ...pi.ctx() });
		assertEq(result?.isError, false, "a lapsed id is settled, not an error");
		assert(String(result?.content[0].text).includes(
			"no longer recognizes: m1"), "and the report says what settled it");
		assertEq(ledger.owing(), false, "the gate opened");
	});
});
