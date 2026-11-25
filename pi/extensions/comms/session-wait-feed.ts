/**
 * pi-daemon — the bounded activity snapshot one session wait streams.
 *
 * Owns the lines a `comms_session_wait` call has read so far: what the run's
 * conversation says (prompts, replies, tool calls and results), plus the
 * state the run published. `publish` hands the newest lines to the tool's
 * update callback only when the text changed, so the tool window shows the
 * session working instead of a page of its terminal; the final tool result
 * replaces that text with everything the wait read.
 */

/** One activity or state event as a wait page carries it. */
export interface SessionWaitEvent {
	type?: string;
	kind?: string;
	entryId?: string;
	text?: string;
	tool?: string;
	args?: string;
	isError?: boolean;
	state?: string;
	seq?: number;
	reason?: string;
	elided?: boolean;
}

/** One page of a session's activity. */
export interface SessionWaitPage {
	status?: string;
	name?: string;
	nodeId?: string;
	state?: string;
	events?: SessionWaitEvent[];
	cursor?: unknown;
	more?: boolean;
	maxWaitMs?: number;
}

export class SessionWaitFeed {
	/** Header plus this many lines is pi's collapsed preview, so the live
	 *  tail is never hidden behind an expand hint. */
	private static readonly LIVE_LINES = 9;
	private static readonly LINE_CHARS = 300;

	private readonly lines: string[] = [];
	private runState: string | undefined;
	private busy = false;
	private active = false;
	private published = "";
	private closed = false;

	constructor(
		private readonly name: string,
		private readonly deliver: (text: string) => void,
		private readonly started = performance.now(),
	) {}

	/** Add one page: its lines, the state it published, and what it proves. */
	add(page: SessionWaitPage): void {
		if (page.state !== undefined) this.runState = page.state;
		if (this.runState === "busy") this.busy = true;
		for (const event of page.events ?? []) {
			if (event.type === "activity" && event.kind !== "user")
				this.active = true;
			const line = SessionWaitFeed.line(event);
			if (line) this.lines.push(line);
		}
	}

	/** Whether the session did something and is no longer busy. */
	settled(): boolean {
		return (this.busy || this.active) && this.runState !== "busy";
	}

	/** The state the run last published, as the result names it. */
	get state(): string | undefined {
		return this.runState;
	}

	/** Deliver the newest lines, once, when they changed. */
	publish(): void {
		if (this.closed) return;
		const shown = this.lines.slice(-SessionWaitFeed.LIVE_LINES);
		const text = [
			`${this.name} (${this.runState ?? "unknown"}, ${this.age()}s)`,
			...(shown.length ? shown.map(SessionWaitFeed.clip)
				: ["(no activity yet)"]),
		].join("\n");
		if (text === this.published) return;
		this.published = text;
		try {
			this.deliver(text);
		} catch {
			// The host callback must never break the wait.
		}
	}

	/** The final result: every line read, the newest kept when it overflows. */
	result(status: string, cursor: unknown, maxChars: number): string {
		const head = `${this.name} ${status}: ${this.runState ?? "unknown"} ` +
			`after ${this.age()}s`;
		const trailer = `cursor: ${JSON.stringify(cursor ?? null)}`;
		const body = this.lines.length ? [...this.lines] : ["(no activity)"];
		let omitted = 0;
		let text = SessionWaitFeed.resultText(head, body, omitted, trailer);
		while (text.length > maxChars && body.length > 1) {
			body.shift();
			omitted += 1;
			text = SessionWaitFeed.resultText(head, body, omitted, trailer);
		}
		return text;
	}

	/** Stop publishing; later pages are dropped. */
	close(): void {
		this.closed = true;
	}

	private age(): number {
		return Math.round((performance.now() - this.started) / 1000);
	}

	private static resultText(head: string, body: readonly string[],
		omitted: number, trailer: string): string {
		const lines = [head];
		if (omitted) lines.push(`… ${omitted} earlier lines omitted`);
		lines.push(...body, trailer);
		return lines.join("\n");
	}

	/** One event as the line a reader sees, or none for a state change. */
	private static line(event: SessionWaitEvent): string | undefined {
		switch (event.type) {
			case "activity":
				return SessionWaitFeed.activity(event);
			case "ended":
				return `ended: ${event.reason ?? "gone"}`;
			default:
				return undefined;
		}
	}

	private static activity(event: SessionWaitEvent): string {
		if (event.kind === "gap") {
			return "(the conversation changed; reading it from its start)";
		}
		if (event.elided) {
			return `${event.kind ?? "activity"} ${event.entryId ?? ""}` +
				" (elided: too large for a page)";
		}
		const text = SessionWaitFeed.clean(event.text);
		switch (event.kind) {
			case "user":
				return `user: ${text}`;
			case "assistant":
				return `assistant: ${text}`;
			case "renamed":
				return `renamed: ${text}`;
			case "tool_call":
				return `→ ${event.tool ?? "tool"}` +
					(event.args ? ` ${event.args}` : "");
			case "tool_result":
				return `← ${event.tool ?? "tool"}` +
					(event.isError ? " (error)" : "") +
					(text ? `: ${text}` : "");
			case "compaction":
				return "(compacted)";
			default:
				return `${event.kind ?? "activity"} ${event.entryId ?? ""}`
					.trimEnd();
		}
	}

	private static clean(text: string | undefined): string {
		return (text ?? "").replace(/\s+/g, " ").trim();
	}

	private static clip(line: string): string {
		return line.length <= SessionWaitFeed.LINE_CHARS ? line
			: `${line.slice(0, SessionWaitFeed.LINE_CHARS)}…`;
	}
}
