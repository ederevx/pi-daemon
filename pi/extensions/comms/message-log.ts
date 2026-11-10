/**
 * pi-daemon — the branch record of received agent-comms mail.
 *
 * A wait or replay page is where a peer's report actually lives: the
 * rendered tool text is bounded, so a reader that quotes a verdict out of
 * it holds no evidence. Each received envelope is appended to the branch
 * whole and once per message id, so a later reader settles one delivery
 * exactly once, even across a reload.
 */

import type {
	ExtensionAPI,
	ExtensionToolContext,
} from "@earendil-works/pi-coding-agent";

/** The entry type a received envelope is recorded under. */
export const COMMS_LOG = "pi-agent-comms-log";

/** The envelope the service returns inside a page; every field it carries
 *  is recorded, so a field added later is not lost by this reader. */
interface Envelope {
	messageId?: unknown;
	[key: string]: unknown;
}

/** The branch entry each recorded envelope is stored as. */
type LoggedEnvelope = Envelope & { direction: "received"; messageId: string };

export class CommsMessageLog {
	constructor(private readonly pi: ExtensionAPI) {}

	/** Record every envelope in one answer: a page, several pages, or a
	 *  single envelope. Anything else is not mail and is left alone. */
	record(answer: unknown, ctx?: ExtensionToolContext): void {
		// Only the branch settles a delivery: a message recorded on another
		// branch is this branch's to record, and nothing is claimed until
		// the append itself returned.
		const known = new Set<string>(CommsMessageLog.branch(ctx));
		for (const message of CommsMessageLog.envelopes(answer)) {
			const id = message.messageId;
			if (typeof id !== "string" || !id || known.has(id)) continue;
			const entry: LoggedEnvelope = {
				...message, messageId: id, direction: "received",
			};
			// A branch that refuses the entry must not fail the delivery
			// the answer already made: nothing is claimed, so a later
			// record retries it.
			try {
				this.pi.appendEntry(COMMS_LOG, entry);
			} catch {
				continue;
			}
			known.add(id);
		}
	}

	/** The message ids already on the branch, read in its own shape. */
	private static branch(ctx?: ExtensionToolContext): string[] {
		const entries = ctx?.sessionManager?.getBranch?.() as
			| Array<Record<string, unknown>>
			| undefined;
		if (!Array.isArray(entries)) return [];
		const ids: string[] = [];
		for (const entry of entries) {
			if (!entry || entry.type !== "custom"
				|| entry.customType !== COMMS_LOG) continue;
			const data = entry.data as { messageId?: unknown } | undefined;
			if (typeof data?.messageId === "string" && data.messageId) {
				ids.push(data.messageId);
			}
		}
		return ids;
	}

	/** Every envelope an answer carries, in the order it carries them. */
	private static envelopes(answer: unknown): Envelope[] {
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
}
