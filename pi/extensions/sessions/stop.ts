/**
 * `session_stop` - end one hosted session's run on this node.
 *
 * The sessions domain owns stopping a run; the tool only names the target
 * and reports the projected record, so nothing here reaches a process
 * directly. The conversation survives: a stopped record is resumed with
 * `pi-rc attach <name>`, which is why the reply says so. The session the
 * tool itself runs in is refused - a stop request from inside would end
 * the turn that asked for it.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { ControlRequester } from "../control/client.ts";

/** Owns the `session_stop` tool: target validation and rendering. */
export class SessionStop {
	private readonly client: ControlRequester;
	private readonly own: string;

	constructor(client: ControlRequester, own: string = process.env.PI_HOSTED_SESSION ?? "") {
		this.client = client;
		this.own = own;
	}

	/** Register the tool with pi. */
	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: "session_stop",
			label: "session stop",
			description:
				"End one hosted session's run by name on this node. The " +
				"conversation is kept and stays resumable with `pi-rc attach " +
				"<name>`; the session this tool runs in is refused, and `sessions` " +
				"lists the names.",
			parameters: Type.Object({
				name: Type.String({
					description: "The hosted session name from `sessions`.",
				}),
			}),
			execute: async (_id, params) =>
				this.run(params as { name?: string }),
		});
	}

	/** Stop the named session and render the reply. */
	private async run(
		params: { name?: string },
	): Promise<{
		content: Array<{ type: "text"; text: string }>;
		details: unknown;
		isError?: boolean;
	}> {
		const name = (params.name ?? "").trim();
		if (!name) {
			return this.refused("A session name is required.", "name-required");
		}
		if (this.own && (name === this.own || `pi-${name}` === this.own)) {
			return this.refused(
				`${name} is this session; a stop request from inside would end the turn that asked for it.`,
				"own-session",
			);
		}
		const response = await this.client.request({ cmd: "stop", name });
		if (response.ok !== true) {
			const error = typeof response.error === "string" ? response.error : "failed";
			const detail = typeof response.message === "string" ? ` (${response.message})` : "";
			return this.refused(`session_stop ${name}: ${error}${detail}`, error, response);
		}
		return {
			content: [{
				type: "text",
				text: `session_stop ${name}: run ended; the conversation stays resumable with \`pi-rc attach ${name}\`.`,
			}],
			details: { ok: true, name, response },
		};
	}

	/** A refusal that reads as a tool error, not as a completed stop. */
	private refused(
		text: string,
		code: string,
		response: Record<string, unknown> = {},
	): {
		content: Array<{ type: "text"; text: string }>;
		details: unknown;
		isError: true;
	} {
		return {
			content: [{ type: "text", text }],
			details: { ok: false, error: code, response },
			isError: true,
		};
	}
}
