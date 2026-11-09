/**
 * `session_stop` - end one hosted session's run on this node.
 *
 * The sessions domain owns stopping a run; the tool only names the target
 * and reports the projected record, so nothing here reaches a process
 * directly. A stopped record stays attachable, but only a recorded
 * conversation on disk can be resumed, so the reply points at `sessions`
 * rather than promising one. The session the tool itself runs in is
 * refused - a stop request from inside would end the turn that asked for
 * it.
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
				"End one hosted session's run by name on this node. The record " +
				"is kept; `sessions` with all: true shows whether its conversation " +
				"file is still on disk, which is what `pi-rc attach <name>` needs to " +
				"resume it. The session this tool runs in is refused, and `sessions` " +
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
		if (this.own !== "" && this.reachesOwn(name)) {
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
		const target = typeof response.name === "string" ? response.name : name;
		const state = typeof response.state === "string" ? response.state : "unknown";
		const ended = response.alive !== true;
		const text = ended
			? `session_stop ${target}: run ended (state=${state}); \`sessions\` with all: true shows whether its conversation file can still be resumed, and \`pi-rc attach ${target}\` is refused when it cannot.`
			: `session_stop ${target}: stop requested (state=${state}, alive) - the run ends when the child exits.`;
		return {
			content: [{ type: "text", text }],
			details: { ok: true, name: target, ended, response },
		};
	}

	/** Whether the name reaches this session once the store's one-step
	 *  `pi-` alias is applied. The tool cannot ask the daemon which record a
	 *  spelling resolves to before sending the stop, so a distinct session
	 *  named next to this one's alias is refused too. */
	private reachesOwn(name: string): boolean {
		const alternate = name.startsWith("pi-") ? name.slice(3) : `pi-${name}`;
		return name === this.own || alternate === this.own;
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
