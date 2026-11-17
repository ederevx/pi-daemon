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
 * delivered id is recorded. Until then, one `tool_call` gate blocks
 * every tool except `comms_ack` itself and `pre_daemon` — the
 * fail-closed shape pi-consult's implementation gate uses. The reply's
 * request id is generated and persisted before the send, so a lost
 * reply never produces a duplicate acknowledgement message; the resend
 * reclaims the same send receipt — and since the reply holds nothing,
 * a later comms_ack may retry it at any time.
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
	/** The service acknowledgement of the original was recorded. */
	acknowledged?: boolean;
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
	/** Ids this session never received — replay first, never just ack. */
	unknown: string[];
	/** Ids the reply of which the branch could not credit; reported, they
	 *  never hold the id out of the service acknowledgement. */
	replyFailed: string[];
	/** Ids whose service markers did not record or complete durably; the
	 *  gate stays for these. */
	serviceAckFailed: string[];
}

export class CommsAcknowledgements {
	private readonly pendingMap = new Map<string, PendingRecord>();

	constructor(
		private readonly cli: CommsCli,
		private readonly log: CommsMessageLog,
		private readonly pi: ExtensionAPI,
	) {}

	/** Rebuild the pending set from the session's own entries, so a
	 *  reload or a session switch keeps every obligation — including
	 *  completed ones, so a repeated envelope cannot arm a fresh reply
	 *  — and so a different session starts from its own record alone. */
	attach(ctx?: ExtensionContext): void {
		const entries = ctx?.sessionManager?.getEntries?.() as
			| Array<Record<string, unknown>>
			| undefined;
		if (!Array.isArray(entries)) return;
		this.pendingMap.clear();
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
	 *  no-op that retries a known nondurable record's persist first. */
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
		this.persist(record);
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

	/** Every obligation not yet fully carried out. */
	pending(): PendingRecord[] {
		return [...this.pendingMap.values()].filter((record) =>
			!record.acknowledged);
	}

	/** Whether anything is owed — the gate's whole question. */
	owing(): boolean {
		return this.pending().length > 0;
	}

	/** The gate: no tool runs while an acknowledgement is owed, except
	 *  the two the acknowledgement itself needs. Fails closed: a state
	 *  read that cannot be trusted blocks rather than allows. */
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
		if (!this.owing()) return undefined;
		const name = String(event.toolName ?? "");
		if (name === "comms_ack" || name === "pre_daemon") return undefined;
		return {
			block: true,
			reason: CommsAcknowledgements.denial(
				this.pending().map((record) => record.messageId)),
		};
	}

	/** One acknowledgement turn, per id: the reply, best-effort and only
	 *  reported, then the service acknowledgement of every id the ledger
	 *  holds — the reply never holds an id back, and the recording opens
	 *  the gate. A retry resumes where it stopped. */
	async acknowledge(
		messageIds: string[],
		params: { node?: string },
		signal?: AbortSignal,
	): Promise<AckReport> {
		const ids = messageIds.map(String);
		const report: AckReport = {
			acknowledged: [],
			alreadyAcknowledged: [],
			unknown: [],
			replyFailed: [],
			serviceAckFailed: [],
		};
		const known: string[] = [];
		for (const id of ids) {
			const record = this.pendingMap.get(id);
			if (!record) {
				report.unknown.push(id);
				continue;
			}
			// The reply only goes out on a durably armed obligation: the
			// persist retries here, since nothing else redelivers mail the
			// branch already holds; a still nondurable record cannot give
			// the reply its request id. A reply that cannot be delivered
			// is reported — it never holds the id out of the service
			// acknowledgement, whose recording is what opens the gate.
			if (!record.durable) record.durable = this.persist(record);
			if (record.replySent ||
				(record.durable && await this.sendReply(record, signal))) {
				// The reply's traffic entry repairs itself from the record:
				// a progress append that survived without its log entry is
				// rewritten, so the branch never claims a reply it cannot
				// show.
				if (!this.repairReply(record)) report.replyFailed.push(id);
			} else {
				report.replyFailed.push(id);
			}
			known.push(id);
		}
		if (known.length === 0) return report;
		const mailbox = params.node ?? this.readyMailbox(known);
		const outcome = await this.cli.run(
			"ack",
			CommsArgv.ack({ messageIds: known, node: mailbox }),
			{ signal },
		);
		const answer = outcome.answer as
			| { acknowledged?: unknown; alreadyAcknowledged?: unknown;
				unknown?: unknown }
			| undefined;
		if (outcome.exitCode !== CommsExit.OK || !answer) {
			// The reply may have gone out, but the service markers did not
			// record. The gate stays for these ids and the next call
			// retries the ack.
			for (const id of known) report.serviceAckFailed.push(id);
			return report;
		}
		const settled = CommsAcknowledgements.strings(answer.acknowledged)
			.concat(CommsAcknowledgements.strings(answer.alreadyAcknowledged));
		const unsettled = CommsAcknowledgements.strings(answer.unknown);
		for (const id of known) {
			if (settled.includes(id)) {
				if (this.complete(id)) {
					report.acknowledged.push(id);
				} else {
					// The gate stays: the durable completion is its opening,
					// and the next comms_ack retries it.
					report.serviceAckFailed.push(id);
				}
			} else {
				report.serviceAckFailed.push(id);
				if (unsettled.includes(id)) report.unknown.push(id);
			}
		}
		return report;
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
		);
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

	/** Settle one id: the durable completion is the gate's opening. The
	 *  completed snapshot is appended before the live record is marked,
	 *  so a failed append leaves the obligation owed and reported. */
	private complete(id: string): boolean {
		const record = this.pendingMap.get(id);
		if (!record) return false;
		if (!this.persist({ ...record, acknowledged: true })) return false;
		record.acknowledged = true;
		record.durable = true;
		return true;
	}

	/** The mailbox the acknowledged ids belong to, when the caller named
	 *  no node: a remote mailbox stores on the node that held it. */
	private readyMailbox(ids: string[]): string | undefined {
		let mailbox: string | undefined;
		for (const id of ids) {
			const record = this.pendingMap.get(id);
			if (!record || !record.toNodeId) continue;
			const node = record.toNodeId;
			if (mailbox !== undefined && mailbox !== node) return undefined;
			mailbox = node;
		}
		return mailbox;
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
}
