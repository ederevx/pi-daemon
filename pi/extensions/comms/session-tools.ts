/**
 * pi-daemon — the session-reach tools over agent-comms.
 *
 * `comms_sessions` lists what a node hosts and which conversations
 * `/resume` would offer there; `comms_reach` opens one — adopting the live
 * run, or hosting the conversation on that node — and ends one;
 * `comms_input` types into a hosted run; `comms_session_wait` holds on one
 * until it settles, streaming what it does.
 *
 * A session wait is one of pi's blocking waits: it publishes the waiting
 * state, streams the session's activity through the tool's update channel,
 * and yields when a caller abort or a queued message arrives. It is
 * read-only, so it never takes the mailbox lease the receiver owns.
 */

import { resolve } from "node:path";
import type {
	ExtensionAPI,
	ExtensionToolContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { CommsCli, type CommsOutcome } from "./client.ts";
import {
	CommsArgv,
	CommsExit,
	type CommsInputParams,
	type CommsReachParams,
	type CommsSessionWaitParams,
	type CommsSessionsParams,
} from "./protocol.ts";
import { SessionWaitFeed, type SessionWaitPage } from "./session-wait-feed.ts";

/** One tool's answer, already rendered for pi. */
interface SessionToolResult {
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

/** What the composition may wire in; all of it is optional. */
export interface CommsSessionOptions {
	/** Publishes "waiting" for a session wait; the tool restores on exit. */
	setWaitState?: (waiting: boolean) => void;
	waitUpdateMs?: number;
}

/** How one call's chain of held pages ended. */
interface WaitEnd {
	status: string;
	cursor?: unknown;
	nodeId?: string;
	failed?: CommsOutcome;
}

export class CommsSessionTools {
	static readonly SESSIONS = "comms_sessions";
	static readonly REACH = "comms_reach";
	static readonly INPUT = "comms_input";
	static readonly WAIT = "comms_session_wait";

	/** The wait's live-update cadence, presentational only. */
	private static readonly UPDATE_MS = 250;
	private static readonly MAX_TEXT = 60000;

	constructor(
		private readonly cli: CommsCli,
		private readonly options: CommsSessionOptions = {},
	) {}

	/** Register the four tools with pi. */
	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: CommsSessionTools.SESSIONS,
			label: "comms sessions",
			description:
				"List what a node can be reached for, over agent-comms: the " +
				"sessions it hosts (`view: live`, with `all` the records whose " +
				"run ended and can still be revived), or the conversations " +
				"`/resume` offers there (`view: conversations`, optionally " +
				"narrowed by `dir`). `node` asks another peer; without it the " +
				"answer is this node's own.",
			parameters: Type.Object({
				view: Type.Optional(Type.Union(
					[Type.Literal("live"), Type.Literal("conversations")],
					{ description: "What to list; live by default." },
				)),
				all: Type.Optional(Type.Boolean({
					description: "For live: include records whose run ended.",
				})),
				dir: Type.Optional(Type.String({
					description: "For conversations: only this directory.",
				})),
				limit: Type.Optional(Type.Integer({
					minimum: 1,
					description: "For conversations: at most this many.",
				})),
				after: Type.Optional(Type.String({
					description: "For conversations: continue after this cursor.",
				})),
				node: Type.Optional(Type.String({
					description: "Ask this peer instead of this node.",
				})),
			}),
			annotations: { readOnlyHint: true, openWorldHint: true },
			execute: async (_id, params, signal, _update, ctx) =>
				this.sessions(params as CommsSessionsParams, signal, ctx),
		});

		pi.registerTool({
			name: CommsSessionTools.REACH,
			label: "comms reach",
			description:
				"Reach one conversation over agent-comms, as `/resume` would: " +
				"`action: open` names it by `name` (a hosted session), `file` " +
				"(a conversation file on that node) or `sessionId`, and answers " +
				"whether it is now live or was hosted there for its directory. " +
				"`action: stop` ends one hosted session by `name`. `node` " +
				"reaches a peer; without it this node answers.",
			parameters: Type.Object({
				action: Type.Optional(Type.Union(
					[Type.Literal("open"), Type.Literal("stop")],
					{ description: "Open a conversation (default) or stop one." },
				)),
				name: Type.Optional(Type.String({
					description: "A hosted session's name.",
				})),
				file: Type.Optional(Type.String({
					description: "A conversation file on that node.",
				})),
				sessionId: Type.Optional(Type.String({
					description: "A conversation's id, as /resume shows it.",
				})),
				dir: Type.Optional(Type.String({
					description: "The directory to host it in.",
				})),
				node: Type.Optional(Type.String({
					description: "Reach this peer instead of this node.",
				})),
			}),
			annotations: { readOnlyHint: false, openWorldHint: true },
			execute: async (_id, params, signal) =>
				this.reach(params as CommsReachParams, signal),
		});

		pi.registerTool({
			name: CommsSessionTools.INPUT,
			label: "comms input",
			description:
				"Type into a session over agent-comms: the text is delivered to " +
				"that run's terminal as input, exactly as if a keyboard attached " +
				"there sent it. The session must be live on `node`, or here.",
			parameters: Type.Object({
				name: Type.String({ description: "The hosted session's name." }),
				text: Type.String({
					description: "The text to type; add \\r yourself to submit.",
				}),
				node: Type.Optional(Type.String({
					description: "Type into that peer's session.",
				})),
			}),
			annotations: { readOnlyHint: false, openWorldHint: true },
			execute: async (_id, params, signal) =>
				this.input(params as CommsInputParams, signal),
		});

		pi.registerTool({
			name: CommsSessionTools.WAIT,
			label: "comms session wait",
			description:
				"Wait on a hosted session over agent-comms, streaming what it " +
				"does - its prompts, replies, tool calls and results, read from " +
				"its conversation - until it settles (its turn ends: idle or " +
				"waiting), its run ends, or `timeoutMs` passes (the service's " +
				"own wait limit when omitted). Without a `cursor` the wait " +
				"starts now; pass the cursor `comms_input` returned to see the " +
				"turn that input starts, or a previous wait's to continue. A " +
				"caller abort or a queued message ends it early with what it " +
				"saw and the cursor.",
			parameters: Type.Object({
				name: Type.String({ description: "The hosted session's name." }),
				cursor: Type.Optional(Type.Unknown({
					description: "The cursor `comms_input` or an earlier wait " +
						"returned; omit to start now.",
				})),
				timeoutMs: Type.Optional(Type.Integer({
					minimum: 0,
					description: "Bound the whole wait; the service's own wait " +
						"limit when omitted.",
				})),
				node: Type.Optional(Type.String({
					description: "Wait on that peer's session.",
				})),
			}),
			annotations: { readOnlyHint: true, openWorldHint: true },
			execute: async (_id, params, signal, update, ctx) =>
				this.wait(params as CommsSessionWaitParams, signal, update, ctx),
		});
	}

	private async sessions(
		params: CommsSessionsParams,
		signal: AbortSignal | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<SessionToolResult> {
		// A relative directory is the caller's, resolved before it is sent.
		const dir = params.dir && !params.node
			? resolve(ctx?.cwd ?? process.cwd(), params.dir)
			: params.dir;
		const outcome = await this.cli.run(
			CommsSessionTools.SESSIONS,
			CommsArgv.sessions({ ...params, dir }),
			{ signal });
		return CommsSessionTools.result(outcome);
	}

	private async reach(
		params: CommsReachParams,
		signal: AbortSignal | undefined,
	): Promise<SessionToolResult> {
		const action = params.action ?? "open";
		const named = [params.name, params.file, params.sessionId]
			.filter((value) => value !== undefined && value !== "");
		if (action === "stop") {
			if (!params.name) {
				return CommsSessionTools.refusal(
					"comms_reach action stop takes the hosted session's name.");
			}
		} else if (named.length !== 1) {
			return CommsSessionTools.refusal(
				"comms_reach takes exactly one of name, file or sessionId.");
		}
		const outcome = await this.cli.run(
			CommsSessionTools.REACH,
			CommsArgv.reach({ ...params, action }),
			{ signal });
		return CommsSessionTools.result(outcome);
	}

	private async input(
		params: CommsInputParams,
		signal: AbortSignal | undefined,
	): Promise<SessionToolResult> {
		const outcome = await this.cli.run(
			CommsSessionTools.INPUT, CommsArgv.input(params), { signal });
		return CommsSessionTools.result(outcome);
	}

	/** Hold on one session until it settles, streaming what it does. */
	private async wait(
		params: CommsSessionWaitParams,
		signal: AbortSignal | undefined,
		onUpdate: ToolUpdate | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<SessionToolResult> {
		// A steer already queued ends the wait now: the message it carries
		// is the answer, and a later wait continues from the cursor.
		if (ctx?.hasPendingMessages?.()) {
			return CommsSessionTools.stopped("yield");
		}
		if (signal?.aborted) return CommsSessionTools.stopped("abort");
		const controller = new AbortController();
		const onCallerAbort = () => controller.abort();
		signal?.addEventListener("abort", onCallerAbort, { once: true });
		const startedAt = performance.now();
		const feed = new SessionWaitFeed(params.name, (text) => {
			try {
				onUpdate?.({
					content: [{ type: "text", text }],
					details: {
						waitedMs: Math.round(performance.now() - startedAt),
					},
				});
			} catch {
				// The host callback must never break the wait.
			}
		}, startedAt);
		let yielded = false;
		const tick = setInterval(() => {
			if (ctx?.hasPendingMessages?.()) {
				yielded = true;
				controller.abort();
				return;
			}
			feed.publish();
		}, this.options.waitUpdateMs ?? CommsSessionTools.UPDATE_MS);
		// Every call holds, so every call is a wait.
		this.options.setWaitState?.(true);
		let end: WaitEnd = { status: "stopped" };
		try {
			end = await this.follow(params, feed, controller.signal, startedAt);
		} finally {
			clearInterval(tick);
			feed.close();
			signal?.removeEventListener("abort", onCallerAbort);
			this.options.setWaitState?.(false);
		}
		if (yielded) return CommsSessionTools.stopped("yield", feed, end.cursor);
		if (signal?.aborted) {
			return CommsSessionTools.stopped("abort", feed, end.cursor);
		}
		if (end.failed) {
			return CommsSessionTools.failure(end.failed, feed, end.cursor);
		}
		return {
			content: [{
				type: "text",
				text: feed.result(end.status, end.cursor,
					CommsSessionTools.MAX_TEXT),
			}],
			structuredContent: {
				status: end.status, name: params.name,
				nodeId: end.nodeId, state: feed.state, cursor: end.cursor,
			},
			details: {
				status: end.status,
				state: feed.state,
				cursor: end.cursor,
				waitedMs: Math.round(performance.now() - startedAt),
			},
		};
	}

	/** Page after page until the session settles, ends, or the bound passes:
	 *  each page is one held request, answered as soon as there is news. */
	private async follow(
		params: CommsSessionWaitParams,
		feed: SessionWaitFeed,
		signal: AbortSignal,
		startedAt: number,
	): Promise<WaitEnd> {
		let cursor = params.cursor;
		let nodeId: string | undefined;
		let deadline = params.timeoutMs === undefined
			? undefined : startedAt + params.timeoutMs;
		for (;;) {
			const timeoutMs = deadline === undefined ? undefined
				: Math.max(0, Math.round(deadline - performance.now()));
			const outcome = await this.cli.run(CommsSessionTools.WAIT,
				CommsArgv.sessionWait({ ...params, cursor, timeoutMs }),
				{ signal });
			if (signal.aborted) return { status: "stopped", cursor, nodeId };
			const page = outcome.answer as SessionWaitPage | undefined;
			// A hold that found nothing is a page, not a failure.
			if (!page || (outcome.exitCode !== CommsExit.OK &&
					outcome.exitCode !== CommsExit.TIMED_OUT)) {
				return { status: "failed", cursor, nodeId, failed: outcome };
			}
			cursor = page.cursor;
			nodeId = page.nodeId;
			// The serving node's own limit bounds a wait that named none.
			deadline ??= startedAt + (page.maxWaitMs ?? 0);
			feed.add(page);
			feed.publish();
			if (page.status === "ended") {
				return { status: "ended", cursor, nodeId };
			}
			if (!page.more && feed.settled()) {
				return { status: "settled", cursor, nodeId };
			}
			if (performance.now() >= deadline) {
				return { status: "timed-out", cursor, nodeId };
			}
		}
	}

	/** An early stop: nothing taken, nothing lost. */
	private static stopped(
		stoppedBy: "yield" | "abort",
		feed?: SessionWaitFeed,
		cursor?: unknown,
	): SessionToolResult {
		const head = stoppedBy === "yield"
			? "comms_session_wait yielded: a message is queued; read it now."
			: "comms_session_wait aborted: nothing was taken from the run.";
		return {
			content: [{
				type: "text",
				text: feed ? `${head}\n` +
					feed.result(stoppedBy, cursor, CommsSessionTools.MAX_TEXT)
					: head,
			}],
			structuredContent: { status: "yielded", stoppedBy, cursor },
			details: { stoppedBy, cursor },
		};
	}

	private static refusal(text: string): SessionToolResult {
		return {
			content: [{ type: "text", text }],
			isError: true,
			details: {},
		};
	}

	/** Render one outcome: the answer on success, the CLI's stderr and exit
	 *  code on failure, with the answer kept in details either way. */
	private static result(outcome: CommsOutcome): SessionToolResult {
		if (outcome.exitCode !== CommsExit.OK || outcome.answer === undefined) {
			return CommsSessionTools.failure(outcome);
		}
		return {
			content: [{
				type: "text",
				text: CommsSessionTools.answer(outcome.answer),
			}],
			structuredContent: outcome.answer,
			details: {
				command: outcome.command,
				argv: outcome.argv,
				exitCode: outcome.exitCode,
			},
		};
	}

	private static answer(value: unknown): string {
		let text: string;
		try {
			text = JSON.stringify(value);
		} catch {
			text = String(value);
		}
		return text.length > CommsSessionTools.MAX_TEXT
			? `${text.slice(0, CommsSessionTools.MAX_TEXT)}\n… truncated`
			: text;
	}

	private static failure(
		outcome: CommsOutcome,
		feed?: SessionWaitFeed,
		cursor?: unknown,
	): SessionToolResult {
		const head = outcome.missingBinary
			? `pi-comms not found at ${outcome.missingBinary}`
			: `pi-comms ${outcome.command} failed: exit ${outcome.exitCode} ` +
				`(${outcome.killed ? "cancelled" : CommsExit.label(outcome.exitCode)})`;
		const lines = [head];
		if (outcome.stderr.trim()) lines.push(outcome.stderr.trim());
		if (outcome.answer !== undefined) {
			lines.push(CommsSessionTools.answer(outcome.answer));
		}
		if (feed) {
			lines.push(feed.result("failed", cursor,
				CommsSessionTools.MAX_TEXT));
		}
		return {
			content: [{ type: "text", text: lines.join("\n") }],
			isError: true,
			details: {
				command: outcome.command,
				argv: outcome.argv,
				exitCode: outcome.exitCode,
				answer: outcome.answer,
				cursor,
			},
		};
	}
}
