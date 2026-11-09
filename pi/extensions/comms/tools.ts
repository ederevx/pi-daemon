/**
 * pi-daemon — the agent-comms tool set.
 *
 * Six model-facing tools over the `agent-comms` service: send, wait,
 * replay, acknowledge, spawn and peer list. Each one shapes the argv for
 * `pi/bin/pi-comms`, runs it through `CommsCli`, and returns the
 * service's JSON answer as the structured result. A non-zero exit is a
 * tool error carrying the CLI's stderr and exit code; an empty wait
 * (exit 3) is a page with no mail, not a failure.
 */

import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import type { ExtensionAPI, ExtensionToolContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { CommsCli, type CommsOutcome } from "./client.ts";
import {
	CommsArgv,
	CommsExit,
	type CommsAckParams,
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

	constructor(private readonly cli: CommsCli) {}

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
				requestId: Type.Optional(Type.String({
					description: "Reuse this id to retry one send safely.",
				})),
			}),
			annotations: { readOnlyHint: false, openWorldHint: false },
			execute: async (_toolCallId, params: CommsSendParams, signal) =>
				this.send(params, signal),
		});

		pi.registerTool({
			name: CommsTools.WAIT,
			label: "comms wait",
			description:
				"Hold until agent-comms mail arrives, bounded by the service's " +
				"own wait timeout, and return the page and its cursor. An empty " +
				"wait is a page with no messages, not an error.",
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
			}),
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (_toolCallId, params: CommsWaitParams, signal) =>
				this.wait(params, signal),
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
			}),
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (_toolCallId, params: CommsReplayParams, signal) =>
				this.replay(params, signal),
		});

		pi.registerTool({
			name: CommsTools.ACK,
			label: "comms ack",
			description:
				"Acknowledge delivered agent-comms messages by message id.",
			parameters: Type.Object({
				messageIds: Type.Array(Type.String(), {
					minItems: 1,
					description: "The message ids to acknowledge.",
				}),
			}),
			annotations: { readOnlyHint: false, openWorldHint: false },
			execute: async (_toolCallId, params: CommsAckParams, signal) =>
				this.ack(params, signal),
		});

		pi.registerTool({
			name: CommsTools.SPAWN,
			label: "comms spawn",
			description:
				"Launch another agent through the pi-daemon's comms launcher: " +
				"a directory and the argv to run there, optionally under an " +
				"explicit agent id. Use it to run the independently bounded " +
				"units of one task in parallel sessions, coordinated by " +
				"comms_send, comms_wait and comms_replay. It is the stand-in " +
				"for the in-session child-agent tool, and the more independent " +
				"one: a spawned session keeps its own conversation and can be " +
				"reached again after it answers.",
			parameters: Type.Object({
				agentId: Type.Optional(Type.String({
					description: "The identity the launched agent binds.",
				})),
				dir: Type.String({ description: "The working directory." }),
				argv: Type.Array(Type.String(), {
					minItems: 1,
					description: "The command and its arguments.",
				}),
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
			parameters: Type.Object({}),
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (_toolCallId, _params, signal) => this.peers(signal),
		});
	}

	private async send(
		params: CommsSendParams,
		signal?: AbortSignal,
	): Promise<CommsToolResult> {
		const requestId = params.requestId || randomUUID();
		const argv = CommsArgv.send({ ...params, requestId });
		const outcome = await this.cli.run(CommsTools.SEND, argv, { signal });
		return this.result(outcome, { requestId });
	}

	private async wait(
		params: CommsWaitParams,
		signal?: AbortSignal,
	): Promise<CommsToolResult> {
		const outcome = await this.cli.run(
			CommsTools.WAIT, CommsArgv.wait(params), { signal });
		const empty = outcome.exitCode === CommsExit.TIMED_OUT
			&& outcome.answer !== undefined;
		return this.result(outcome, { timedOut: empty }, empty);
	}

	private async replay(
		params: CommsReplayParams,
		signal?: AbortSignal,
	): Promise<CommsToolResult> {
		const outcome = await this.cli.run(
			CommsTools.REPLAY, CommsArgv.replay(params), { signal });
		return this.result(outcome, {});
	}

	private async ack(
		params: CommsAckParams,
		signal?: AbortSignal,
	): Promise<CommsToolResult> {
		const outcome = await this.cli.run(
			CommsTools.ACK, CommsArgv.ack(params), { signal });
		return this.result(outcome, {});
	}

	private async spawn(
		params: CommsSpawnParams,
		signal: AbortSignal | undefined,
		ctx: ExtensionToolContext | undefined,
	): Promise<CommsToolResult> {
		const dir = resolve(ctx?.cwd ?? process.cwd(), params.dir);
		const outcome = await this.cli.run(
			CommsTools.SPAWN, CommsArgv.spawn({ ...params, dir }), { signal });
		return this.result(outcome, {});
	}

	private async peers(signal?: AbortSignal): Promise<CommsToolResult> {
		const outcome = await this.cli.run(
			CommsTools.PEERS, CommsArgv.peers(), { signal });
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
