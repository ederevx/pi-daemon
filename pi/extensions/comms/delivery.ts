/**
 * pi-daemon — background agent-comms reception.
 *
 * One long-running loop of ordinary CLI waits holds the session's
 * mailbox so mail arrives while the agent works instead of sitting
 * unread behind it. Every envelope flows through one ingestion path:
 * the branch log first, then the pending acknowledgement ledger, then —
 * for mail the tool result is not itself presenting — one steered
 * message that starts the acknowledgement turn. The receive cursor
 * advances only once the page's envelopes are durably recorded, and a
 * foreground `comms_wait` parks the receiver before its own hold, so
 * the two never race one mailbox.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { CommsCli, CommsOutcome } from "./client.ts";
import {
	CommsArgv,
	CommsExit,
	type CommsEnvelope,
	isProtocolAck,
} from "./protocol.ts";
import { CommsMessageLog } from "./message-log.ts";
import type { CommsAcknowledgements } from "./acknowledgements.ts";

/** The custom entry type persisted receive cursors are stored under. */
export const COMMS_CURSOR = "pi-agent-comms-cursor";

/** The custom type inbound delivery steers into the session under. */
export const COMMS_DELIVERY = "pi-agent-comms";

/** Tuning the receiver reads from the comms policy. */
export interface DeliveryPolicy {
	/** The hold the background relay asks for; the service caps it. */
	holdMs: number;
	retryInitialMs: number;
	retryMaxMs: number;
}

/** One ingest result: what was newly presented, and whether the page's
 *  receive cursor may advance. */
export interface IngestReport {
	/** Message ids presented to the model now, in page order. */
	presented: string[];
	/** The page's cursor sequence, when every envelope was recorded. */
	cursor?: number;
}

/** The session context ingestion reads the branch through; the live
 *  session's context is attached at session start. */
interface SessionContext {
	sessionManager?: {
		getBranch?: () => Array<Record<string, unknown>>;
		getEntries?: () => Array<Record<string, unknown>>;
	};
}

export class CommsDelivery {
	/** The loop's identity token; late outcomes of a stopped loop are
	 *  discarded the moment their await returns. */
	private generation = 0;
	private running = false;
	private task: Promise<void> | null = null;
	private controller: AbortController | null = null;
	/** The mailbox key the active child holds, when one holds. */
	private holding = "";
	/** Set while a foreground hold owns the mailbox: the receiver
	 *  stands at its safe point until the owner releases it. */
	private parked = false;
	private parkedResolve: (() => void) | null = null;
	/** The foreground lease queue: every acquire awaits the previous
	 *  owner's release, so one hold is ever in the mailbox and only the
	 *  owning caller's cancel can leave the queue. */
	private mailboxQueue: Promise<void> = Promise.resolve();
	/** One sleep handle: retry backoff, parked suspension and the stop
	 *  all cut it short through the same wake. */
	private timer: ReturnType<typeof setTimeout> | undefined;
	private wakeup: (() => void) | null = null;
	private backoffMs = 0;
	/** Set once the loop stopped on a permanent failure and said so. */
	private stopShown = false;
	/** Receive cursor per mailbox key ("node/agent"). */
	private cursors = new Map<string, number>();
	/** The cursor sequence already durable per mailbox key — the equal
	 *  advance is not appended twice, but an equal value never durably
	 *  written (its append refused) retries. */
	private persistedCursors = new Map<string, number>();
	/** Message ids this runtime already presented to the model. */
	private presented = new Set<string>();
	/** Set when the pending-ledger append failed: no cursor advance
	 *  until it clears, so unacknowledged mail is never skipped. */
	private faultLatch = false;
	private gapShown = false;
	private ctx: SessionContext | undefined;

	constructor(
		private readonly cli: CommsCli,
		private readonly log: CommsMessageLog,
		private readonly pi: ExtensionAPI,
		private readonly ledger: CommsAcknowledgements,
		private readonly policy: DeliveryPolicy,
	) {}

	/** The live session context delivery works through. */
	attach(ctx: unknown): void {
		this.ctx = ctx as SessionContext;
		this.restore();
		this.steerOutstanding();
	}

	/** Rebuild cursors and presentation memory from the active branch's own
	 *  entries, so a reload or a tree switch resumes where that branch left
	 *  off rather than replaying the mailbox or inheriting a sibling's place.
	 *  The session file holds every branch after a switch, and a cursor names
	 *  what one conversation already read; taking it from the file would let
	 *  a sibling's later record skip mail this branch never saw. The cursor is
	 *  the latest valid record per mailbox — a deliberate restart down to the
	 *  mailbox's surviving history is a record itself — and the mail the model
	 *  was shown lives in the ledger's own records. A branch that cannot be
	 *  read restores nothing: an empty cursor replays mail, a foreign one
	 *  skips it. */
	private restore(): void {
		let entries: Array<Record<string, unknown>> | undefined;
		try {
			const branch = this.ctx?.sessionManager?.getBranch?.();
			if (Array.isArray(branch)) entries = branch;
		} catch {
			entries = undefined;
		}
		const cursors = new Map<string, number>();
		const persisted = new Map<string, number>();
		for (const entry of entries ?? []) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_CURSOR) continue;
			const data = entry.data as Record<string, unknown> | undefined;
			const mailbox = typeof data?.mailbox === "string" ? data.mailbox : "";
			const sequence = data?.sequence;
			if (mailbox && typeof sequence === "number") {
				cursors.set(mailbox, sequence);
				persisted.set(mailbox, sequence);
			}
		}
		this.cursors = cursors;
		this.persistedCursors = persisted;
		this.presented = new Set(this.ledger.presentedIds());
	}

	/** Mail the branch owes an acknowledgement for but never showed —
	 *  a crash between the receive record and the presentation — is
	 *  steered now, at attach, before the tools can answer anything. */
	private steerOutstanding(): void {
		for (const record of this.ledger.pending()) {
			const id = record.messageId;
			if (this.presented.has(id)) continue;
			const payload = CommsMessageLog.receivedEntries(this.ctx)
				.find((entry) => entry.messageId === id)?.payload;
			const envelope: CommsEnvelope = {
				messageId: id,
				from: record.from,
				to: {
					agentId: record.toAgentId,
					nodeId: record.toNodeId || undefined,
				},
				sequence: record.sequence,
				kind: record.kind || "message",
				payload,
			};
			this.steer(envelope);
			this.presented.add(id);
			this.ledger.markPresented(id);
		}
	}

	/** Start the receiver for this session's identity; a session the
	 *  launch could not name has no mailbox to hold, so nothing runs. A
	 *  receiver stopped on a permanent failure stays stopped: the reason
	 *  is already on the session, and the comms tools report it. */
	start(): boolean {
		if (this.running) return true;
		if (this.stopShown) return false;
		if (!this.cli.identity()) return false;
		this.running = true;
		this.backoffMs = 0;
		this.gapShown = false;
		const generation = ++this.generation;
		this.task = this.loop(generation);
		return true;
	}

	/** Whether the receiver holds this session's mailbox. */
	active(): boolean {
		return this.running;
	}

	/** The receive cursor a foreground hold should continue from, when
	 *  none was named. */
	cursorHint(): number | undefined {
		if (!this.running || !this.cli.identity()) return undefined;
		return this.cursors.get(this.localKey());
	}

	/** The unacknowledged-mail fault latch, for the tool gate's report. */
	latched(): boolean {
		return this.faultLatch;
	}

	clearLatch(): void {
		this.faultLatch = false;
	}

	/** Stop the receiver: abort the hold, cancel the retry timer, and
	 *  wait for the loop to reach its next safe point. A foreground
	 *  owner reaches its safe point with the stop, and no stale park or
	 *  queue survives into a later start. */
	async stop(): Promise<void> {
		this.running = false;
		++this.generation;
		this.parked = false;
		this.parkedResolve?.();
		this.parkedResolve = null;
		this.mailboxQueue = Promise.resolve();
		this.wake();
		if (this.controller) this.controller.abort();
		await this.task;
		this.task = null;
		this.controller = null;
	}

	/** Own the mailbox for one foreground hold: queue behind any
	 *  previous owner, park the receiver at its safe point, and hand
	 *  back the idempotent release that stands the receiver up again.
	 *  A cancelled acquire never takes the turn: it leaves the queue
	 *  free and the receiver parked-or-unparked as it found it, and no
	 *  other caller's lease is affected. */
	async acquireMailbox(signal?: AbortSignal): Promise<() => void> {
		const prev = this.mailboxQueue;
		let release!: () => void;
		const mine = new Promise<void>((resolve) => { release = resolve; });
		// The turn passes through the queue in order: callers chain on the
		// whole predecessor line, so a cancelled caller standing down
		// without the turn cannot hand the mailbox to its successor.
		this.mailboxQueue = prev.then(() => mine);
		const turn = signal
			? await CommsDelivery.aborted(signal, prev)
			: (await prev, false);
		if (turn || (signal?.aborted ?? false)) {
			release();
			throw new DOMException(
				"This operation was aborted", "AbortError");
		}
		if (!this.running) return release;
		// Park the receiver and wait out its safe point — the loop stands
		// on this promise the next time it tops out without a hold. The
		// aborted child has not necessarily exited yet, so the wait is
		// unconditional: no caller acquires over a live hold. No other
		// caller parks concurrently: the queue settled the turn.
		this.parked = true;
		if (this.controller) this.controller.abort();
		this.wake();
		const safe = new Promise<void>((resolve) => {
			this.parkedResolve = resolve;
		});
		const cancelled = signal
			? await CommsDelivery.aborted(signal, safe)
			: (await safe, false);
		if (cancelled || (signal?.aborted ?? false) || !this.running) {
			this.unpark();
			release();
			throw new DOMException(
				"This operation was aborted", "AbortError");
		}
		let released = false;
		return () => {
			if (released) return;
			released = true;
			this.unpark();
			release();
		};
	}

	/** Stand the receiver up after a foreground hold released it. */
	private unpark(): void {
		this.parked = false;
		this.parkedResolve = null;
		this.wake();
	}

	// -- ingestion -----------------------------------------------------------

	/** One shared path for every answer carrying mail: log each
	 *  envelope, arm the acknowledgement obligation, and — when the
	 *  tool result is not itself the presentation — steer one message
	 *  that starts the acknowledgement turn. The cursor advances only
	 *  when every envelope of the page was durably recorded, and the
	 *  mailbox the page was read from owns that cursor: the local
	 *  service when no node was named, that node's mailbox otherwise. */
	async ingest(
		answer: unknown,
		options: { steer: boolean; node?: string },
	): Promise<IngestReport> {
		const envelopes = CommsMessageLog.envelopes(answer);
		const report: IngestReport = { presented: [] };
		let recorded = true;
		for (const envelope of envelopes as CommsEnvelope[]) {
			const appended = this.log.appendEnvelope(envelope, this.ctx as never);
			const id = envelope.messageId;
			if (!appended) recorded = false;
			if (isProtocolAck(envelope)) {
				// A protocol acknowledgement is bookkeeping: recorded,
				// service-acknowledged, but no reply and no gate. A failed
				// recording repeats; a repeated service acknowledgement is
				// `alreadyAcknowledged`, so the retry is side-effect free.
				this.presented.add(id);
				if (appended) await this.ledger.protocolAck(envelope);
				continue;
			}
			if (!appended) continue;
			if (!this.ledger.markPending(envelope)) this.faultLatch = true;
			if (this.presented.has(id)) continue;
			report.presented.push(id);
			if (options.steer) this.steer(envelope);
			this.presented.add(id);
			this.ledger.markPresented(id);
		}
		const cursor = this.pageCursor(answer);
		if (cursor !== null && recorded && !this.faultLatch) {
			report.cursor = cursor;
		}
		return report;
	}

	/** Record the receive cursor a foreground page read from, keyed by
	 *  the mailbox it was read from: the local service's mailbox when no
	 *  node was named, that node's mailbox otherwise. The next hold —
	 *  foreground or resumed background — continues after it. */
	acceptCursor(sequence: number, node?: string): void {
		this.recordCursor(this.mailboxKey(node), sequence);
	}

	/** The cursor sequence of one replay/wait page, when it carries one. */
	private pageCursor(answer: unknown): number | null {
		const page = Array.isArray(answer) ? answer.at(-1) : answer;
		const cursor = (page as Record<string, unknown> | undefined)
			?.cursor as Record<string, unknown> | undefined;
		const sequence = cursor?.sequence;
		return typeof sequence === "number" ? sequence : null;
	}

	/** The steered message the model reads: the report itself, with the
	 *  exact acknowledgement it owes before anything else. */
	private steer(envelope: CommsEnvelope): void {
		this.pi.sendMessage({
			customType: COMMS_DELIVERY,
			content: [
				`agent-comms message from ${CommsMessageLog.address(envelope.from)} ` +
					`(kind ${envelope.kind}, message ${envelope.messageId}):`,
				typeof envelope.payload === "string"
					? envelope.payload
					: JSON.stringify(envelope.payload, null, 2),
				"",
				"You must acknowledge this message before doing anything " +
					"else — every other tool stays blocked until then:",
				`comms_ack { messageIds: ["${envelope.messageId}"] }`,
			].join("\n"),
			display: true,
			details: {
				messageId: envelope.messageId,
				mailbox: envelope.to,
			},
		}, { triggerTurn: true, deliverAs: "steer" });
	}

	// -- the loop ------------------------------------------------------------

	private async loop(generation: number): Promise<void> {
		while (this.running && generation === this.generation) {
			// The safe point a foreground owner waits for: no hold active.
			if (this.parked) {
				this.parkedResolve?.();
				this.parkedResolve = null;
				await this.sleep();
				continue;
			}
			const identity = this.cli.identity();
			if (!identity) return;
			const key = this.localKey();
			const after = this.cursors.get(key) ?? 0;
			const controller = new AbortController();
			this.controller = controller;
			this.holding = key;
			let outcome: CommsOutcome | undefined;
			try {
				outcome = await this.cli.run(
					"wait",
					CommsArgv.wait({ after, timeoutMs: this.policy.holdMs }),
					{ signal: controller.signal },
				);
			} catch {
				// The runner failed before it produced an outcome: treat as
				// any other hold failure, through the backoff.
			}
			this.controller = null;
			this.holding = "";
			if (generation !== this.generation) return;
			// A foreground owner has the mailbox: the safe point is next,
			// without any retry delay.
			if (this.parked) continue;
			if (outcome && outcome.exitCode === CommsExit.OK) {
				this.backoffMs = 0;
				const report = await this.ingest(outcome.answer, { steer: true });
				if (report.cursor !== undefined) {
					this.recordCursor(key, report.cursor);
				}
				continue;
			}
			if (outcome?.exitCode === CommsExit.DECLINED &&
					(outcome.answer as { status?: string } | undefined)
						?.status === "cursor-expired") {
				await this.restartFromOldest(outcome);
				continue;
			}
			if (outcome?.exitCode === CommsExit.TIMED_OUT) {
				this.backoffMs = 0;
				continue;
			}
			// Permanent failures stop the receiver; the next comms tool
			// reports the reason instead of the loop repeating it.
			if (outcome && (outcome.missingBinary ||
					outcome.exitCode === CommsExit.USAGE ||
					outcome.exitCode === CommsExit.UNAUTHORIZED)) {
				this.reportStop(outcome);
				this.running = false;
				return;
			}
			// A killed child is the cancellation the parking or shutdown
			// itself initiated: the next hold stands without delay.
			if (outcome?.killed) {
				this.backoffMs = 0;
				continue;
			}
			// A runner exception, or a child that ended without an exit
			// code, is an unexpected failure: bounded backoff, no spin.
			await this.backoff();
		}
		if (this.parkedResolve) {
			this.parkedResolve();
			this.parkedResolve = null;
		}
	}

	/** Bounded backoff between failing holds, doubling from the policy's
	 *  initial step. Repeated empty waits reset it. */
	private async backoff(): Promise<void> {
		this.backoffMs = Math.min(
			this.backoffMs > 0 ? this.backoffMs * 2 : this.policy.retryInitialMs,
			this.policy.retryMaxMs);
		await this.sleep(this.backoffMs);
	}

	/** Cursor-expired: the mailbox lost the retained history this
	 *  session's cursor points into. Replay from the oldest surviving
	 *  sequence instead of silently skipping to the head, and say so
	 *  once per runtime. */
	private async restartFromOldest(outcome: CommsOutcome): Promise<void> {
		const oldest = (outcome.answer as
			{ oldestAvailableSequence?: unknown } | undefined)
			?.oldestAvailableSequence;
		if (typeof oldest === "number") {
			const key = this.localKey();
			// The restart lowers the cursor on purpose; the durable position
			// is re-tracked from this record, so the next advance is
			// rewritten from scratch.
			const sequence = Math.max(oldest - 1, 0);
			this.cursors.set(key, sequence);
			this.persistedCursors.delete(key);
			try {
				this.pi.appendEntry(COMMS_CURSOR, { mailbox: key, sequence });
			} catch {
				// The in-memory cursor still guides the next hold; the
				// restart entry is rewritten by a later advance.
			}
			if (!this.gapShown) {
				this.gapShown = true;
				this.pi.sendMessage({
					customType: COMMS_DELIVERY,
					content:
						"agent-comms: the receive history this session tracked is " +
						`no longer retained; resuming from sequence ${oldest}.`,
					display: true,
				});
			}
		}
		this.backoffMs = 0;
	}

	/** One report when the receiver stops for good: the reason, and
	 *  what to do about it. Never repeated. */
	private reportStop(outcome: CommsOutcome): void {
		if (this.stopShown) return;
		this.stopShown = true;
		const reason = outcome.missingBinary
			? `pi-comms not found at ${outcome.missingBinary}`
			: `pi-comms wait failed: exit ${outcome.exitCode} ` +
				`(${CommsExit.label(outcome.exitCode)})` +
				(outcome.stderr.trim()
					? ` — ${outcome.stderr.trim().split("\n")[0]}`
					: "");
		this.pi.sendMessage({
			customType: COMMS_DELIVERY,
			content:
				"agent-comms reception stopped: " + reason +
				". Incoming mail is no longer received while the agent works; " +
				"comms_wait still works, and comms_send reports the same reason.",
			display: true,
		});
	}

	/** The receive cursor is durable per mailbox: one append per
	 *  advance, the latest record stands on restore. An advance whose
	 *  append is refused keeps the in-memory value only, and the equal
	 *  advance retries it because the durable position lagged. */
	private recordCursor(key: string, sequence: number): void {
		const durable = this.persistedCursors.get(key);
		if (durable !== undefined && durable >= sequence) return;
		this.cursors.set(key, sequence);
		try {
			this.pi.appendEntry(COMMS_CURSOR, { mailbox: key, sequence });
			this.persistedCursors.set(key, sequence);
		} catch {
			// The in-memory cursor still advances; the next advance — the
			// same value included — rewrites the durable record.
		}
	}

	/** One sleep cut short by `wake`, or by nothing and resolved later
	 *  from the safe-point path. */
	private async sleep(ms?: number): Promise<void> {
		await new Promise<void>((resolve) => {
			if (ms !== undefined) {
				this.timer = setTimeout(resolve, ms);
			}
			this.wakeup = resolve;
		});
		this.timer = undefined;
		this.wakeup = null;
	}

	private wake(): void {
		if (this.timer !== undefined) {
			clearTimeout(this.timer);
			this.timer = undefined;
		}
		const wakeup = this.wakeup;
		this.wakeup = null;
		wakeup?.();
	}

	private localKey(): string {
		return `@${this.cli.identity()}`;
	}

	/** The mailbox key a page's cursor is stored under: the local
	 *  mailbox when no node was named, that node's mailbox otherwise. */
	private mailboxKey(node?: string): string {
		const identity = this.cli.identity();
		return node && identity ? `${node}/@${identity}` : this.localKey();
	}

	/** A promise that resolves `true` when the signal aborts and
	 *  `false` when the settled promise wins the race, whose listener is
	 *  then removed. Checks `aborted` before registering. */
	private static aborted(
		signal: AbortSignal,
		settled: Promise<unknown>,
	): Promise<boolean> {
		return new Promise<boolean>((resolve) => {
			if (signal.aborted) {
				resolve(true);
				return;
			}
			const onAbort = () => resolve(true);
			const onSettled = () => {
				signal.removeEventListener("abort", onAbort);
				resolve(false);
			};
			signal.addEventListener("abort", onAbort, { once: true });
			settled.then(onSettled, onSettled);
		});
	}
}
