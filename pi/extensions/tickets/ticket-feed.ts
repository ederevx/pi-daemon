/**
 * pi-daemon — the bounded multi-ticket snapshot for the live tool view.
 *
 * Owns the ordered sections one wait is streaming: each ticket's latest
 * state plus its retained output tail. `deliverIfChanged` builds the
 * bounded snapshot text and the per-stream detail the TUI renderer
 * draws, and hands both to the tool's update callback only when the
 * text changed. The snapshot is capped at 16 KiB by dropping the oldest
 * line from the longest section, never below a section's header, so no
 * stream is pushed out of view; at most eight streams are rendered and
 * the rest collapse onto one `+N more:` line.
 */

import type { TicketStream } from "./ticket-stream.ts";

export type TicketState = "running" | "done" | "failed" | "cancelled" | "lost";

/** The ticket fields the feed reads; offload.ts's Ticket satisfies this
 *  structurally, so the feed never imports it. */
export interface FeedTicket {
	id: string;
	command: string;
	status: TicketState;
	exit: number | null;
	error: string | null;
	started: number;
}

/** One stream as the TUI renderer draws it. */
export interface StreamDetail {
	id: string;
	command: string;
	state: TicketState;
	exit: number | null;
	runtimeSeconds: number;
	tail: string;
	behind: boolean;
}

/** One delivered revision: the model-facing text and the view's data. */
export interface FeedFrame {
	text: string;
	streams: StreamDetail[];
}

interface Section {
	header: string;
	lines: string[];
}

export class TicketFeed {
	private static readonly MAX_BYTES = 16 * 1024;
	private static readonly MAX_LINE_BYTES = 400;
	private static readonly MAX_STREAMS = 8;

	private readonly order: string[] = [];
	private readonly tickets = new Map<string, FeedTicket>();
	private streams = new Map<string, TicketStream>();
	private waitSeconds = 0;
	private lastText = "";
	private closed = false;

	constructor(
		ids: readonly string[],
		private readonly deliver: (frame: FeedFrame) => void,
	) {
		for (const id of ids) this.order.push(id);
	}

	/** Records the tick's statuses and the streams the wait is pulling. */
	update(
		tickets: readonly FeedTicket[],
		streams: ReadonlyMap<string, TicketStream>,
		waitSeconds: number,
	): void {
		for (const ticket of tickets) this.tickets.set(ticket.id, ticket);
		this.streams = new Map(streams);
		this.waitSeconds = waitSeconds;
	}

	/** Delivers the current frame when its text changed. The host's
	 *  callback must never be able to break the wait. */
	deliverIfChanged(): void {
		if (this.closed) return;
		const frame = this.frame();
		if (frame.text === this.lastText) return;
		this.lastText = frame.text;
		try {
			this.deliver(frame);
		} catch {
			// The renderer owns its own failures; the wait goes on.
		}
	}

	/** The current bounded snapshot and its per-stream detail. */
	frame(): FeedFrame {
		return { text: this.snapshot(), streams: this.details() };
	}

	/** Stops delivering; the final tool result supersedes the feed. */
	close(): void {
		this.closed = true;
	}

	private snapshot(): string {
		const sections: Section[] = [];
		for (const id of this.order.slice(0, TicketFeed.MAX_STREAMS)) {
			const section = this.section(id);
			if (section) sections.push(section);
		}
		let bytes = this.bytes(sections);
		while (bytes > TicketFeed.MAX_BYTES) {
			const longest = this.longest(sections);
			if (!longest || longest.lines.length === 0) break;
			bytes -= Buffer.byteLength(longest.lines.shift()!, "utf8") + 1;
		}
		const out = sections.map((s) => [s.header, ...s.lines].join("\n"));
		if (this.order.length > TicketFeed.MAX_STREAMS) {
			const extra = this.order
				.slice(TicketFeed.MAX_STREAMS)
				.map((id) => `${id}(${this.tickets.get(id)?.status ?? "running"})`);
			out.push(`+${extra.length} more: ${extra.join(", ")}`);
		}
		return out.join("\n");
	}

	private section(id: string): Section | null {
		const ticket = this.tickets.get(id);
		if (!ticket) return null;
		const tail = this.streams.get(id)?.tail() ?? "";
		const lines = tail === ""
			? []
			: tail.split("\n").map((line) => this.clip(line));
		if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
		return { header: this.header(ticket), lines };
	}

	/** The legacy running wording keeps the single-ticket snapshot
	 *  byte-compatible with the old wait status line. */
	private header(ticket: FeedTicket): string {
		if (ticket.status === "running") {
			return `waiting for ticket ${ticket.id} ` +
				`(${Math.round(this.waitSeconds)}s elapsed)`;
		}
		const parts = [`ticket ${ticket.id} ${ticket.status}`];
		if (ticket.exit !== null) parts.push(`exit ${ticket.exit}`);
		if (ticket.error) parts.push(`(${ticket.error})`);
		return parts.join(" ");
	}

	private details(): StreamDetail[] {
		const now = Date.now() / 1000;
		return this.order.map((id) => {
			const ticket = this.tickets.get(id);
			const stream = this.streams.get(id);
			return {
				id,
				command: ticket?.command ?? "",
				state: ticket?.status ?? "running",
				exit: ticket?.exit ?? null,
				runtimeSeconds: ticket
					? Math.max(0, now - ticket.started)
					: this.waitSeconds,
				tail: stream?.tail() ?? "",
				behind: stream?.behind ?? false,
			};
		});
	}

	private bytes(sections: readonly Section[]): number {
		let total = 0;
		for (const section of sections) {
			total += Buffer.byteLength(section.header, "utf8") + 1;
			for (const line of section.lines) {
				total += Buffer.byteLength(line, "utf8") + 1;
			}
		}
		return total;
	}

	private longest(sections: readonly Section[]): Section | undefined {
		let best: Section | undefined;
		for (const section of sections) {
			if (!best || section.lines.length > best.lines.length) best = section;
		}
		return best;
	}

	/** Clip one over-long line to the newest MAX_LINE_BYTES bytes. */
	private clip(text: string): string {
		if (Buffer.byteLength(text, "utf8") <= TicketFeed.MAX_LINE_BYTES) return text;
		let size = 0;
		let start = text.length;
		while (start > 0) {
			const width = Buffer.byteLength(text[start - 1], "utf8");
			if (size + width > TicketFeed.MAX_LINE_BYTES) break;
			size += width;
			start -= 1;
		}
		return `\u2026${text.slice(start)}`;
	}
}
