/**
 * pi-daemon — the session-reach tools over agent-comms.
 *
 * `comms_sessions` lists what a node hosts and which conversations
 * `/resume` would offer there; `comms_reach` opens one — adopting the live
 * run, or hosting the conversation on that node — and ends one;
 * `comms_input` types into a hosted run; `comms_feed` reads one page of
 * what a run is doing and printing, or holds for the next page.
 *
 * A feed is one of pi's blocking waits: it publishes the waiting state,
 * streams elapsed time through the tool's update channel, and yields when
 * a caller abort or a queued message arrives. It is read-only, so it never
 * takes the mailbox lease the receiver owns.
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
	type CommsFeedParams,
	type CommsInputParams,
	type CommsReachParams,
	type CommsSessionsParams,
} from "./protocol.ts";

/** One tool's answer, already rendered for pi. */
interface SessionToolResult {
	content: Array<{ type: "text"; text: string }>;
	structuredContent?: unknown;
	details: Record<string, unknown>;
	isError?: boolean;
}

/** The feed's streamed tool-window update. */
type ToolUpdate = (update: {
	content: Array<{ type: "text"; text: string }>;
	details: Record<string, unknown>;
}) => void;

/** What the composition may wire in; all of it is optional. */
export interface CommsSessionOptions {
	/** Publishes "waiting" for a held feed; the tool restores on exit. */
	setWaitState?: (waiting: boolean) => void;
	waitUpdateMs?: number;
}

/** One event as a feed page carries it. */
interface FeedEvent {
	type?: string;
	text?: string;
	kind?: string;
	entryId?: string;
	state?: string;
	reason?: string;
	skipped?: number;
	elided?: boolean;
}

/** One feed page. */
interface FeedPage {
	status?: string;
	name?: string;
	nodeId?: string;
	state?: string;
	events?: FeedEvent[];
	cursor?: unknown;
	more?: boolean;
}

export class CommsSessionTools {
	static readonly SESSIONS = "comms_sessions";
	static readonly REACH = "comms_reach";
	static readonly INPUT = "comms_input";
	static readonly FEED = "comms_feed";

	/** The feed's live-update cadence, presentational only. */
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
			name: CommsSessionTools.FEED,
			label: "comms feed",
			description:
				"Read what a session is doing and printing, over agent-comms: " +
				"one page of output, activity and state, or - with a `cursor` " +
				"from the previous page - a hold until the next page is ready. " +
				"Watching never counts as a viewer, so it never changes how the " +
				"run renders. `include` picks output, activity or state; " +
				"`timeoutMs` bounds a hold.",
			parameters: Type.Object({
				name: Type.String({ description: "The hosted session's name." }),
				include: Type.Optional(Type.Array(
					Type.Union([
						Type.Literal("output"),
						Type.Literal("activity"),
						Type.Literal("state"),
					]),
					{
						minItems: 1,
						description: "What to read: output, activity, state.",
					},
				)),
				cursor: Type.Optional(Type.Unknown({
					description: "The previous page's cursor; omit for the tail.",
				})),
				timeoutMs: Type.Optional(Type.Integer({
					minimum: 0,
					description: "Bound the hold, in milliseconds.",
				})),
				maxBytes: Type.Optional(Type.Integer({
					minimum: 1,
					description: "Bound the page, in bytes (at least 256).",
				})),
				node: Type.Optional(Type.String({
					description: "Watch that peer's session.",
				})),
			}),
			annotations: { readOnlyHint: true, openWorldHint: true },
			execute: async (_id, params, signal, update, ctx) =>
				this.feed(params as CommsFeedParams, signal, update, ctx),
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

	/** One page, or a hold for the next one, streamed while it waits. */
	private async feed(
		params: CommsFeedParams,
		signal: AbortSignal | undefined,
		onUpdate: ToolUpdate | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<SessionToolResult> {
		// A steer already queued ends the hold now: the message it carries
		// is the answer, and the page can be read after it.
		if (ctx?.hasPendingMessages?.()) {
			return CommsSessionTools.stopped("yield");
		}
		if (signal?.aborted) return CommsSessionTools.stopped("abort");
		const controller = new AbortController();
		const onCallerAbort = () => controller.abort();
		signal?.addEventListener("abort", onCallerAbort, { once: true });
		const updateMs = this.options.waitUpdateMs
			?? CommsSessionTools.UPDATE_MS;
		const startedAt = performance.now();
		let yielded = false;
		const tick = setInterval(() => {
			if (ctx?.hasPendingMessages?.()) {
				yielded = true;
				controller.abort();
				return;
			}
			onUpdate?.({
				content: [{
					type: "text",
					text: `Watching ${params.name} ` +
						`(${Math.round((performance.now() - startedAt) / 1000)}s)`,
				}],
				details: {
					waitedMs: Math.round(performance.now() - startedAt),
				},
			});
		}, updateMs);
		let outcome: CommsOutcome;
		// Only a hold is a wait: a first page answers at once, so the
		// session's published state never flickers for it.
		const holds = params.cursor !== undefined;
		if (holds) this.options.setWaitState?.(true);
		try {
			outcome = await this.cli.run(
				CommsSessionTools.FEED, CommsArgv.feed(params),
				{ signal: controller.signal });
		} finally {
			clearInterval(tick);
			signal?.removeEventListener("abort", onCallerAbort);
			if (holds) this.options.setWaitState?.(false);
		}
		if (yielded) return CommsSessionTools.stopped("yield");
		if (signal?.aborted) return CommsSessionTools.stopped("abort");
		const page = outcome.answer as FeedPage | undefined;
		// A hold that found nothing is an empty page, not a failure: it
		// carries the cursor that continues from here.
		const empty = outcome.exitCode === CommsExit.TIMED_OUT;
		if (!page || (outcome.exitCode !== CommsExit.OK && !empty)) {
			return CommsSessionTools.failure(outcome);
		}
		return {
			content: [{
				type: "text",
				text: CommsSessionTools.pageText(page, params.name),
			}],
			structuredContent: page,
			details: {
				command: outcome.command,
				exitCode: outcome.exitCode,
				status: page.status,
				more: page.more === true,
				cursor: page.cursor,
				waitedMs: Math.round(performance.now() - startedAt),
			},
		};
	}

	/** A feed page as model text: the output as it printed, then one line
	 *  per activity event, and the cursor that continues from here. */
	private static pageText(page: FeedPage, name: string): string {
		const lines: string[] = [];
		for (const event of page.events ?? []) {
			switch (event.type) {
				case "output":
					if (event.text) lines.push(event.text);
					break;
				case "activity":
					lines.push(`· ${event.kind ?? "activity"}` +
						(event.entryId ? ` ${event.entryId}` : "") +
						(event.elided ? " (elided: too large for a page)" : ""));
					break;
				case "state":
					lines.push(`state: ${event.state ?? "unknown"}`);
					break;
				case "reset":
					lines.push("(the run restarted; output is read from its start)");
					break;
				case "ended":
					lines.push(`ended: ${event.reason ?? "gone"}`);
					break;
				default:
					break;
			}
		}
		if (!lines.length) {
			lines.push(page.status === "timed-out"
				? `${name}: no new output or activity`
				: `${name}: ${page.status ?? "ok"}`);
		}
		lines.push(`[${page.status ?? "ok"}` +
			`${page.more ? ", more" : ""}] ` +
			`cursor: ${JSON.stringify(page.cursor ?? null)}`);
		const text = lines.join("\n");
		return text.length > CommsSessionTools.MAX_TEXT
			? `${text.slice(0, CommsSessionTools.MAX_TEXT)}\n… truncated`
			: text;
	}

	/** An early stop: nothing taken, nothing lost. */
	private static stopped(stoppedBy: "yield" | "abort"): SessionToolResult {
		return {
			content: [{
				type: "text",
				text: stoppedBy === "yield"
					? "comms_feed yielded: a message was already queued; read it now."
					: "comms_feed aborted: nothing was taken from the run.",
			}],
			structuredContent: { status: "yielded", stoppedBy },
			details: { stoppedBy },
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

	private static failure(outcome: CommsOutcome): SessionToolResult {
		const head = outcome.missingBinary
			? `pi-comms not found at ${outcome.missingBinary}`
			: `pi-comms ${outcome.command} failed: exit ${outcome.exitCode} ` +
				`(${outcome.killed ? "cancelled" : CommsExit.label(outcome.exitCode)})`;
		const lines = [head];
		if (outcome.stderr.trim()) lines.push(outcome.stderr.trim());
		if (outcome.answer !== undefined) {
			lines.push(CommsSessionTools.answer(outcome.answer));
		}
		return {
			content: [{ type: "text", text: lines.join("\n") }],
			isError: true,
			details: {
				command: outcome.command,
				argv: outcome.argv,
				exitCode: outcome.exitCode,
				answer: outcome.answer,
			},
		};
	}
}
