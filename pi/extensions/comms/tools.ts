/**
 * pi-daemon — the agent-comms tool set.
 *
 * Six model-facing tools over the `agent-comms` service: send, wait,
 * replay, acknowledge, spawn and peer list. Each one shapes the argv for
 * `pi/bin/pi-comms`, runs it through `CommsCli`, and returns the
 * service's JSON answer as the structured result. A non-zero exit is a
 * tool error carrying the CLI's stderr and exit code; an empty wait
 * (exit 3) is a page with no mail, not a failure.
 *
 * The wait is one of pi's blocking waits, aligned with the other
 * blocking tools: it publishes the session's waiting state, streams
 * elapsed-time updates through the tool's own update channel, yields
 * when a caller abort or a queued message arrives, and parks the
 * background receiver for its mailbox so one hold is ever in it. Sends
 * and acknowledgements pass through the message log and the mandatory
 * acknowledgement ledger; when the background receiver is present,
 * every answer is ingested exactly once through it.
 */

import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import type {
	ExtensionAPI,
	ExtensionToolContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { CommsCli, type CommsOutcome } from "./client.ts";
import { type CommsAcknowledgements } from "./acknowledgements.ts";
import { type CommsDelivery } from "./delivery.ts";
import { CommsMessageLog } from "./message-log.ts";
import {
	CommsArgv,
	CommsExit,
	type CommsAckParams,
	type CommsPeersParams,
	type CommsReplayParams,
	type CommsSendParams,
	type CommsSpawnParams,
	type CommsWaitParams,
} from "./protocol.ts";

/** One tool's answer, already rendered for pi. */
interface CommsToolResult {
	content: Array<{ type: "text"; text: string }>;
	structuredContent?: unknown;
	details: Record<string, unknown>;
	isError?: boolean;
}

/** The wait's streamed tool-window update. */
type ToolUpdate = (update: {
	content: Array<{ type: "text"; text: string }>;
	details: Record<string, unknown>;
}) => void;

/** The optional machinery the composition wires in: the background
 *  receiver, the acknowledgement ledger, and the session-state hook the
 *  foreground wait publishes through. */
export interface CommsToolOptions {
	delivery?: CommsDelivery;
	acks?: CommsAcknowledgements;
	/** Publishes "waiting" for a foreground hold; the tool restores. */
	setWaitState?: (waiting: boolean) => void;
	waitUpdateMs?: number;
}

/** Why one foreground wait stopped. */
type WaitStopReason = "message" | "timeout" | "yield" | "abort" | "closed";

export class CommsTools {
	static readonly SEND = "comms_send";
	static readonly WAIT = "comms_wait";
	static readonly REPLAY = "comms_replay";
	static readonly ACK = "comms_ack";
	static readonly SPAWN = "comms_spawn";
	static readonly PEERS = "peers";

	/** The model-facing answer is a compact JSON line; keep it bounded like
	 *  the transcript tool bounds a page. */
	private static readonly MAX_TEXT = 60000;

	/** The wait's live-update cadence, presentational only. */
	private static readonly WAIT_UPDATE_MS = 250;

	constructor(
		private readonly cli: CommsCli,
		private readonly log: CommsMessageLog,
		private readonly options: CommsToolOptions = {},
	) {}

	/** Register all six tools with pi. */
	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: CommsTools.SEND,
			label: "comms send",
			description:
				"Send one direct agent-comms message to another agent through " +
				"the pi-daemon service. A string payload is sent as plain text; " +
				"any other value travels as JSON.",
			parameters: Type.Object({
				to: Type.String({ description: "The recipient agent id." }),
				payload: Type.Unknown({
					description: "A JSON value; a string is sent as plain text.",
				}),
				node: Type.Optional(Type.String({
					description: "The recipient's node (default: this node).",
				})),
				at: Type.Optional(Type.String({
					description: "Run the send where that peer is, as this agent.",
				})),
				requestId: Type.Optional(Type.String({
					description: "Reuse this id to retry one send safely.",
				})),
				kind: Type.Optional(Type.String({
					description: "The message kind (default: message).",
				})),
			}),
			annotations: { readOnlyHint: false, openWorldHint: false },
			execute: async (
				_toolCallId, params: CommsSendParams, signal, _onUpdate, ctx,
			) => this.send(params, signal, ctx),
		});

		pi.registerTool({
			name: CommsTools.WAIT,
			label: "comms wait",
			description:
				"Hold until agent-comms mail arrives, bounded by the service's " +
				"own wait timeout, and return the page and its cursor. Ends on " +
				"the first mail, a caller abort, or a queued message; the wait " +
				"continues from the mailbox's receive cursor when none is named. " +
				"An empty wait is a page with no messages, not an error.",
			parameters: Type.Object({
				after: Type.Optional(Type.Integer({
					description: "Read the mailbox after this sequence.",
				})),
				limit: Type.Optional(Type.Integer({
					description: "At most this many messages in the page.",
				})),
				timeoutMs: Type.Optional(Type.Integer({
					minimum: 0,
					description: "How long the service may hold the request.",
				})),
				node: Type.Optional(Type.String({
					description: "Wait on the mailbox kept for this agent there.",
				})),
			}),
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (
				_toolCallId, params: CommsWaitParams, signal, onUpdate, ctx,
			) => this.wait(params, signal, onUpdate, ctx),
		});

		pi.registerTool({
			name: CommsTools.REPLAY,
			label: "comms replay",
			description:
				"Read the agent-comms mailbox from a cursor, or with `all` " +
				"follow every page. Returns the page(s) and the cursor.",
			parameters: Type.Object({
				after: Type.Optional(Type.Integer({
					description: "Read the mailbox after this sequence.",
				})),
				limit: Type.Optional(Type.Integer({
					description: "At most this many messages per page.",
				})),
				all: Type.Optional(Type.Boolean({
					description: "Follow every page from the cursor.",
				})),
				node: Type.Optional(Type.String({
					description: "Read the mailbox kept for this agent there.",
				})),
			}),
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (
				_toolCallId, params: CommsReplayParams, signal, _update, ctx,
			) => this.replay(params, signal, ctx),
		});

		pi.registerTool({
			name: CommsTools.ACK,
			label: "comms ack",
			description:
				"Acknowledge delivered agent-comms messages by message id. Each " +
				"id is first answered with one acknowledgement reply to its " +
				"sender, sent best-effort: a reply that cannot be delivered is " +
				"reported, but does not hold the gate. The acknowledgement gate " +
				"opens only once the service acknowledgement of every delivered " +
				"id is recorded.",
			parameters: Type.Object({
				messageIds: Type.Array(Type.String(), {
					minItems: 1,
					description: "The message ids to acknowledge.",
				}),
				node: Type.Optional(Type.String({
					description: "Acknowledge mail in the mailbox kept for this agent there.",
				})),
			}),
			annotations: { readOnlyHint: false, openWorldHint: false },
			execute: async (
				_toolCallId, params: CommsAckParams, signal, _update, ctx,
			) => this.ack(params, signal, ctx),
		});

		pi.registerTool({
			name: CommsTools.SPAWN,
			label: "comms spawn",
			description:
				"Launch another agent through the pi-daemon's comms launcher: " +
				"a directory and either the argv to run there or one `task` for " +
				"a fresh `pi`, optionally under an explicit agent id and a " +
				"caller-chosen request id so a lost answer retries instead of " +
				"launching twice. Use it to run the independently bounded " +
				"units of one task in parallel sessions, coordinated by " +
				"comms_send, comms_wait and comms_replay. It is the stand-in " +
				"for the in-session child-agent tool, and the more independent " +
				"one: a spawned session keeps its own conversation and can be " +
				"reached again after it answers.",
			parameters: Type.Object({
				agentId: Type.Optional(Type.String({
					description: "The identity the launched agent binds.",
				})),
				requestId: Type.Optional(Type.String({
					description: "Reuse this id to retry one launch safely.",
				})),
				dir: Type.String({ description: "The working directory." }),
				argv: Type.Optional(Type.Array(Type.String(), {
					minItems: 1,
					description: "The command and its arguments.",
				})),
				task: Type.Optional(Type.String({
					description: "One task for a fresh pi conversation.",
				})),
				node: Type.Optional(Type.String({
					description: "Launch where that peer is; dir is then that " +
						"node's own path, taken verbatim.",
				})),
			}),
			annotations: { readOnlyHint: false, openWorldHint: false },
			execute: async (
				_toolCallId, params: CommsSpawnParams, signal, _onUpdate, ctx,
			) => this.spawn(params, signal, ctx),
		});

		pi.registerTool({
			name: CommsTools.PEERS,
			label: "peers",
			description:
				"List the agent-comms peers this node is configured to reach.",
			parameters: Type.Object({
				node: Type.Optional(Type.String({
					description: "List the peers that node is configured to reach.",
				})),
			}),
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (_toolCallId, params, signal) =>
				this.peers(params as CommsPeersParams, signal),
		});
	}

	private async send(
		params: CommsSendParams,
		signal?: AbortSignal,
		ctx?: ExtensionToolContext,
	): Promise<CommsToolResult> {
		const requestId = params.requestId || randomUUID();
		const argv = CommsArgv.send({ ...params, requestId });
		const outcome = await this.cli.run(CommsTools.SEND, argv, { signal });
		const recorded = this.logSend(params, requestId, outcome, ctx);
		return this.result(outcome, { requestId, recorded }, outcome.exitCode === CommsExit.OK);
	}

	/** Record a send the service accepted or queued, so the session
	 *  shows its own mail as it moved; a failed send is kept out of the
	 *  log and stays a tool error. The log answers whether the traffic
	 *  entry is durable — a refused append is surfaced in the result
	 *  instead of silently weakening the branch's record. */
	private logSend(
		params: CommsSendParams,
		requestId: string,
		outcome: CommsOutcome,
		ctx?: ExtensionToolContext,
	): boolean {
		const status = (outcome.answer as { status?: string } | undefined)
			?.status;
		if (status !== "accepted" && status !== "queued") return false;
		return this.log.sent({
			to: { agentId: params.to, nodeId: params.node ?? "" },
			kind: params.kind,
			requestId,
			payload: params.payload,
			status,
		}, ctx);
	}

	private async wait(
		params: CommsWaitParams,
		signal: AbortSignal | undefined,
		onUpdate: ToolUpdate | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<CommsToolResult> {
		// A steer already queued ends the wait now: the mail it carries
		// is the answer.
		if (ctx?.hasPendingMessages?.()) {
			return this.stoppedWait("yield");
		}
		if (signal?.aborted) return this.stoppedWait("abort");
		// One hold per mailbox: take the mailbox lease before the
		// foreground hold, and release it in `finally` — only the owning
		// call's release stands the receiver up again.
		const parksReceiver = !params.node
			&& this.options.delivery?.active() === true;
		let release: (() => void) | undefined;
		if (parksReceiver) {
			try {
				release = await this.options.delivery?.acquireMailbox(signal);
			} catch (error) {
				if (signal?.aborted) return this.stoppedWait("abort");
				throw error;
			}
		}
		// Re-checked after the park: a caller abort or a queued steer
		// that arrived while the lease was taken ends the wait here,
		// before its own hold begins.
		if (ctx?.hasPendingMessages?.()) {
			release?.();
			return this.stoppedWait("yield");
		}
		if (signal?.aborted) {
			release?.();
			return this.stoppedWait("abort");
		}
		try {
			return await this.hold(params, signal, onUpdate, ctx);
		} finally {
			release?.();
		}
	}

	/** One foreground hold: the CLI long-poll, streamed elapsed time,
	 *  publish waiting state, early yield on queued mail. */
	private async hold(
		params: CommsWaitParams,
		signal: AbortSignal | undefined,
		onUpdate: ToolUpdate | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<CommsToolResult> {
		// Without a named position the hold continues where the mailbox's
		// receive cursor stands, so history is never replayed as new mail.
		const effective: CommsWaitParams = params.after === undefined
			&& params.node === undefined
			&& this.options.delivery
			? { ...params, after: this.options.delivery.cursorHint() }
			: params;
		const updateMs = this.options.waitUpdateMs
			?? CommsTools.WAIT_UPDATE_MS;
		const controller = new AbortController();
		let yielded = false;
		let callerAborted = false;
		const onCallerAbort = () => {
			callerAborted = true;
			controller.abort();
		};
		if (signal) {
			if (signal.aborted) onCallerAbort();
			else signal.addEventListener("abort", onCallerAbort, { once: true });
		}
		const startedAt = performance.now();
		const tick = setInterval(() => {
			if (ctx?.hasPendingMessages?.()) {
				yielded = true;
				controller.abort();
				return;
			}
			onUpdate?.({
				content: [{
					type: "text",
					text: `Waiting for agent-comms mail (` +
						`${Math.floor((performance.now() - startedAt) / 1000)}s)`,
				}],
				details: { stoppedBy: undefined, waitedMs: Math.round(performance.now() - startedAt) },
			});
		}, updateMs);
		this.options.setWaitState?.(true);
		let outcome: CommsOutcome;
		try {
			outcome = await this.cli.run(
				CommsTools.WAIT, CommsArgv.wait(effective),
				{ signal: controller.signal });
		} finally {
			clearInterval(tick);
			if (signal) signal.removeEventListener("abort", onCallerAbort);
			this.options.setWaitState?.(false);
		}
		// Early stop, or a genuine CLI failure: no answer of record. The
		// wait already ended, so the elapsed time is still the truth.
		if (yielded) {
			return this.stoppedWait("yield",
				Math.round(performance.now() - startedAt));
		}
		if (callerAborted || (signal?.aborted ?? false)) {
			return this.stoppedWait("abort",
				Math.round(performance.now() - startedAt));
		}
		const badChild = outcome.killed || outcome.exitCode === null;
		if (badChild) {
			return this.result(outcome, { stoppedBy: "closed" as WaitStopReason });
		}
		const report = await this.ingest(outcome, ctx, {
			node: params.node,
			live: params.after === undefined,
		});
		const answer = outcome.answer as { messages?: unknown[] } | undefined;
		const delivered = outcome.exitCode === CommsExit.OK
			&& Array.isArray(answer?.messages) && answer.messages.length > 0;
		const empty = outcome.exitCode === CommsExit.TIMED_OUT
			|| (outcome.exitCode === CommsExit.OK && answer?.messages !== undefined
				&& Array.isArray(answer.messages) && answer.messages.length === 0);
		const stoppedBy: WaitStopReason = delivered ? "message" : "timeout";
		return this.result(outcome, {
			stoppedBy,
			waitedMs: Math.round(performance.now() - startedAt),
			...(report ? { presented: report.presented } : {}),
		}, empty || outcome.exitCode === CommsExit.OK);
	}

	/** Ingest through the receiver when one is wired in; otherwise log
	 *  the answer the legacy way, without steering (the tool result is
	 *  the presentation). A page read from the live receive position
	 *  commits its cursor through the receiver, keyed by the mailbox it
	 *  was read from; a replay asks history explicitly and does not
	 *  move that position. */
	private async ingest(
		outcome: CommsOutcome,
		ctx: ExtensionToolContext | undefined,
		options: { node?: string; live: boolean } = { live: false },
	): Promise<{ presented: string[] } | undefined> {
		const delivery = this.options.delivery;
		if (delivery) {
			const report = await delivery.ingest(outcome.answer, {
				steer: false,
				node: options.node,
			});
			if (options.live && report.cursor !== undefined) {
				delivery.acceptCursor(report.cursor, options.node);
			}
			return report;
		}
		this.log.record(outcome.answer, ctx);
		return undefined;
	}

	/** An early stop: no mail taken, nothing lost; a stop that ended a
	 *  hold still reports how long it held. */
	private stoppedWait(
		stoppedBy: "yield" | "abort",
		waitedMs = 0,
	): CommsToolResult {
		return {
			content: [{
				type: "text",
				text: stoppedBy === "yield"
					? "comms_wait yielded: a message was already queued; read it now."
					: "comms_wait aborted before the hold began.",
			}],
			structuredContent: { status: "yielded", stoppedBy },
			details: { stoppedBy, waitedMs },
		};
	}

	private async replay(
		params: CommsReplayParams,
		signal: AbortSignal | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<CommsToolResult> {
		const outcome = await this.cli.run(
			CommsTools.REPLAY, CommsArgv.replay(params),
			{ signal });
		const report = await this.ingest(outcome, ctx);
		return this.result(outcome, {
			presented: report?.presented ?? CommsMessageLog.receivedIds(ctx),
		});
	}

	private async ack(
		params: CommsAckParams,
		signal: AbortSignal | undefined,
		_ctx: ExtensionToolContext | undefined,
	): Promise<CommsToolResult> {
		const acks = this.options.acks;
		if (acks) {
			this.options.delivery?.clearLatch();
			const report = await acks.acknowledge(
				params.messageIds, { node: params.node }, signal);
			const settled = report.acknowledged.length
				+ report.alreadyAcknowledged.length > 0;
			const text = CommsTools.ackText(report);
			return {
				content: [{ type: "text", text }],
				structuredContent: report,
				details: { report },
				isError: !settled,
			};
		}
		const outcome = await this.cli.run(
			CommsTools.ACK, CommsArgv.ack(params), { signal });
		return this.result(outcome, {}, outcome.exitCode === CommsExit.OK);
	}

	/** The acknowledgement report as model text: what completed, what
	 *  remains owed, and the exact retry when something does. */
	private static ackText(report: {
		acknowledged: string[];
		alreadyAcknowledged: string[];
		unknown: string[];
		replyFailed: string[];
		serviceAckFailed: string[];
	}): string {
		const lines: string[] = [];
		if (report.acknowledged.length) {
			lines.push(`Acknowledged: ${report.acknowledged.join(", ")}`);
		}
		if (report.alreadyAcknowledged.length) {
			lines.push(`Already acknowledged: ` +
				report.alreadyAcknowledged.join(", "));
		}
		if (report.unknown.length) {
			lines.push(`Not received here — replay first, then acknowledge: ` +
				report.unknown.join(", "));
		}
		if (report.replyFailed.length) {
			lines.push(`The acknowledgement reply was not accepted for: ` +
				`${report.replyFailed.join(", ")}; the reply retries with ` +
				"the same request id on the next comms_ack.");
		}
		if (report.serviceAckFailed.length) {
			lines.push(`Acknowledged by reply but not recorded: ` +
				`${report.serviceAckFailed.join(", ")} — the tools stay ` +
				"blocked for these until comms_ack records them.");
		}
		return lines.join("\n");
	}

	private async spawn(
		params: CommsSpawnParams,
		signal: AbortSignal | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<CommsToolResult> {
		// One task means one of them: an empty argv is not a command, so it
		// leaves `task` alone rather than reaching the CLI as a conflict.
		const argv = params.argv && params.argv.length > 0
			? params.argv
			: undefined;
		if ((params.task === undefined) === (argv === undefined)) {
			return CommsTools.refusal(
				"comms_spawn takes exactly one of argv or task.");
		}
		const dir = params.node
			? params.dir
			: resolve(ctx?.cwd ?? process.cwd(), params.dir);
		const requestId = params.requestId || randomUUID();
		const outcome = await this.cli.run(
			CommsTools.SPAWN,
			CommsArgv.spawn({ ...params, dir, requestId, argv }),
			{ signal });
		return this.result(outcome, { requestId });
	}

	/** A request the tool itself refuses, before any CLI runs. */
	private static refusal(text: string): CommsToolResult {
		return {
			content: [{ type: "text", text }],
			isError: true,
			details: {},
		};
	}

	private async peers(
		params: CommsPeersParams,
		signal?: AbortSignal,
	): Promise<CommsToolResult> {
		const outcome = await this.cli.run(
			CommsTools.PEERS, CommsArgv.peers(params.node), { signal });
		return this.result(outcome, {});
	}

	/** Render one outcome: the JSON answer on success, the CLI's stderr and
	 *  exit code on failure, with the answer kept in details either way. */
	private result(
		outcome: CommsOutcome,
		extra: Record<string, unknown>,
		accepted = false,
	): CommsToolResult {
		const ok = (outcome.exitCode === CommsExit.OK
			&& outcome.answer !== undefined) || accepted;
		const details = {
			command: outcome.command,
			argv: outcome.argv,
			exitCode: outcome.exitCode,
			stderr: outcome.stderr,
			answer: outcome.answer,
			...extra,
		};
		if (!ok) {
			return {
				content: [{ type: "text", text: CommsTools.failure(outcome) }],
				isError: true,
				details,
			};
		}
		return {
			content: [{ type: "text", text: CommsTools.answer(outcome.answer) }],
			structuredContent: outcome.answer,
			details,
		};
	}

	/** The success text: the service's answer, compactly. */
	private static answer(value: unknown): string {
		if (value === undefined) return "ok";
		let text: string;
		try {
			text = JSON.stringify(value);
		} catch {
			text = String(value);
		}
		return text.length > CommsTools.MAX_TEXT
			? `${text.slice(0, CommsTools.MAX_TEXT)}\n… truncated (${text.length} chars)`
			: text;
	}

	/** The failure text: what failed, the exit code, and the CLI's stderr. */
	private static failure(outcome: CommsOutcome): string {
		const head = outcome.missingBinary
			? `pi-comms not found at ${outcome.missingBinary}`
			: `pi-comms ${outcome.command} failed: exit ${outcome.exitCode} ` +
				`(${outcome.killed ? "cancelled" : CommsExit.label(outcome.exitCode)})`;
		const lines = [head];
		if (outcome.stderr.trim()) lines.push(outcome.stderr.trim());
		if (outcome.answer !== undefined) {
			lines.push(CommsTools.answer(outcome.answer));
		}
		return lines.join("\n");
	}
}
