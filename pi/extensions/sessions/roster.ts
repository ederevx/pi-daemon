/**
 * `sessions` - the daemon's roster of the sessions it hosts on this node.
 *
 * One read over the sessions domain: the name, state, directory, terminal
 * attachment and conversation file of every live record, and with `all` the
 * records whose run ended, which are the names `pi-rc attach` can still
 * revive. Read-only: the tool issues `list` and renders the reply, so a
 * missing daemon degrades to one line instead of breaking the turn.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { ControlRequester } from "../control/client.ts";

/** One record as the sessions domain publishes it. */
interface SessionView {
	name?: string;
	state?: string;
	dir?: string;
	attached?: boolean;
	clients?: number;
	file?: string | null;
	phase?: string;
	fileExists?: boolean;
}

/** Owns the `sessions` tool: request shaping and rendering. */
export class SessionRoster {
	private readonly client: ControlRequester;

	constructor(client: ControlRequester) {
		this.client = client;
	}

	/** Register the tool with pi. */
	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: "sessions",
			label: "sessions",
			description:
				"List the sessions the pi-daemon hosts on this node: name, " +
				"state, directory, whether a terminal is attached, and the " +
				"conversation file. `all` adds the records whose run ended - " +
				"the names `pi-rc attach <name>` can still revive - and says " +
				"whether each one's conversation is on disk.",
			parameters: Type.Object({
				all: Type.Optional(Type.Boolean({
					description: "Include records whose run ended (revivable names).",
				})),
			}),
			annotations: { readOnlyHint: true },
			execute: async (_id, params) =>
				this.run(params as { all?: boolean }),
		});
	}

	/** Issue the list and render it. */
	private async run(
		params: { all?: boolean },
	): Promise<{
		content: Array<{ type: "text"; text: string }>;
		details: unknown;
		isError: boolean;
	}> {
		const includeStopped = params.all === true;
		const response = await this.client.request({
			cmd: "list",
			all: includeStopped,
		});
		if (response.ok !== true) {
			const error = typeof response.error === "string" ? response.error : "failed";
			const detail = typeof response.message === "string" ? ` (${response.message})` : "";
			return this.message(`sessions: ${error}${detail}`, response, []);
		}
		const sessions = Array.isArray(response.sessions)
			? (response.sessions as SessionView[])
			: [];
		const lines = sessions.map((view) => this.line(view, includeStopped));
		const header = includeStopped
			? `${sessions.length} record${sessions.length === 1 ? "" : "s"}:`
			: `${sessions.length} live session${sessions.length === 1 ? "" : "s"}:`;
		const stopped = typeof response.stopped === "number" ? response.stopped : 0;
		const footer = !includeStopped && stopped > 0
			? `\n${stopped} stopped record${stopped === 1 ? "" : "s"} not shown; pass all: true to count them in.`
			: "";
		return this.message([header, ...lines].join("\n") + footer, response, sessions);
	}

	/** One record as a line: name, state, the phase a stopped record ended
	 *  in, attachment, directory, conversation file. */
	private line(view: SessionView, includeStopped: boolean): string {
		const name = view.name ?? "(unnamed)";
		const state = view.state ?? "unknown";
		const phase = includeStopped && view.phase ? ` phase=${view.phase}` : "";
		const attached = view.attached === true
			? `attached${view.clients && view.clients > 1 ? ` x${view.clients}` : ""}`
			: "detached";
		const dir = view.dir ? ` ${view.dir}` : "";
		const file = view.file
			? ` file=${view.file}${view.fileExists === false ? " (missing)" : ""}`
			: " file=none";
		return `- ${name} ${state}${phase} ${attached}${dir}${file}`;
	}

	/** Build the tool result, keeping `details` small. A failed request is a
	 *  tool error, and still one readable line. */
	private message(
		text: string,
		response: Record<string, unknown>,
		sessions: SessionView[],
	): {
		content: Array<{ type: "text"; text: string }>;
		details: unknown;
		isError: boolean;
	} {
		return {
			content: [{ type: "text", text }],
			details: {
				ok: response.ok === true,
				error: response.error,
				count: sessions.length,
				names: sessions.map((view) => view.name ?? ""),
			},
			isError: response.ok !== true,
		};
	}
}
