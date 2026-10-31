/**
 * pi-daemon — the `tool_completion` resolver.
 *
 * The continuation a consumer calls when a tool answered with a pending
 * completion: it blocks until the daemon-owned operation is terminal and
 * returns that operation's real output and status. It is registered with
 * `codemode` exposure, so it is never declared to the model - only a
 * resolver holding an acknowledgment calls it.
 *
 * Reading the output here does not consume it: the session's background
 * watcher still delivers its own copy when the operation finishes, so both
 * consumers observe the same completion and only the caller answers.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	DEFAULT_MAX_BYTES,
	DEFAULT_MAX_LINES,
	truncateTail,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { DaemonTasks, Ticket } from "../offload.ts";
import { COMPLETION_VERSION, CompletionMetadata } from "./protocol.ts";

/** The resolver's parameters: the version and the producer's opaque payload. */
interface CompletionParams {
	version: number;
	payload: unknown;
}

export class ToolCompletionTool {
	static readonly NAME = "tool_completion";

	private readonly tickets: Pick<DaemonTasks, "client">;
	private readonly waitSeconds: number;

	constructor(tickets: Pick<DaemonTasks, "client">, waitSeconds: number) {
		this.tickets = tickets;
		this.waitSeconds = waitSeconds;
	}

	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: ToolCompletionTool.NAME,
			label: "tool completion",
			description:
				"Resolve a tool result that was an acknowledgment: waits for the " +
				"daemon-owned operation the producer started and returns its real " +
				"output and status. Called by tools that resolve deferred results; " +
				"it is not a model-facing tool.",
			parameters: Type.Object({
				version: Type.Number({
					description: "Completion schema version the caller understands",
				}),
				payload: Type.Unknown({
					description: "The producer's opaque pending-completion payload",
				}),
			}),
			// Callable through ctx.executeTool, never declared to the model.
			exposure: "codemode",
			annotations: { readOnlyHint: true, openWorldHint: false },
			execute: async (_toolCallId: string, params: CompletionParams, signal?: AbortSignal) =>
				this.execute(params, signal),
		});
	}

	/** Block for the operation, then report its output and its own status. */
	async execute(
		params: CompletionParams,
		signal?: AbortSignal,
	): Promise<{
		content: Array<{ type: "text"; text: string }>;
		details: { ticket: Ticket; toolCompletion: ReturnType<typeof CompletionMetadata.final> };
		isError?: boolean;
	}> {
		if (params?.version !== COMPLETION_VERSION) {
			throw new Error(
				`tool_completion: unsupported completion version ${String(params?.version)}; ` +
					`this resolver speaks ${COMPLETION_VERSION}.`,
			);
		}
		const id = ToolCompletionTool.ticketOf(params.payload);
		if (id === undefined) {
			throw new Error("tool_completion: the payload names no operation to resolve.");
		}
		const ticket = await this.settled(id, signal);
		const output = ToolCompletionTool.bounded(
			await this.tickets.client.outputAll(id, signal),
		);
		const failed = ticket.status !== "done" || (ticket.exit ?? 0) !== 0;
		const text = `${output}${failed ? ToolCompletionTool.failureNote(ticket) : ""}`;
		return {
			content: [{ type: "text", text }],
			details: { ticket, toolCompletion: CompletionMetadata.final() },
			isError: failed ? true : undefined,
		};
	}

	/** Wait in bounded chunks until the ticket leaves `running`; a wait that
	 *  outlives its chunk simply asks again, so no chunk is a deadline. */
	private async settled(id: string, signal?: AbortSignal): Promise<Ticket> {
		let ticket = await this.tickets.client.wait(id, this.waitSeconds, signal);
		while (ticket.status === "running") {
			signal?.throwIfAborted();
			ticket = await this.tickets.client.wait(id, this.waitSeconds, signal);
		}
		return ticket;
	}

	/** The producer's payload shape: an operation id it owns. */
	private static ticketOf(payload: unknown): string | undefined {
		if (payload === null || typeof payload !== "object") return undefined;
		const id = (payload as { ticket?: unknown }).ticket;
		return typeof id === "string" && id !== "" ? id : undefined;
	}

	/** Bound the output exactly as the daemon's own result view does. */
	private static bounded(output: string): string {
		const truncation = truncateTail(output, {
			maxLines: DEFAULT_MAX_LINES,
			maxBytes: DEFAULT_MAX_BYTES,
		});
		let text = truncation.content || "(no output)";
		if (truncation.truncated) {
			text += `\n[Output truncated: ${truncation.outputLines} of ${truncation.totalLines} lines kept]`;
		}
		return text;
	}

	/** One line naming why the operation did not succeed cleanly. */
	private static failureNote(ticket: Ticket): string {
		const parts = [ticket.status];
		if (ticket.exit !== null) parts.push(`exit ${ticket.exit}`);
		if (ticket.error) parts.push(ticket.error);
		return `\n[${parts.join(" ")}]`;
	}
}
