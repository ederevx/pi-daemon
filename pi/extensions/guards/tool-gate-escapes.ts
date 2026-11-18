/**
 * pi-daemon — the declared escape a prerequisite gate admits.
 *
 * A registration declares `annotations.piGateEscape = { version, admits }`,
 * naming the tools that stay reachable while its extension's own
 * prerequisite is unmet. A gate reads the union of the live declarations,
 * so every gate admits the same set. Only own properties count; an unknown
 * version, a malformed declaration and an unreadable registry admit
 * nothing.
 */

import type { ExtensionAPI, ToolInfo } from "@earendil-works/pi-coding-agent";

/** Reads the escape declarations registered in this session. */
export class ToolGateEscapes {
	/** The annotation key a registration declares its admissions under. */
	static readonly KEY = "piGateEscape";
	/** The declaration version this reader understands. */
	static readonly VERSION = 1;

	/** Whether a live registration admits this tool while a gate is
	 *  closed. Any failure reads as "no declaration". */
	static admits(
		pi: Pick<ExtensionAPI, "getAllTools"> | undefined,
		name: string,
	): boolean {
		if (!name) return false;
		let tools: ToolInfo[];
		try {
			const read = pi?.getAllTools;
			if (typeof read !== "function") return false;
			tools = read.call(pi) as ToolInfo[];
		} catch {
			return false;
		}
		if (!Array.isArray(tools)) return false;
		return tools.some((tool) => ToolGateEscapes.declares(tool, name));
	}

	/** True when one registration's declaration names this tool. */
	private static declares(tool: ToolInfo | undefined, name: string): boolean {
		const annotations = tool?.annotations as
			| Record<string, unknown>
			| undefined;
		if (!annotations || !Object.hasOwn(annotations, ToolGateEscapes.KEY)) {
			return false;
		}
		const declaration = annotations[ToolGateEscapes.KEY];
		if (!declaration || typeof declaration !== "object"
			|| Array.isArray(declaration)) return false;
		const record = declaration as Record<string, unknown>;
		if (!Object.hasOwn(record, "version")
			|| record.version !== ToolGateEscapes.VERSION) return false;
		if (!Object.hasOwn(record, "admits")) return false;
		const admits = record.admits;
		if (!Array.isArray(admits)) return false;
		return admits.some((item) => typeof item === "string" && item === name);
	}
}
