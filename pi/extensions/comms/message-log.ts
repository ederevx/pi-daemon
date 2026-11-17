/**
 * pi-daemon — the branch record of agent-comms traffic.
 *
 * Every message the session sends and receives is appended to the
 * branch under one custom type, so the transcript shows the mail as it
 * moved: one dim line per direction in the TUI, expandable to the full
 * payload, and whole envelopes for a later reader. A wait or replay
 * page is where a peer's report actually lives — the rendered tool text
 * is bounded, so a reader that quotes a verdict out of it holds no
 * evidence — and each envelope is appended whole and once per message
 * id, so a later reader settles one delivery exactly once, even across
 * a reload. Sends deduplicate by their request id, which a retried
 * send reuses.
 */

import type {
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";

import { ACK_KIND, type CommsAddress, type CommsEnvelope } from "./protocol.ts";

/** The entry type mail traffic is recorded under. */
export const COMMS_LOG = "pi-agent-comms-log";

/** The envelope the service returns inside a page; every field it carries
 *  is recorded, so a field added later is not lost by this reader. */
interface Envelope {
	messageId?: unknown;
	[key: string]: unknown;
}

/** The default collapsed preview: a one-line, truncated payload. */
export const DEFAULT_PREVIEW_CHARS = 96;

/** The branch entry each recorded message is stored as. */
interface LoggedMessage {
	direction: "sent" | "received";
	[key: string]: unknown;
}

/** A send outcome the log is given, after the service accepted it. */
export interface SentMessage {
	to: CommsAddress;
	kind?: string;
	requestId: string;
	payload: unknown;
	/** "accepted" or "queued"; a queued send is not yet confirmed delivered. */
	status: string;
}

export class CommsMessageLog {
	/** The session context appends go through when a call names none. */
	private activeCtx?: ExtensionContext;

	/** The collapsed preview length; the presentation policy overrides it. */
	constructor(
		private readonly pi: ExtensionAPI,
		private readonly previewChars = DEFAULT_PREVIEW_CHARS,
	) {}

	/** The session context appends go through when a caller names none:
	 *  every path that holds the live session attaches it once at
	 *  session start, so a caller deep in a send path cannot forget it. */
	attach(ctx?: ExtensionContext): void {
		this.activeCtx = ctx;
	}

	/** Register the renderer that draws both directions in the TUI. */
	registerRenderer(pi: ExtensionAPI): void {
		// Non-TUI embedding hosts may not register entry renderers; the
		// branch keeps serving the log without them.
		if (typeof pi.registerEntryRenderer !== "function") return;
		pi.registerEntryRenderer(COMMS_LOG, (entry, options, theme) => {
			const data = entry.data as {
				direction?: "sent" | "received";
				from?: CommsAddress;
				to?: CommsAddress;
				kind?: string;
				preview?: string;
				payload?: unknown;
				requestId?: string;
				messageId?: string;
				sequence?: number;
				status?: string;
			} | undefined;
			const sent = data?.direction === "sent";
			const arrow = sent ? "->" : "<-";
			const peer = sent ? data?.to : data?.from;
			const head = `[agent-comms] ${arrow} ` +
				`${CommsMessageLog.address(peer)} (${data?.kind ?? "text"})`;
			if (options.expanded) {
				const lines = [
					theme.fg("dim", head +
						(data?.status ? ` (${data.status})` : "")),
					theme.fg("dim", CommsMessageLog.identifiers(data)),
				];
				for (const line of CommsMessageLog.payloadText(data?.payload)
					.split("\n")) {
					lines.push(theme.fg("dim", `  ${line}`));
				}
				return { render: () => lines, invalidate() {} };
			}
			const preview = data?.preview ??
				CommsMessageLog.preview(
					CommsMessageLog.payloadText(data?.payload),
					this.previewChars);
			return {
				render: () => [theme.fg("dim", `${head}: ${preview}`)],
				invalidate() {},
			};
		});
	}

	/** Record every envelope in one received answer: a page, several
	 *  pages, or a single envelope. Anything else is not mail and is
	 *  left alone. */
	record(answer: unknown, ctx?: ExtensionContext): void {
		for (const message of CommsMessageLog.envelopes(answer)) {
			this.appendEnvelope(message as CommsEnvelope, ctx);
		}
	}

	/** Append one received envelope whole, once per message id, and
	 *  answer whether the branch now holds it durably — false is a
	 *  failed append, the caller's cue to hold the receive cursor. */
	appendEnvelope(
		envelope: CommsEnvelope | undefined,
		ctx?: ExtensionContext,
	): boolean {
		const id = envelope?.messageId;
		if (!envelope || typeof id !== "string" || !id) return false;
		if (CommsMessageLog.receivedIds(ctx ?? this.activeCtx).includes(id)) {
			return true;
		}
		try {
			this.pi.appendEntry(COMMS_LOG, {
				...envelope,
				direction: "received" as const,
				preview: CommsMessageLog.preview(
					CommsMessageLog.payloadText(envelope.payload),
					this.previewChars),
			});
			return true;
		} catch {
			// Nothing is claimed; the next record of the same message id
			// retries.
			return false;
		}
	}

	/** Record one send the service accepted or queued; the request id
	 *  keeps a retried send to a single entry. Answers whether the
	 *  traffic is durably recorded — false is a refused append, and the
	 *  caller's cue to keep the send's own progress unclaimed. */
	sent(message: SentMessage, ctx?: ExtensionContext): boolean {
		if (CommsMessageLog.sentRequestIds(ctx ?? this.activeCtx)
			.has(message.requestId)) {
			return true;
		}
		const entry: LoggedMessage = {
			direction: "sent",
			to: message.to,
			kind: message.kind ?? "text",
			requestId: message.requestId,
			status: message.status,
			payload: message.payload,
			preview: CommsMessageLog.preview(
				CommsMessageLog.payloadText(message.payload),
				this.previewChars),
		};
		try {
			this.pi.appendEntry(COMMS_LOG, entry);
			return true;
		} catch {
			// The traffic entry is not durable; the next record or the
			// retry of the same request id rewrites it.
			return false;
		}
	}

	/** The message ids this branch already recorded as received. */
	static receivedIds(ctx?: ExtensionContext): string[] {
		return CommsMessageLog.scan(ctx, (data) =>
			data.direction !== "sent" && typeof data.messageId === "string"
				? [data.messageId]
				: []);
	}

	/** The received entries whole, in branch order — every envelope
	 *  field the receipt recorded, for a reload to reconcile against. */
	static receivedEntries(
		ctx?: ExtensionContext,
	): Array<Record<string, unknown>> {
		const entries = ctx?.sessionManager?.getBranch?.() as
			| Array<Record<string, unknown>>
			| undefined;
		if (!Array.isArray(entries)) return [];
		const found: Array<Record<string, unknown>> = [];
		for (const entry of entries) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_LOG) continue;
			const data = (entry.data ?? {}) as Record<string, unknown>;
			if (data.direction !== "sent") found.push(data);
		}
		return found;
	}

	/** The request id one delivered message's acknowledgement reply was
	 *  sent under, when this branch already recorded that reply. */
	static replyRequestIdFor(
		messageId: string,
		ctx?: ExtensionContext,
	): string | undefined {
		const entries = ctx?.sessionManager?.getBranch?.() as
			| Array<Record<string, unknown>>
			| undefined;
		if (!Array.isArray(entries)) return undefined;
		for (const entry of entries) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_LOG) continue;
			const data = (entry.data ?? {}) as Record<string, unknown>;
			if (data.direction !== "sent" || data.kind !== ACK_KIND) continue;
			const payload = data.payload as Record<string, unknown> | undefined;
			if (payload?.messageId !== messageId) continue;
			if (typeof data.requestId === "string" && data.requestId) {
				return data.requestId;
			}
		}
		return undefined;
	}

	/** The request ids this branch already recorded as sent. */
	static sentRequestIds(ctx?: ExtensionContext): Set<string> {
		return new Set(CommsMessageLog.scan(ctx, (data) =>
			data.direction === "sent" && typeof data.requestId === "string"
				? [data.requestId]
				: []));
	}

	/** Map one branch's log entries through `pick`, flatly. */
	private static scan(
		ctx?: ExtensionContext,
		pick: (data: Record<string, unknown>) => string[],
	): string[] {
		const entries = ctx?.sessionManager?.getBranch?.() as
			| Array<Record<string, unknown>>
			| undefined;
		if (!Array.isArray(entries)) return [];
		const found: string[] = [];
		for (const entry of entries) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_LOG) continue;
			found.push(...pick((entry.data ?? {}) as Record<string, unknown>));
		}
		return found;
	}

	/** Every envelope an answer carries, in the order it carries them. */
	static envelopes(answer: unknown): Envelope[] {
		if (Array.isArray(answer)) return answer.flatMap((page) =>
			CommsMessageLog.envelopes(page));
		if (!answer || typeof answer !== "object") return [];
		const record = answer as Record<string, unknown>;
		if (Array.isArray(record.messages)) {
			return record.messages.filter((message): message is Envelope =>
				!!message && typeof message === "object");
		}
		return typeof record.messageId === "string" ? [record as Envelope] : [];
	}

	/** The payload as display text: a string as-is, any other value as
	 *  JSON, nothing when there is none. */
	private static payloadText(payload: unknown): string {
		if (payload === undefined || payload === null) return "";
		return typeof payload === "string"
			? payload
			: JSON.stringify(payload);
	}

	/** One collapsed line, truncated to `chars` when longer. */
	static preview(text: string, chars: number): string {
		const collapsed = text.replace(/\s+/g, " ").trim();
		return collapsed.length > chars
			? `${collapsed.slice(0, Math.max(chars - 3, 0))}...`
			: collapsed;
	}

	/** A peer as `agent@node`, the node left off when absent. */
	static address(address?: CommsAddress): string {
		if (!address?.agentId) return "?";
		return address.nodeId
			? `${address.agentId}@${address.nodeId}`
			: address.agentId;
	}

	/** The identifiers the expanded view names, as one line. */
	private static identifiers(
		data?: { messageId?: string; from?: CommsAddress; to?: CommsAddress;
			requestId?: string; sequence?: number },
	): string {
		const parts: string[] = [];
		if (data?.messageId) parts.push(`message ${data.messageId}`);
		if (data?.requestId) parts.push(`request ${data.requestId}`);
		if (data?.sequence !== undefined) parts.push(`# ${data.sequence}`);
		if (data?.from) parts.push(`from ${CommsMessageLog.address(data.from)}`);
		if (data?.to) parts.push(`to ${CommsMessageLog.address(data.to)}`);
		return parts.join(", ");
	}

}
