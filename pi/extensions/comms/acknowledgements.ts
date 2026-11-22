/**
 * pi-daemon — the mandatory agent-comms acknowledgement.
 *
 * Every delivered application envelope carries an obligation: the
 * receiving agent answers its sender with one fixed, correlated
 * acknowledgement message, and the service acknowledgement is recorded
 * beside it. The reply is best-effort: it is attempted with the
 * persisted request id so a lost reply never duplicates mail, and a
 * reply that cannot be delivered is reported but never holds the
 * obligation — the gate opens once the service acknowledgement of each
 * delivered id is recorded. A delivered id the mailbox no longer holds
 * settles as lapsed instead, for the branch whose own arm record shows
 * the envelope arrived in that same mailbox: the gate never opens for an
 * id this session cannot show it received. An operator's `/comms-release`
 * settles every remaining id as released, which opens the gate without
 * claiming a service acknowledgement. Until then, one `tool_call` gate
 * blocks every tool except `comms_ack` and `comms_replay` — the call the
 * ack's own report names for an id this session never received —
 * `pre_daemon`, and any tool another extension declares as its own
 * prerequisite's escape (see guards/tool-gate-escapes.ts) — the
 * fail-closed shape pi-consult's implementation gate uses. The reply's
 * request id is generated and persisted before the send, so a lost reply
 * never produces a duplicate acknowledgement message; the resend reclaims
 * the same send receipt — and since the reply holds nothing, a later
 * comms_ack may retry it at any time.
 */

import { randomUUID } from "node:crypto";
import type {
	ExtensionAPI,
	ExtensionContext,
	ToolCallEvent,
	ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";

import type { CommsCli } from "./client.ts";
import {
	ACK_KIND,
	ACK_TYPE,
	ACK_VERSION,
	CommsArgv,
	CommsExit,
	type CommsAddress,
	type CommsEnvelope,
	isProtocolAck,
} from "./protocol.ts";
import {
	COMMS_LOG,
	CommsMessageLog,
} from "./message-log.ts";
import { ToolGateEscapes } from "../guards/tool-gate-escapes.ts";

/** The custom entry type acknowledgement progress is stored under. */
export const COMMS_ACK_STATE = "pi-agent-comms-ack-state";

/** One delivered message's outstanding acknowledgement. */
interface PendingRecord {
	messageId: string;
	/** The mailbox the message arrived in, where its service ack runs. */
	toAgentId: string;
	toNodeId: string;
	/** The sender the reply goes to. */
	from: CommsAddress;
	kind: string;
	sequence: number;
	/** Generated before the reply and reused across retries. */
	replyRequestId: string;
	/** The reply was accepted by the service (or queued for it). */
	replySent?: boolean;
	/** A service or lapsed acknowledgement of the original was recorded. */
	acknowledged?: boolean;
	/** What settled it: a recorded service marker, a mailbox that no longer
	 *  holds what the branch received there, or the operator's release. */
	settlement?: "service" | "lapsed" | "released";
	/** Whether a released record's own snapshot was appended; in memory
	 *  only, and meaningful only while `settlement` is `released`. */
	releaseDurable?: boolean;
	/** The model was shown the message (steered or tool result). */
	presented?: boolean;
	/** Whether the durable append of this record itself succeeded. */
	durable?: boolean;
}

/** What one comms_ack call did, per id and overall. */
export interface AckReport {
	/** Ids whose reply and service acknowledgement both completed. */
	acknowledged: string[];
	/** Ids the service already had markers for; their reply, if not yet
	 *  sent, still ran here. */
	alreadyAcknowledged: string[];
	/** Ids settled locally because the mailbox no longer holds mail this
	 *  branch received there. */
	lapsed: string[];
	/** Ids the operator released; no service acknowledgement is claimed. */
	released: string[];
	/** Ids this session never received — replay first, never just ack. */
	unknown: string[];
	/** Ids the reply of which the branch could not credit; reported, they
	 *  never hold the id out of the service acknowledgement. */
	replyFailed: string[];
	/** Ids whose service markers did not record or complete durably; the
	 *  gate stays for these. */
	serviceAckFailed: string[];
}

/** What one `/comms-release` did, per id and for the branch itself. */
export interface ReleaseReport {
	released: Array<{
		messageId: string;
		replySent: boolean;
		durable: boolean;
	}>;
	/** The branch could not be read, so no obligation could be named. */
	unreadable: boolean;
}

/** One mailbox's share of an acknowledgement turn: the agent whose mail is
 *  kept there, the node it is kept on, and the ids that arrived in it. */
interface MailboxGroup {
	agent: string;
	node: string;
	records: PendingRecord[];
}

export class CommsAcknowledgements {
	private readonly pendingMap = new Map<string, PendingRecord>();
	/** Bumped by every `attach`: an acknowledgement turn that started on an
	 *  earlier mapping may not settle anything in this one. */
	private generation = 0;
	/** Set by an `attach` that could not read the branch: the gate fails
	 *  closed until an operator releases it or a later attach succeeds. */
	private unreadable = false;

	constructor(
		private readonly cli: CommsCli,
		private readonly log: CommsMessageLog,
		private readonly pi: ExtensionAPI,
	) {}

	/** Rebuild the pending set from the session's current branch, so a
	 *  reload or a session switch keeps every obligation — including
	 *  completed ones, so a repeated envelope cannot arm a fresh reply
	 *  — and so a different branch starts from its own record alone. An
	 *  unreadable branch leaves the gate closed until it can be read. */
	attach(ctx?: ExtensionContext): void {
		this.generation += 1;
		let entries: Array<Record<string, unknown>> | undefined;
		try {
			const branch = ctx?.sessionManager?.getBranch?.();
			if (Array.isArray(branch)) entries = branch;
		} catch {
			entries = undefined;
		}
		this.pendingMap.clear();
		if (!entries) {
			// The branch could not be read: an obligation it holds cannot
			// be named, but the gate must not open as if none existed.
			this.unreadable = true;
			return;
		}
		this.unreadable = false;
		for (const entry of entries) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_ACK_STATE) continue;
			const data = entry.data as Record<string, unknown> | undefined;
			const record = CommsAcknowledgements.record(data);
			if (!record) continue;
			this.pendingMap.set(record.messageId, record);
		}
		this.reconcile(ctx, entries);
	}

	/** Close the gap between the traffic log and the acknowledgement
	 *  records: an envelope the branch recorded but whose pending append
	 *  was lost is re-armed — reusing the reply request id a recorded
	 *  reply entry carried, so no acknowledgement mail is duplicated —
	 *  while protocol acknowledgements stay outside the gate. */
	private reconcile(
		ctx: ExtensionContext | undefined,
		entries: Array<Record<string, unknown>>,
	): void {
		for (const entry of entries) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_LOG) continue;
			const data = (entry.data ?? {}) as Record<string, unknown>;
			if (data.direction === "sent") continue;
			const envelope = data as unknown as CommsEnvelope;
			if (typeof envelope.messageId !== "string" || !envelope.messageId) {
				continue;
			}
			if (isProtocolAck(envelope) || this.pendingMap.has(envelope.messageId)) {
				continue;
			}
			const replyRequestId =
				CommsMessageLog.replyRequestIdFor(envelope.messageId, ctx);
			this.markPending(envelope, replyRequestId, !!replyRequestId);
		}
	}

	/** Arm the obligation for one newly delivered envelope; returns
	 *  whether the durable record succeeded. A repeated delivery is a
	 *  no-op that retries a known nondurable record's persist first, and
	 *  a released id is never armed again. */
	markPending(
		envelope: CommsEnvelope,
		replyRequestId?: string,
		replySent = false,
	): boolean {
		if (!envelope || typeof envelope.messageId !== "string" || !envelope.messageId) {
			return false;
		}
		const known = this.pendingMap.get(envelope.messageId);
		if (known) {
			// A released id is never re-armed by a redelivery: its own
			// snapshot is written only by a deliberate release.
			if (known.settlement === "released") {
				return known.releaseDurable === true;
			}
			if (!known.durable) known.durable = this.persist(known);
			return known.durable;
		}
		const record: PendingRecord = {
			messageId: envelope.messageId,
			toAgentId: envelope.to?.agentId ?? "",
			toNodeId: envelope.to?.nodeId ?? "",
			from: envelope.from ?? { agentId: "" },
			kind: envelope.kind,
			sequence: envelope.sequence,
			replyRequestId: replyRequestId || randomUUID(),
			replySent,
			durable: false,
		};
		record.durable = this.persist(record);
		this.pendingMap.set(record.messageId, record);
		return record.durable;
	}

	/** Durable presentation: the model has seen the message, so a
	 *  reload must not steer it again. The model reading the mail is a
	 *  claim only when its append is durable. */
	markPresented(messageId: string): void {
		const record = this.pendingMap.get(messageId);
		if (!record || record.presented) return;
		record.presented = true;
		// A volatile release stays volatile until the operator releases it
		// again: presentation never turns one into a durable snapshot.
		if (record.settlement !== "released" || record.releaseDurable) {
			this.persist(record);
		}
	}

	/** The message ids the ledger treats as shown to the model: marked
	 *  presented, or acknowledged — an acknowledged obligation was
	 *  necessarily seen to be answered. */
	presentedIds(): Set<string> {
		const shown = new Set<string>();
		for (const record of this.pendingMap.values()) {
			if (record.presented || record.acknowledged) {
				shown.add(record.messageId);
			}
		}
		return shown;
	}

	/** Every obligation not yet fully carried out; a release settles one
	 *  for the gate's purposes without claiming a service acknowledgement. */
	pending(): PendingRecord[] {
		return [...this.pendingMap.values()].filter((record) =>
			!record.acknowledged && record.settlement !== "released");
	}

	/** Whether anything is owed — the gate's whole question. */
	owing(): boolean {
		return this.pending().length > 0;
	}

	/** The operator's override: settle every id the gate still holds as
	 *  released — including one whose earlier release the branch refused,
	 *  which is retried here — and open the gate. No reply is sent and no
	 *  service acknowledgement is claimed. A refused append still opens
	 *  the live gate and is reported as not durable, so the operator knows
	 *  the id can re-arm from the branch. */
	release(): ReleaseReport {
		// An acknowledgement turn still in flight must not write over a
		// release, so its mapping is replaced before any record changes.
		this.generation += 1;
		const report: ReleaseReport = {
			released: [],
			unreadable: this.unreadable,
		};
		this.unreadable = false;
		for (const record of this.pendingMap.values()) {
			if (record.acknowledged
				|| (record.settlement === "released" && record.releaseDurable)) {
				continue;
			}
			const durable = this.persist({
				...record,
				acknowledged: false,
				settlement: "released",
			});
			record.acknowledged = false;
			record.settlement = "released";
			record.releaseDurable = durable;
			if (durable) record.durable = true;
			report.released.push({
				messageId: record.messageId,
				replySent: record.replySent === true,
				durable,
			});
		}
		return report;
	}

	/** The gate: no tool runs while an acknowledgement is owed, except
	 *  the tools the acknowledgement itself needs and a tool a live
	 *  registration declares as its escape. Fails closed: a state read
	 *  that cannot be trusted blocks rather than allows. */
	register(pi: ExtensionAPI): void {
		pi.on("tool_call", (event: ToolCallEvent): ToolCallEventResult | undefined => {
			try {
				return this.decide(event);
			} catch {
				return {
					block: true,
					reason: CommsAcknowledgements.FAIL_CLOSED,
				};
			}
		});
	}

	private decide(event: ToolCallEvent): ToolCallEventResult | undefined {
		const name = String(event.toolName ?? "");
		const admitted = name === "comms_ack" || name === "comms_replay"
			|| name === "pre_daemon" || ToolGateEscapes.admits(this.pi, name);
		if (!this.owing()) {
			return this.unreadable && !admitted
				? { block: true, reason: CommsAcknowledgements.UNREADABLE }
				: undefined;
		}
		if (admitted) return undefined;
		return {
			block: true,
			reason: CommsAcknowledgements.denial(
				this.pending().map((record) => record.messageId)),
		};
	}

	/** One acknowledgement turn: every id's reply first, best-effort and
	 *  only reported, then the service acknowledgement of the mailbox each
	 *  id arrived in — one call per mailbox, since a marker records only
	 *  where the mail was kept. The reply never holds an id back. A retry
	 *  resumes where it stopped. The branch context is not read here: an
	 *  id's receipt proof is its own arm record. */
	async acknowledge(
		messageIds: string[],
		params: { node?: string },
		signal?: AbortSignal,
		_ctx?: ExtensionContext,
	): Promise<AckReport> {
		const generation = this.generation;
		const ids = messageIds.map(String);
		const report: AckReport = {
			acknowledged: [],
			alreadyAcknowledged: [],
			lapsed: [],
			released: [],
			unknown: [],
			replyFailed: [],
			serviceAckFailed: [],
		};
		const mailboxes = new Map<string, MailboxGroup>();
		for (const id of ids) {
			if (this.stale(generation)) break;
			const record = this.pendingMap.get(id);
			if (!record) {
				report.unknown.push(id);
				continue;
			}
			if (record.settlement === "released") {
				// An operator already settled it: no reply, no service call.
				report.released.push(id);
				continue;
			}
			if (!await this.answer(record, report, signal, generation)) break;
			// A marker records only where the mail was kept: the agent it was
			// addressed to on the node it arrived at. One call per mailbox,
			// never one per caller.
			const agent = record.toAgentId;
			const node = params.node ?? record.toNodeId;
			const key = `${agent}\u0000${node}`;
			const group = mailboxes.get(key) ?? { agent, node, records: [] };
			group.records.push(record);
			mailboxes.set(key, group);
		}
		for (const group of mailboxes.values()) {
			if (this.stale(generation)) break;
			await this.settle(
				group.records, group.node, group.agent, report, signal,
				generation,
			);
		}
		if (this.stale(generation)) {
			// The branch this turn started on is gone: whatever it had not
			// settled yet stays owed to the branch that holds it now.
			for (const id of ids) {
				if (!CommsAcknowledgements.reported(report, id)) {
					report.serviceAckFailed.push(id);
				}
			}
		}
		return report;
	}

	/** Answer one delivered id's sender, once. The reply only goes out on a
	 *  durably armed obligation: the persist retries here, since nothing
	 *  else redelivers mail the branch already holds, and a still nondurable
	 *  record cannot give the reply its request id. A reply that cannot be
	 *  delivered is reported; it never holds the id out of the settlement.
	 *  False means the branch changed under the turn, which then stops
	 *  without writing anything into the mapping that replaced it. */
	private async answer(
		record: PendingRecord,
		report: AckReport,
		signal: AbortSignal | undefined,
		generation: number,
	): Promise<boolean> {
		const id = record.messageId;
		if (!record.durable) record.durable = this.persist(record);
		const replied = record.replySent
			|| (record.durable && await this.sendReply(record, signal, generation));
		if (this.stale(generation)) return false;
		if (replied) {
			// The reply's traffic entry repairs itself from the record: a
			// progress append that survived without its log entry is
			// rewritten, so the branch never claims a reply it cannot show.
			if (!this.repairReply(record)) report.replyFailed.push(id);
		} else {
			report.replyFailed.push(id);
		}
		return true;
	}

	/** The service acknowledgement of one mailbox's records: a recorded
	 *  marker settles the id, and an id that mailbox no longer holds settles
	 *  as lapsed when this branch proves it received it there. Anything else
	 *  stays owed. */
	private async settle(
		records: PendingRecord[],
		mailbox: string,
		mailboxAgent: string,
		report: AckReport,
		signal?: AbortSignal,
		generation = this.generation,
	): Promise<void> {
		const ids = records.map((record) => record.messageId);
		const outcome = await this.cli.run(
			"ack",
			CommsArgv.ack({ messageIds: ids, node: mailbox || undefined }),
			{ signal },
			mailboxAgent,
		);
		const answer = outcome.answer as
			| { acknowledged?: unknown; alreadyAcknowledged?: unknown;
				unknown?: unknown }
			| undefined;
		if (outcome.exitCode !== CommsExit.OK || !answer) {
			// The reply may have gone out, but the service markers did not
			// record. The gate stays for these ids and the next call retries
			// the ack.
			report.serviceAckFailed.push(...ids);
			return;
		}
		const settled = CommsAcknowledgements.strings(answer.acknowledged)
			.concat(CommsAcknowledgements.strings(answer.alreadyAcknowledged));
		const unsettled = CommsAcknowledgements.strings(answer.unknown);
		for (const record of records) {
			const id = record.messageId;
			if (settled.includes(id)) {
				if (this.complete(id, "service", generation)) {
					report.acknowledged.push(id);
				} else {
					// The gate stays: the durable completion is its opening,
					// and the next comms_ack retries it.
					report.serviceAckFailed.push(id);
				}
			} else if (unsettled.includes(id)
				&& this.lapsed(record, mailbox, mailboxAgent)) {
				// The mailbox holds no record of mail this branch received
				// there; the reply already answered its sender.
				if (this.complete(id, "lapsed", generation)) {
					report.lapsed.push(id);
				} else {
					report.serviceAckFailed.push(id);
				}
			} else {
				report.serviceAckFailed.push(id);
			}
		}
	}

	/** The protocol acknowledgement of an arriving reply: bookkeeping
	 *  only — service-acknowledged, never answered, never gated. */
	async protocolAck(envelope: CommsEnvelope): Promise<void> {
		await this.cli.run(
			"ack",
			CommsArgv.ack({ messageIds: [envelope.messageId] }),
		);
	}

	/** Send the reply once; the persisted request id makes the retry
	 *  reclaim the same send receipt instead of duplicating the mail.
	 *  The traffic entry is durable before the progress that names the
	 *  reply sent: progress beside its log entry, never ahead of it. */
	private async sendReply(
		record: PendingRecord,
		signal?: AbortSignal,
		generation = this.generation,
	): Promise<boolean> {
		const payload = {
			type: ACK_TYPE,
			version: ACK_VERSION,
			messageId: record.messageId,
		};
		const outcome = await this.cli.run(
			"send",
			CommsArgv.send({
				to: record.from.agentId,
				node: record.from.nodeId || undefined,
				kind: ACK_KIND,
				requestId: record.replyRequestId,
				payload,
			}),
			{ signal },
			// The reply answers as the mailbox that received the mail: the
			// turn acts as that agent throughout, not as whatever identity
			// the session resolves now.
			record.toAgentId || undefined,
		);
		// A send that outlived its branch records nothing: the request id it
		// carried is already durable, so a retry on the original branch
		// reclaims this same send instead of duplicating the mail.
		if (this.stale(generation)) return false;
		const status = (outcome.answer as { status?: string } | undefined)
			?.status;
		if (outcome.exitCode !== CommsExit.OK ||
				(status !== "accepted" && status !== "queued")) {
			return false;
		}
		if (!this.log.sent({
			to: { agentId: record.from.agentId, nodeId: record.from.nodeId },
			kind: ACK_KIND,
			requestId: record.replyRequestId,
			payload,
			status: status || "accepted",
		})) {
			// The branch refused the traffic entry: the reply claims
			// nothing, and its request id retries the send whole.
			return false;
		}
		record.replySent = true;
		if (!this.persist(record)) {
			record.replySent = false;
			return false;
		}
		return true;
	}

	/** Rewrite a reply's traffic entry the branch is missing — after a
	 *  reply that was sent under an older write order, or a refused
	 *  append. False is a still-refused append, reported as replyFailed:
	 *  the next comms_ack retries the repair. */
	private repairReply(record: PendingRecord): boolean {
		return this.log.sent({
			to: { agentId: record.from.agentId, nodeId: record.from.nodeId },
			kind: ACK_KIND,
			requestId: record.replyRequestId,
			payload: {
				type: ACK_TYPE,
				version: ACK_VERSION,
				messageId: record.messageId,
			},
			status: "accepted",
		});
	}

	/** Settle one id, naming what settled it: the durable completion is
	 *  the gate's opening. The completed snapshot is appended before the
	 *  live record is marked, so a failed append leaves the obligation owed
	 *  and reported. */
	private complete(
		id: string,
		settlement: "service" | "lapsed",
		generation = this.generation,
	): boolean {
		// The branch this turn started on may be gone: an attach rebuilt the
		// map, and a record that shares this id belongs to a branch that
		// never saw this turn's mail. Settling it here would claim an
		// obligation as carried out that was never asked.
		if (generation !== this.generation) return false;
		const record = this.pendingMap.get(id);
		// An operator's release is final: no later acknowledgement turn may
		// replace it with a service or lapsed completion.
		if (!record || record.settlement === "released") return false;
		if (!this.persist({ ...record, acknowledged: true, settlement })) {
			return false;
		}
		record.acknowledged = true;
		record.settlement = settlement;
		record.durable = true;
		return true;
	}

	/** Whether an id the ledger holds is proven delivered to the mailbox
	 *  whose service no longer recognizes it: the arm record itself names
	 *  that mailbox as the one the envelope arrived in, and the call ran as
	 *  that mailbox's own agent. A caller's own choice of node proves
	 *  nothing, and neither does another agent's silence; an empty recorded
	 *  recipient proves nothing either, since the call would have run as
	 *  whatever identity the session resolves now. */
	private lapsed(
		record: PendingRecord,
		mailbox: string,
		mailboxAgent: string,
	): boolean {
		return !!record.toAgentId
			&& mailboxAgent === record.toAgentId
			&& mailbox === record.toNodeId;
	}

	/** Whether the branch this turn started on was replaced under it by a
	 *  reload or a session switch: such a turn settles nothing and writes
	 *  nothing. */
	private stale(generation: number): boolean {
		return generation !== this.generation;
	}

	private static reported(report: AckReport, id: string): boolean {
		return report.acknowledged.includes(id) || report.lapsed.includes(id)
			|| report.released.includes(id) || report.unknown.includes(id)
			|| report.serviceAckFailed.includes(id);
	}

	/** One durable progress append; a failure keeps the in-memory state
	 *  and the gate open, never the obligation lost. */
	private persist(record: PendingRecord): boolean {
		try {
			this.pi.appendEntry(COMMS_ACK_STATE, CommsAcknowledgements.state(record));
			return true;
		} catch {
			return false;
		}
	}

	static denial(ids: string[]): string {
		return "An agent-comms acknowledgement is outstanding — every tool " +
			`is blocked until the delivered message${ids.length === 1 ? " is" : "s are"} ` +
			"acknowledged. Call " +
			`comms_ack { messageIds: ["${ids.join('", "')}"] } first ` +
			`(owed: ${ids.join(", ")})`;
	}

	private static state(record: PendingRecord): Record<string, unknown> {
		return {
			messageId: record.messageId,
			toAgentId: record.toAgentId,
			toNodeId: record.toNodeId,
			from: record.from,
			kind: record.kind,
			sequence: record.sequence,
			replyRequestId: record.replyRequestId,
			replySent: record.replySent ?? false,
			acknowledged: record.acknowledged ?? false,
			settlement: record.settlement,
			presented: record.presented ?? false,
		};
	}

	private static record(data: Record<string, unknown> | undefined): PendingRecord | null {
		if (!data || typeof data.messageId !== "string" || !data.messageId) {
			return null;
		}
		return {
			messageId: data.messageId,
			toAgentId: typeof data.toAgentId === "string" ? data.toAgentId : "",
			toNodeId: typeof data.toNodeId === "string" ? data.toNodeId : "",
			from: (data.from as CommsAddress | undefined) ?? { agentId: "" },
			kind: typeof data.kind === "string" ? data.kind : "",
			sequence: typeof data.sequence === "number" ? data.sequence : 0,
			replyRequestId: typeof data.replyRequestId === "string"
				&& data.replyRequestId
				? data.replyRequestId
				: randomUUID(),
			replySent: data.replySent === true,
			acknowledged: data.acknowledged === true,
			settlement: data.settlement === "service"
				|| data.settlement === "lapsed"
				|| data.settlement === "released"
				? data.settlement
				: undefined,
			releaseDurable: data.settlement === "released",
			presented: data.presented === true,
			durable: true,
		};
	}

	private static strings(value: unknown): string[] {
		return Array.isArray(value)
			? value.filter((item): item is string => typeof item === "string")
			: [];
	}

	private static readonly FAIL_CLOSED =
		"agent-comms acknowledgement state could not be read; refusing to " +
		"run tools until it can be trusted.";

	private static readonly UNREADABLE =
		"The agent-comms acknowledgement branch could not be read, so an " +
		"obligation it holds cannot be named. Call comms_ack for any id the " +
		"turn names, or release the gate from the keyboard with " +
		"`/comms-release`.";
}
