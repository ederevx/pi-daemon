/**
 * `transcript_read` - read the active conversation from the daemon's
 * positional transcript index instead of loading the whole JSONL file.
 *
 * One tool, dispatched by `view`: `stat` (metadata and index state),
 * `path` (leaf-to-root chain), `entries` (paged by cursor or ids),
 * `tree` (every node), and `range` (entry-aligned raw bytes). The file
 * defaults to the current session and is always validated by the daemon
 * against the session store, so the tool can never read an arbitrary
 * path. Read-only: it appends nothing and changes no session state.
 */

import type { ExtensionAPI, ExtensionToolContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { ControlRequester } from "./client.ts";

/** The five read views and their knobs. */
export interface TranscriptParams {
	view?: "stat" | "path" | "entries" | "tree" | "range";
	file?: string;
	session?: string;
	leafId?: string;
	since?: string;
	ids?: string[];
	offset?: number;
	limit?: number;
	length?: number;
	align?: "none" | "entry";
	fields?: "ref" | "full";
}

/** Owns the `transcript_read` tool: request shaping, dispatch, and
 *  rendering. The transport is injected so it can be tested without a
 *  daemon. */
export class TranscriptTool {
	private readonly client: ControlRequester;
	private readonly rangeDefault = 16384;
	private readonly maxChars = 60000;

	constructor(client: ControlRequester) {
		this.client = client;
	}

	/** Register the tool with pi. */
	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: "transcript_read",
			label: "transcript_read",
			description:
				"Read this conversation from the pi-daemon transcript index " +
				"without loading the whole session file. `view`: stat " +
				"(metadata and index state), path (leaf-to-root chain), " +
				"entries (paged by `since` cursor or `ids`), tree (all " +
				"nodes), range (entry-aligned raw bytes by `offset`/" +
				"`length`). Defaults to the current session; `file` may name " +
				"another conversation inside the store.",
			parameters: Type.Object({
				view: Type.Optional(Type.Union([
					Type.Literal("stat"),
					Type.Literal("path"),
					Type.Literal("entries"),
					Type.Literal("tree"),
					Type.Literal("range"),
				])),
				file: Type.Optional(Type.String({
					description: "Session JSONL inside the store; defaults to this session.",
				})),
				session: Type.Optional(Type.String({
					description: "Hosted session name to read instead of a file.",
				})),
				leafId: Type.Optional(Type.String({
					description: "path: start the chain at this entry id.",
				})),
				since: Type.Optional(Type.String({
					description: "entries: return entries after this id.",
				})),
				ids: Type.Optional(Type.Array(Type.String(), {
					description: "entries: return exactly these entry ids.",
				})),
				offset: Type.Optional(Type.Number({
					description: "range: byte offset in the file.",
				})),
				length: Type.Optional(Type.Number({
					description: "range: byte count (default 16384).",
				})),
				limit: Type.Optional(Type.Number({
					description: "entries: page size (default 200).",
				})),
				align: Type.Optional(Type.Union([
					Type.Literal("none"),
					Type.Literal("entry"),
				], { description: "range: snap to whole JSONL records." })),
				fields: Type.Optional(Type.Union([
					Type.Literal("ref"),
					Type.Literal("full"),
				], { description: "path/entries: detail level (default ref)." })),
			}),
			annotations: { readOnlyHint: true },
			execute: async (_id, params, _signal, _onUpdate, ctx) =>
				this.run(params as TranscriptParams, ctx),
		});
	}

	/** Resolve the target and issue the view's wire command. */
	private async run(
		params: TranscriptParams,
		ctx?: ExtensionToolContext,
	): Promise<{ content: Array<{ type: "text"; text: string }>; details: unknown }> {
		const view = params.view ?? "stat";
		const target = this.target(params, ctx);
		if (target === null) {
			return this.message(
				view,
				"No session file: this session has no transcript and no " +
					"`file`/`session` target was given.",
				{ ok: false, error: "no-session" },
			);
		}
		const response = await this.client.request(this.command(view, params, target));
		return this.message(view, this.render(view, target, response), response);
	}

	/** The file or session to read: an explicit target, else the current
	 *  session file. Never both. */
	private target(
		params: TranscriptParams,
		ctx?: ExtensionToolContext,
	): Record<string, unknown> | null {
		if (params.file) return { file: params.file };
		if (params.session) return { session: params.session };
		const file = ctx?.sessionManager?.getSessionFile?.();
		return file ? { file } : null;
	}

	/** Build the control command for one view. */
	private command(
		view: TranscriptParams["view"],
		params: TranscriptParams,
		target: Record<string, unknown>,
	): Record<string, unknown> {
		const cmd: Record<string, unknown> = { ...target };
		switch (view) {
			case "path":
				cmd.cmd = "transcript-path";
				if (params.leafId) cmd.leafId = params.leafId;
				cmd.fields = params.fields ?? "ref";
				break;
			case "entries":
				cmd.cmd = "transcript-entries";
				if (params.since) cmd.since = params.since;
				if (params.ids) cmd.ids = params.ids;
				if (typeof params.limit === "number") cmd.limit = params.limit;
				cmd.fields = params.fields ?? "ref";
				break;
			case "tree":
				cmd.cmd = "transcript-tree";
				break;
			case "range":
				cmd.cmd = "transcript-range";
				cmd.offset = params.offset ?? 0;
				cmd.length = params.length ?? this.rangeDefault;
				cmd.align = params.align ?? "entry";
				break;
			default:
				cmd.cmd = "transcript-stat";
				break;
		}
		return cmd;
	}

	/** Render a reply as model-facing text. */
	private render(
		view: TranscriptParams["view"],
		target: Record<string, unknown>,
		response: Record<string, unknown>,
	): string {
		const label = this.label(view, target);
		if (response.ok !== true) {
			const error = typeof response.error === "string" ? response.error : "failed";
			const detail = typeof response.message === "string"
				? ` (${response.message})`
				: "";
			return `${label}: ${error}${detail}`;
		}
		if (view === "range") return this.decodeRange(label, response);
		const { ok: _ok, id: _id, ...payload } = response;
		return `${label}\n${this.truncate(JSON.stringify(payload, null, 2))}`;
	}

	/** Decode the base64 byte range into text. */
	private decodeRange(
		label: string,
		response: Record<string, unknown>,
	): string {
		const encoded = typeof response.bytes === "string" ? response.bytes : "";
		let text: string;
		try {
			text = Buffer.from(encoded, "base64").toString("utf8");
		} catch {
			return `${label}: undecodable range`;
		}
		const offset = typeof response.offset === "number" ? response.offset : 0;
		const length = typeof response.length === "number" ? response.length : 0;
		return `${label} [${offset}..${offset + length}]\n${this.truncate(text)}`;
	}

	/** A short, human-readable target label. */
	private label(
		view: TranscriptParams["view"],
		target: Record<string, unknown>,
	): string {
		const file = typeof target.file === "string" ? target.file : "";
		const session = typeof target.session === "string" ? target.session : "";
		const name = session || file.split(/[\\/]/).pop() || "transcript";
		return `transcript_read ${view} (${name})`;
	}

	/** Keep the model-facing text bounded. */
	private truncate(text: string): string {
		if (text.length <= this.maxChars) return text;
		return `${text.slice(0, this.maxChars)}\n… truncated (${text.length} chars)`;
	}

	/** Build the tool result, keeping `details` small. */
	private message(
		view: TranscriptParams["view"],
		text: string,
		response: Record<string, unknown>,
	): { content: Array<{ type: "text"; text: string }>; details: unknown } {
		const details = {
			view,
			ok: response.ok === true,
			error: response.error,
			entries: response.entryCount,
		};
		return { content: [{ type: "text", text }], details };
	}
}
