/**
 * Command offloading for Pi — every shell command becomes a daemon-owned
 * ticket.
 *
 * The extension overrides the built-in `bash` tool with a variant whose
 * execution backend submits each command to pi-daemon as a ticket and
 * waits for the result. Output is delivered in ONE GO when the command
 * finishes (nothing is streamed into the tool result while it runs), so
 * agents read a complete result exactly once. Three departures from the
 * stock backend, all deliberate:
 *
 * - When the command outlives the wait bound (the tool's own timeout, or
 *   PI_OFFLOAD_WAIT seconds, default 120), the ticket keeps running in
 *   the daemon, the tool result hands off cleanly ("still running, you
 *   will be notified"), and the agent is freed immediately. When the
 *   ticket finishes the extension delivers the full output as a
 *   follow-up message (triggerTurn + followUp), so the result arrives
 *   unprompted even several turns later.
 * - A user abort (Escape) cancels the ticket in the daemon instead of
 *   killing a local process tree.
 * - If the daemon is unreachable, execution falls back to pi's local
 *   shell backend transparently — the agent only ever sees normal bash
 *   behavior. PI_OFFLOAD=off disables offloading entirely.
 *
 * Tickets survive everything else: they outlive the submitting session
 * (daemon-owned execution), are persisted in the daemon's tickets.json,
 * and a session that crashes or restarts re-arms watchers on
 * session_start so pending results still get delivered. The `daemon_tasks`
 * tool exposes the same machinery explicitly: background submission,
 * status, one-go result fetch, live watch (streamed via partial updates),
 * cancel, and per-session listing.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	createBashTool,
	createLocalBashOperations,
	truncateTail,
	DEFAULT_MAX_BYTES,
	DEFAULT_MAX_LINES,
	DynamicBorder,
	getSettingsListTheme,
	type BashOperations,
} from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import {
	Box,
	matchesKey,
	Text,
	truncateToWidth,
	visibleWidth,
	type Component,
	type TUI,
	type TuiMouseEvent,
} from "@earendil-works/pi-tui";
import { ProcessRunner, resolvePiRc } from "./daemon.ts";

/** pi-rc exit codes: 4 = unreachable before any request landed (safe to
 *  run the task locally); 7 = the request was sent but the outcome is
 *  unknown — the daemon may have acted on it, so callers must adopt any
 *  matching ticket instead of re-running. */
const EXIT_NO_DAEMON = 4;
const EXIT_AMBIGUOUS = 7;

/** Default hand-off bound for bash offloading, in seconds. */
const DEFAULT_WAIT_SECONDS = 120;

/** Longest single ticket-wait round trip, in seconds. */
const WAIT_CHUNK_SECONDS = 300;

export interface TicketCost {
	input?: number;
	output?: number;
	cacheRead?: number;
	cacheWrite?: number;
	total?: number;
}

export interface TicketUsage {
	input?: number;
	output?: number;
	cacheRead?: number;
	cacheWrite?: number;
	totalTokens?: number;
	cost?: TicketCost;
}

/** One ticket record as the daemon stores and returns it. */
export interface Ticket {
	id: string;
	session: string;
	cwd: string;
	command: string;
	status: "running" | "done" | "failed" | "cancelled" | "lost";
	kind?: "shell";
	created: number;
	started: number;
	finished: number | null;
	exit: number | null;
	term: number | null;
	truncated: boolean;
	error: string | null;
	usage?: TicketUsage;
	cost?: TicketCost;
	input?: number;
	cacheRead?: number;
	cacheWrite?: number;
	totalTokens?: number;
	final_message?: string;
	output?: string;
}

/** Error thrown when the daemon cannot be reached or misbehaves.
 *  ambiguous=true means the request may have been acted on already. */
class DaemonUnavailable extends Error {
	constructor(
		message: string,
		public readonly ambiguous = false,
	) {
		super(message);
	}
}


/** Formats one completed ticket + its output as agent-facing text. */
function formatResult(ticket: Ticket, output: string): string {
	const cost = ticket.cost?.total ?? ticket.usage?.cost?.total;
	const costStr = typeof cost === "number" && cost > 0 ? ` · $${cost.toFixed(4)}` : "";
	const tokens = ticket.totalTokens ?? ticket.usage?.totalTokens;
	const tokStr = typeof tokens === "number" && tokens > 0 ? ` · ${tokens} tok` : "";
	const header = [
		`ticket ${ticket.id} ${ticket.status}`,
		ticket.exit !== null ? `exit ${ticket.exit}` : null,
		ticket.error ? `(${ticket.error})` : null,
	]
		.filter(Boolean)
		.join(" ") + costStr + tokStr;
	if (!output) return header;
	const truncation = truncateTail(output, {
		maxLines: DEFAULT_MAX_LINES,
		maxBytes: DEFAULT_MAX_BYTES,
	});
	let text = truncation.content || "(no output)";
	if (truncation.truncated) {
		text += `\n[Output truncated: ${truncation.outputLines} of ${truncation.totalLines} lines kept]`;
	}
	return `${header}\n${text}`;
}

/** The owning session's stable key: the hosted session's short name
 *  when hosted, else the conversation file's stem, else "standalone". */
export function sessionKeyOf(sessionFile?: string | null): string {
	const hosted = process.env.PI_HOSTED_SESSION;
	if (hosted) return hosted.replace(/^pi-/, "");
	const file = sessionFile || process.env.PI_SESSION_FILE;
	if (file) return file.replace(/\.jsonl$/, "").split("/").pop() || "standalone";
	return "standalone";
}

/** The pi-rc subprocess surface: one method per client command. */
export class TicketClient {
	private readonly runner: ProcessRunner;
	private readonly piRc: string;

	constructor(runner: ProcessRunner) {
		this.runner = runner;
		this.piRc = resolvePiRc();
	}

	/** Runs pi-rc and returns its stdout; throws DaemonUnavailable on an
	 *  unreachable daemon (exit 4) or a failed spawn. */
	private async run(args: string[], timeoutSeconds?: number): Promise<string> {
		let result: Awaited<ReturnType<ProcessRunner["run"]>>;
		try {
			result = await this.runner.run(this.piRc, args);
		} catch (exc) {
			throw new DaemonUnavailable(`pi-rc failed: ${String(exc)}`);
		}
		if (result.code === EXIT_NO_DAEMON) {
			throw new DaemonUnavailable("background service is not running");
		}
		if (result.code === EXIT_AMBIGUOUS) {
			throw new DaemonUnavailable("submit outcome unknown (connection lost mid-request)", true);
		}
		if (result.code !== 0) {
			const detail = (result.stderr || result.stdout || "").trim();
			// A silent nonzero exit is an abnormal client death (signal,
			// OOM) whose request may already have landed daemon-side:
			// treat it as ambiguous so the caller never re-runs the
			// command locally behind a duplicate ticket.
			if (!detail) {
				throw new DaemonUnavailable(
					`pi-rc exit ${result.code} without output (abnormal death)`,
					true);
			}
			throw new DaemonUnavailable(detail || `pi-rc exit ${result.code}`);
		}
		return result.stdout || "";
	}

	async submit(
		session: string,
		cwd: string,
		command: string,
		extraEnv: Record<string, string>,
	): Promise<string> {
		const args = ["ticket-submit", "--session", session, "--cwd", cwd];
		for (const [key, value] of Object.entries(extraEnv)) {
			args.push("--env", `${key}=${value}`);
		}
		args.push("--", command);
		const out = await this.run(args);
		const match = /^ticket (\S+)$/m.exec(out.trim());
		if (!match) {
			throw new DaemonUnavailable(`unexpected ticket-submit reply: ${out.trim()}`);
		}
		return match[1];
	}

	/** Publishes a run-state for a session daemon-side (pi-rc state),
	 *  best-effort: never throws, cosmetic when the daemon is down. */
	async setState(session: string, state: string): Promise<void> {
		try {
			await this.runner.run(this.piRc, ["state", session, state]);
		} catch {
			// State display is best-effort.
		}
	}

	/** Blocks at most timeoutSeconds; timeout 0 is a status probe. */
	async wait(id: string, timeoutSeconds: number): Promise<Ticket> {
		const args = ["ticket-wait", id];
		if (timeoutSeconds > 0) args.push(String(timeoutSeconds));
		const out = await this.run(args, timeoutSeconds + 15);
		const line = out.trim().split("\n")[0] || "";
		const ticket = JSON.parse(line) as Ticket;
		if (!ticket || typeof ticket.id !== "string") {
			throw new DaemonUnavailable("malformed ticket record");
		}
		return ticket;
	}

	/** Fetches the final output text and usage from the daemon. */
	async getOutput(id: string): Promise<{
		ok: boolean;
		id: string;
		status: string;
		exit: number | null;
		output: string;
		usage?: TicketUsage;
	}> {
		const out = await this.run(["ticket-output", id, "--json"]);
		try {
			return JSON.parse(out.trim());
		} catch {
			return { ok: true, id, status: "unknown", exit: null, output: out };
		}
	}

	/** Reads the whole output log in bounded chunks. */
	async outputAll(id: string): Promise<string> {
		const parts: Buffer[] = [];
		let offset = 0;
		for (let i = 0; i < 256; i++) {
			const out = await this.run(["ticket-output", id, String(offset)]);
			const chunk = Buffer.from(out, "binary");
			if (chunk.length === 0) break;
			parts.push(chunk);
			offset += chunk.length;
		}
		return Buffer.concat(parts).toString("utf8");
	}

	/** Drops a finished ticket (and its artifacts) from the store. */
	async remove(id: string): Promise<string> {
		await this.run(["ticket-remove", id]);
		return id;
	}

	/** Resets the whole ticketing state: cancels every running ticket,
	 *  wipes the store and artifacts. */
	async resetAll(): Promise<{ cancelled: number; removed: number }> {
		const out = await this.run(["tickets-reset"]);
		const c = /cancelled (\d+)/.exec(out);
		const r = /removed (\d+)/.exec(out);
		return { cancelled: Number(c?.[1] ?? 0), removed: Number(r?.[1] ?? 0) };
	}

	async list(session?: string): Promise<Ticket[]> {
		const args = ["ticket-list"];
		if (session) args.push(session);
		const out = await this.run(args);
		return out
			.trim()
			.split("\n")
			.filter(Boolean)
			.map((line) => JSON.parse(line) as Ticket);
	}

	async cancel(id: string): Promise<Ticket> {
		const out = await this.run(["ticket-cancel", id]);
		return this.wait(id, 0);
	}

}

/**
 * Session-side task tracking: the watcher registry. Watchers poll a
 * ticket to completion and deliver its full output as one follow-up
 * message; a session restart re-arms watchers for its still-running
 * tickets; a result fetched through the tool, or consumed by an inline
 * bash wait, suppresses the duplicate notification.
 */
class DaemonTasks {
	/** The pi-rc surface; exposed for the bash backend's wait/output. */
	readonly client: TicketClient;
	private readonly send: (
		message: { customType: string; content: string; display: boolean; details?: unknown },
		options: { triggerTurn: boolean; deliverAs: "steer" | "followUp" },
	) => void;
	private readonly append: (customType: string, data: unknown) => void;

	/** ticket id -> live watcher; owning object mutates this only. */
	private watchers = new Map<string, { stopped: boolean }>();

	/** Tickets whose result was already fetched by an explicit call. */
	private fetched = new Set<string>();

	/** Records that the caller consumed a ticket's result itself (an
	 *  inline bash wait or an explicit fetch); the armed delivery
	 *  watcher then stays silent so one ticket notifies exactly once. */
	markFetched(id: string): void {
		this.fetched.add(id);
	}


	constructor(
		runner: ProcessRunner,
		send: DaemonTasks["send"],
		append: DaemonTasks["append"],
	) {
		this.client = new TicketClient(runner);
		this.send = send;
		this.append = append;
	}

	/** The owning session's stable key: the hosted session's short name
	 *  when hosted, else the conversation file's stem, else standalone. */
	sessionKey(sessionFile?: string | null): string {
		return sessionKeyOf(sessionFile);
	}

	/** Publishes a run-state for this session (pi-rc state <key> <state>),
	 *  fire-and-forget: one cheap pi-rc exec, all errors swallowed. */
	setSessionState(state: string): void {
		void this.client.setState(this.sessionKey(), state).catch(() => {});
	}

	async submit(sessionFile: string | null, cwd: string, command: string,
		extraEnv: Record<string, string> = {},
	): Promise<string> {
		const id = await this.client.submit(
			this.sessionKey(sessionFile), cwd, command, extraEnv);
		this.armDelivery(id, command);
		return id;
	}

	status(id: string): Promise<Ticket> {
		return this.client.wait(id, 0);
	}

	/** One-go result fetch; marks the ticket fetched so the watcher
	 *  will not also deliver it. */
	async result(id: string, waitSeconds: number): Promise<{ ticket: Ticket; output: string }> {
		const ticket = await this.client.wait(id, waitSeconds);
		if (ticket.status !== "running") {
			this.fetched.add(id);
			return { ticket, output: await this.client.outputAll(id) };
		}
		return { ticket, output: "" };
	}

	/** Streams a running ticket's output via onUpdate until it finishes;
	 *  blocking waits mean one pi-rc round trip per 300s, not a spin. */
	async watch(id: string, onUpdate?: (text: string) => void): Promise<{ ticket: Ticket; output: string }> {
		let ticket = await this.client.wait(id, 0);
		while (ticket.status === "running") {
			ticket = await this.client.wait(id, WAIT_CHUNK_SECONDS);
			if (ticket.status === "running" && onUpdate) {
				const output = await this.client.outputAll(id);
				onUpdate(output);
			}
		}
		this.fetched.add(id);
		return { ticket, output: await this.client.outputAll(id) };
	}

	async cancel(id: string): Promise<Ticket> {
		const watcher = this.watchers.get(id);
		if (watcher) watcher.stopped = true;
		this.watchers.delete(id);
		return this.client.cancel(id);
	}

	list(sessionFile: string | null): Promise<Ticket[]> {
		return this.client.list(this.sessionKey(sessionFile));
	}

	/** Arms a background watcher: poll to completion, then deliver the
	 *  full output as one follow-up message unless it was already
	 *  fetched. Fire-and-forget; failures only lose the notification,
	 *  the daemon-side result stays fetchable. */
	armDelivery(id: string, command: string): void {
		if (this.watchers.has(id)) return;
		const watcher = { stopped: false };
		this.watchers.set(id, watcher);
		void (async () => {
			try {
				let ticket = await this.client.wait(id, WAIT_CHUNK_SECONDS);
				while (ticket.status === "running" && !watcher.stopped) {
					ticket = await this.client.wait(id, WAIT_CHUNK_SECONDS);
				}
				if (watcher.stopped || this.fetched.has(id)) return;
				const output = await this.client.outputAll(id);
				if (this.fetched.has(id)) return;
				// Steer, not followUp: the notification must reach the
				// agent after its current tool calls finish but BEFORE
				// its next model call, so it learns the task completed
				// instead of re-running it. followUp waits for full idle,
				// which let agents duplicate work.
				// The user sees a one-line card (full detail lives in
				// /daemon-tasks); the agent gets the full result
				// invisibly.
				this.append("daemon-task", { ticket });
				this.send(
					{
						customType: "daemon-task",
						content: `Background task finished: ${command}\n${formatResult(ticket, output)}`,
						display: false,
						details: { ticket },
					},
					{ triggerTurn: true, deliverAs: "steer" },
				);
			} catch {
				// Daemon went away mid-watch: nothing to deliver. The
				// persisted result is still there for an explicit fetch.
			} finally {
				this.watchers.delete(id);
			}
		})();
	}

	/** session_start: re-arm watchers for this session's still-running
	 *  tickets so results survive crashes and restarts. */
	async rearm(sessionFile: string | null): Promise<void> {
		try {
			const tickets = await this.list(sessionFile);
			for (const ticket of tickets) {
				if (ticket.status === "running") {
					this.armDelivery(ticket.id, ticket.command);
				}
			}
		} catch {
			// daemon unavailable; nothing to re-arm
		}
	}

	/** session_shutdown: stop in-process watchers; the tickets keep
	 *  running daemon-side and a later session_start re-arms. */
	stopWatching(): void {
		for (const watcher of this.watchers.values()) {
			watcher.stopped = true;
		}
		this.watchers.clear();
	}
}

/** Ambiguous submit recovery: find a shell ticket created recently for
 *  this session with the exact same command, so the caller adopts the
 *  daemon's copy instead of re-running the command locally. */
async function adoptRecentShellTicket(
	tasks: DaemonTasks,
	sessionFile: string | null,
	command: string,
): Promise<string | null> {
	try {
		const tickets = await tasks.list(sessionFile);
		const cutoff = Date.now() / 1000 - 60;
		const matches = tickets
			.filter((t) => t.kind === "shell" && t.command === command
				&& (t.created ?? 0) >= cutoff)
			.sort((a, b) => b.created - a.created);
		return matches[0]?.id ?? null;
	} catch {
		return null;
	}
}

/** Character-wrap text to width (0/negative width returns it as-is). */
function wrapLine(text: string, width: number): string[] {
	if (width <= 0 || text.length <= width) return [text];
	const out: string[] = [];
	for (let i = 0; i < text.length; i += width) {
		out.push(text.slice(i, i + width));
	}
	return out;
}

/** The session env vars the offloaded command should inherit beyond the
 *  pi process environment pi-rc already forwards. */
function sessionEnvExtra(env?: NodeJS.ProcessEnv): Record<string, string> {
	const extra: Record<string, string> = {};
	if (!env) return extra;
	for (const key of [
		"PI_SESSION_ID",
		"PI_SESSION_FILE",
		"PI_PROVIDER",
		"PI_MODEL",
		"PI_REASONING_LEVEL",
	]) {
		const value = env[key];
		if (typeof value === "string" && value) extra[key] = value;
	}
	return extra;
}

// -- /daemon-tasks dock ----------------------------------------------

interface TaskRow {
	ticket: Ticket;
}

/** Settings-styled dock listing every daemon ticket across sessions:
 *  Running first, then Finished. Enter cancels a running ticket or
 *  removes a finished one; the owning session's own watcher delivers
 *  the cancellation notice when it is still alive. The list live-polls
 *  the daemon once a second while mounted. */
class DaemonTasksDock {
	private readonly st = getSettingsListTheme();
	private readonly border = new DynamicBorder((s: string) => this.theme?.fg("border", s) ?? s);
	private readonly client: TicketClient;
	private rows: TaskRow[] = [];
	/** This session's tickets (always visible, newest first). */
	private sessionRows: TaskRow[] = [];
	/** Every other session's tickets (collapsed by default). */
	private globalRows: TaskRow[] = [];
	/** Whether the global (other-sessions) section is expanded. */
	private globalExpanded = false;
	/** Selectable header rows, for click-to-toggle on the global one. */
	private headerMap: { y: number; section: "session" | "global" }[] = [];
	private selected = 0;
	private expandedId: string | null = null;
	private readonly outputs = new Map<string, string>();
	private pollTimer: ReturnType<typeof setTimeout> | undefined;
	private lastError: string | null = null;
	private rowMap: { y: number; index: number }[] = [];
	private tui: TUI | null = null;
	private theme: { fg: (role: string, text: string) => string } | null = null;
	private done: ((result: null) => void) | null = null;
	// ui.custom re-invokes the mount factory per render; without these
	// guards every mount started another 1s poll chain, multiplying into
	// a spawn storm of pi-rc children (with unreaped zombies).
	private mounted = false;
	private stopped = false;
	private pollInFlight = false;

	constructor(client: TicketClient, private readonly sessionKey: string) {
		this.client = client;
	}

	/** Rebuilds the flat visible row list from the two sections and
	 *  keeps the selection inside it. */
	private rebuildRows(): void {
		this.rows = [
			...this.sessionRows,
			...(this.globalExpanded ? this.globalRows : []),
		];
		this.selected = Math.min(this.selected, Math.max(0, this.rows.length - 1));
	}

	/** Mounts the dock like /settings (non-overlay ui.custom) and blocks
	 *  until Esc; cancel/remove actions happen in-view and refresh. */
	async run(ui: any): Promise<void> {
		try {
			await ui.custom((tui: TUI, theme: any, _kb: any, done: (r: null) => void) => {
				void _kb;
				this.tui = tui;
				this.theme = theme;
				this.done = done;
				if (!this.mounted) {
					this.mounted = true;
					void this.poll();
				}
				return this as unknown as Component;
			});
		} catch {
			/* dock unavailable or canceled */
		} finally {
			this.stop();
		}
	}

	stop(): void {
		this.stopped = true;
		if (this.pollTimer) clearTimeout(this.pollTimer);
		this.pollTimer = undefined;
	}

	private async poll(): Promise<void> {
		// Exactly one chain, one in-flight request, ever.
		if (this.stopped || this.pollInFlight) return;
		this.pollInFlight = true;
		try {
			const tickets = await this.client.list();
			// Session tickets first (always visible), then everything
			// else behind the collapsible global section; each section
			// newest first, top to bottom.
			const byNewest = (a: Ticket, b: Ticket) =>
				(b.started ?? b.created) - (a.started ?? a.created);
			this.sessionRows = tickets
				.filter((t) => t.session === this.sessionKey)
				.sort(byNewest)
				.map((ticket) => ({ ticket }));
			this.globalRows = tickets
				.filter((t) => t.session !== this.sessionKey)
				.sort(byNewest)
				.map((ticket) => ({ ticket }));
			this.rebuildRows();
			this.lastError = null;
		} catch (err) {
			this.rows = [];
			this.sessionRows = [];
			this.globalRows = [];
			this.lastError = err instanceof Error ? err.message : String(err);
		} finally {
			this.pollInFlight = false;
		}
		if (this.selected >= this.rows.length) {
			this.selected = Math.max(0, this.rows.length - 1);
		}
		this.tui?.requestRender();
		if (!this.stopped) {
			this.pollTimer = setTimeout(() => void this.poll(), 1000);
		}
	}

	render(width: number): string[] {
		const lines = [...this.border.render(width)];
		if (this.rows.length === 0 && this.globalRows.length === 0) {
			const msg = this.lastError
				? `  Daemon unreachable: ${this.lastError}`
				: "  No daemon tickets.";
			lines.push(this.st.hint(truncateToWidth(msg, width)));
		} else {
			lines.push(...this.renderBody(width));
		}
		lines.push(this.st.hint(truncateToWidth(
			"  up/down navigate - enter expand - tab sections - c cancel/remove - esc close (live)", width)));
		lines.push(...this.border.render(width));
		return lines;
	}

	invalidate(): void {}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
			this.stop();
			this.done?.(null);
			return;
		}
		if (matchesKey(data, "tab")) {
			// Fold/unfold the global (other-sessions) section.
			this.globalExpanded = !this.globalExpanded;
			this.rebuildRows();
			this.tui?.requestRender();
			return;
		}
		if (this.rows.length === 0) return;
		if (matchesKey(data, "up")) {
			this.selected = (this.selected - 1 + this.rows.length) % this.rows.length;
		} else if (matchesKey(data, "down")) {
			this.selected = (this.selected + 1) % this.rows.length;
		} else if (matchesKey(data, "enter") || data === " ") {
			const row = this.rows[this.selected];
			const id = row.ticket.id;
			if (this.expandedId === id) {
				this.expandedId = null;
			} else {
				this.expandedId = id;
				if (!this.outputs.has(id)) {
					// Cache the full output once; the expanded view reads
					// it from the cache on every render.
					void this.client.outputAll(id).then((output) => {
						this.outputs.set(id, output);
						this.tui?.requestRender();
					}).catch(() => this.poll());
				}
			}
		} else if (data === "c") {
			const row = this.rows[this.selected];
			const action = row.ticket.status === "running"
				? this.client.cancel(row.ticket.id)
				: this.client.remove(row.ticket.id);
			void action.catch(() => undefined).then(() => this.poll());
			return;
		} else return;
		this.tui?.requestRender();
	}

	handleMouse(event: TuiMouseEvent): { handled: boolean } {
		if (this.rows.length === 0) return { handled: false };
		if (event.type === "wheel") {
			this.selected =
				(this.selected + (event.wheelDelta && event.wheelDelta < 0 ? -1 : 1)
					+ this.rows.length) % this.rows.length;
		} else if (event.type === "press" || event.type === "click") {
			const hit = this.headerMap.find((h) => h.y === event.y);
			if (hit && hit.section === "global") {
				this.globalExpanded = !this.globalExpanded;
				this.rebuildRows();
				this.tui?.requestRender();
				return { handled: true };
			}
			const row = this.rowMap.find((r) => r.y === event.y);
			if (!row) return { handled: false };
			this.selected = row.index;
		} else return { handled: false };
		this.tui?.requestRender();
		return { handled: true };
	}

	private renderBody(width: number): string[] {
		const maxVisible = Math.min(this.rows.length, 12);
		const startIndex = Math.max(
			0,
			Math.min(this.selected - Math.floor(maxVisible / 2), this.rows.length - maxVisible),
		);
		const endIndex = Math.min(startIndex + maxVisible, this.rows.length);
		const lines: string[] = [];
		this.rowMap = [];
		this.headerMap = [];
		const now = Date.now() / 1000;
		// Section headers: this session's tickets always on top; the
		// global list starts collapsed (tab or clicking the header
		// toggles it).
		const header = (title: string, count: number, section: "session" | "global") => {
			const caret = section === "global" ? (this.globalExpanded ? "\u25bc" : "\u25b6") : "\u25cf";
			lines.push(truncateToWidth(
				"  " + this.st.label(`${caret} ${title} (${count})`, false), width));
			this.headerMap.push({ y: lines.length, section });
		};
		let globalHeaderDone = this.sessionRows.length === 0;
		if (this.sessionRows.length > 0) {
			header("Session", this.sessionRows.length, "session");
		}
		if (globalHeaderDone && this.globalRows.length > 0) {
			header("Other sessions", this.globalRows.length, "global");
		}
		for (let i = startIndex; i < endIndex; i++) {
			if (!globalHeaderDone && i >= this.sessionRows.length
				&& this.globalRows.length > 0) {
				header("Other sessions", this.globalRows.length, "global");
				globalHeaderDone = true;
			}
			const row = this.rows[i];
			const isSelected = i === this.selected;
			const prefix = isSelected ? this.st.cursor : "  ";
			const t = row.ticket;
			const when = new Date((t.started ?? t.created) * 1000)
				.toLocaleTimeString("en-GB");
			const label = `${t.id}  ${when}`;
			const value = `${t.status}${t.exit !== null ? ` exit ${t.exit}` : ""} - ${
				t.command.length > 40 ? t.command.slice(0, 37) + "..." : t.command}`;
			lines.push(truncateToWidth(
				prefix
					+ this.st.label(truncateToWidth(label, 16) + "  ", isSelected)
					+ this.st.value(value, isSelected),
				width));
			this.rowMap.push({ y: lines.length, index: i });
			if (this.expandedId === t.id) {
				// Expanded detail: full command and output tail, wrapped.
				for (const part of wrapLine(t.command, width - 4)) {
					lines.push(this.st.hint(`    ${part}`));
				}
				const output = this.outputs.get(t.id);
				if (output !== undefined) {
					const trunc = truncateTail(output, {
						maxLines: 14, maxBytes: 8 << 10,
					});
					for (const line of trunc.content.split("\n")) {
						for (const part of wrapLine(line, width - 4)) {
							lines.push(this.st.hint(`    ${part}`));
						}
					}
				} else {
					lines.push(this.st.hint("    loading output..."));
				}
			}
		}
		if (startIndex > 0 || endIndex < this.rows.length) {
			lines.push(this.st.hint(truncateToWidth(
				`  (${this.selected + 1}/${this.rows.length})`, width)));
		}
		return lines;
	}
}

/** The offloading bash backend: submits a command to the daemon, waits for
 *  it within the call bound, hands off past the bound (ticket keeps running
 *  daemon-side, delivery armed, agent freed), and always falls back to the
 *  local shell when the daemon was never in play. Owns its fallback backend
 *  and the wait-bound policy; state is per-call and never shared. */
class OffloadedBash implements BashOperations {
	constructor(
		private readonly localBash: BashOperations,
		private readonly tasks: DaemonTasks,
	) {}

	private offloadDisabled(): boolean {
		return process.env.PI_OFFLOAD === "off";
	}

	private waitBoundSeconds(): number {
		const raw = Number(process.env.PI_OFFLOAD_WAIT);
		return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_WAIT_SECONDS;
	}

	exec: BashOperations["exec"] = async (command, cwd, { onData, signal, timeout, env }) => {
		if (this.offloadDisabled()) {
			return this.localBash.exec(command, cwd, { onData, signal, timeout, env });
		}
		const sessionFile =
			typeof env?.PI_SESSION_FILE === "string" ? env.PI_SESSION_FILE : null;
		let id: string;
		try {
			id = await this.tasks.submit(
				sessionFile, cwd, command, sessionEnvExtra(env));
		} catch (exc) {
			if (!(exc instanceof DaemonUnavailable) || !exc.ambiguous) {
				// Daemon unreachable (contact never established) or a
				// deterministic refusal: the command never started in
				// the daemon, so local fallback is safe and exclusive.
				return this.localBash.exec(command, cwd, { onData, signal, timeout, env });
			}
			// Ambiguous: the ticket may already be running. Adopt a
			// matching recent ticket instead of re-running locally.
			const adopted = await adoptRecentShellTicket(
				this.tasks, sessionFile, command);
			if (adopted === null) {
				onData(Buffer.from(
					`[pi-daemon submit outcome unknown; NOT re-running ` +
					`locally to avoid duplication - check daemon_tasks ` +
					`list to locate the ticket]
`));
				return { exitCode: null };
			}
			id = adopted;
		}
		const deadline = Date.now() + (timeout ?? this.waitBoundSeconds()) * 1000;
		// One abort listener for the whole call: when the user aborts,
		// the race resolves null and the ticket is cancelled daemon-side.
		const aborted = new Promise<null>((resolve) => {
			if (!signal) return;
			if (signal.aborted) resolve(null);
			else signal.addEventListener("abort", () => resolve(null), { once: true });
		});
		let ticket: Ticket | null = null;
		while (ticket === null || ticket.status === "running") {
			const remaining = Math.max(1, Math.min(
				WAIT_CHUNK_SECONDS,
				(deadline - Date.now()) / 1000,
			));
			ticket = await Promise.race([
				this.tasks.client.wait(id, remaining),
				aborted.then(() => null),
			]);
			if (ticket === null) {
				// User abort: kill the daemon-side process too.
				await this.tasks.cancel(id).catch(() => {});
				throw new Error("aborted");
			}
			if (ticket.status === "running" && Date.now() >= deadline) {
				// Hand off: the ticket keeps running in the daemon,
				// the agent is freed now and notified on completion.
				let partial = "";
				try {
					partial = await this.tasks.client.outputAll(id);
				} catch {
					// partial output is best-effort
				}
				this.tasks.armDelivery(id, command);
				onData(Buffer.from(
					`${partial}
[pi-daemon ticket ${id} still running: ` +
					`continuing in the background; the full result will ` +
					`be delivered here when it finishes ` +
					`(daemon_tasks result ${id} fetches it sooner)]
`,
				));
				return { exitCode: null };
			}
		}
		// The inline wait consumed the result: bash itself delivers the
		// output as the tool result, so the delivery watcher armed by
		// submit() must not also steer it in — one command, one delivery,
		// by the parent that waited for it.
		this.tasks.markFetched(id);
		let output = "";
		try {
			output = await this.tasks.client.outputAll(id);
		} catch {
			// output fetch is best-effort; the exit code still stands
		}
		if (ticket.status === "lost") {
			output += `\n[pi-daemon ticket ${id} was interrupted ` +
				`(daemon restart); re-run if it is safe to repeat]`;
		}
		onData(Buffer.from(output));
		return { exitCode: ticket.exit };
	};
}
export default function (pi: ExtensionAPI) {
	const exec = (file: string, args: string[]) => pi.exec(file, args);
	const tasks = new DaemonTasks(new ProcessRunner(exec), (message, options) => {
		void pi.sendMessage(message, options);
	}, (customType, data) => {
		void pi.appendEntry(customType, data);
	});
	// Static one-line card; full detail lives in /daemon-tasks.
	pi.registerEntryRenderer("daemon-task", (entry, _opts, theme) => {
		const t = (entry.data as { ticket?: Ticket } | undefined)?.ticket;
		if (!t) return new Text("daemon task", 0, 0);
		const when = new Date((t.finished ?? t.created) * 1000)
			.toLocaleTimeString("en-GB");
		const head = `${t.id} ${t.status} - ${when}` +
			(t.exit !== null ? ` - exit ${t.exit}` : "");
		// Box pads the line to full width, so the bg spans the card.
		const box = new Box(0, 0, (text) =>
			theme.bg("customMessageBg", text));
		box.addChild(new Text(theme.bold(`[daemon-task] ${head}`)));
		return box;
	});
	const localBash: BashOperations = createLocalBashOperations();

	const disabled = () => process.env.PI_OFFLOAD === "off";

	// -- transparent bash offloading -------------------------------------
	// The offload backend lives in the OffloadedBash class (module level);
	// this bootstrap composes it with the local fallback and the daemon.
	const backend = new OffloadedBash(localBash, tasks);


	// bash tool override: same schema, renderers, prompt snippet, and
	// result shape as the built-in; only the execution backend changes,
	// plus one guideline telling agents about the background hand-off.
	const bashTool = createBashTool(process.cwd(), { operations: backend });
	Object.assign(bashTool, {
		promptGuidelines: [
			"You can inspect PI_* environment variables for current model and session details.",
			"Every command runs as a daemon ticket: results arrive complete in " +
				"one go, and commands outliving the wait bound keep running in the " +
				"background (a note names the ticket id) with the full result " +
				"delivered automatically on completion.",
		],
	});
	pi.registerTool(bashTool);

	// -- explicit task management tool -----------------------------------

	pi.registerTool({
		name: "daemon_tasks",
		label: "daemon tasks",
		description:
			"Manage commands offloaded to the pi-daemon as tickets. " +
			"submit runs a command in the background and returns a ticket id " +
			"immediately (the result is delivered automatically when done); " +
			"result fetches a finished ticket's full output in one go; watch " +
			"streams a running ticket's output; status, list and cancel do " +
			"what they say. Requires the pi-daemon service.",
		promptSnippet: "Run long shell commands as background daemon tickets",
		promptGuidelines: [
			"Prefer daemon_tasks submit for builds, test suites, downloads and " +
				"other long-running commands: you keep working immediately and " +
				"the full result is delivered to you when the task finishes. " +
				"Use result to fetch a ticket's output, watch to follow it live.",
		],
		parameters: Type.Object({
			action: StringEnum(["submit", "status", "result", "watch", "cancel", "remove", "reset", "list"] as const),
			command: Type.Optional(Type.String({ description: "Shell command (submit)" })),
			cwd: Type.Optional(Type.String({ description: "Working directory (submit; default session cwd)" })),
			id: Type.Optional(Type.String({ description: "Ticket id (status/result/watch/cancel)" })),
			wait: Type.Optional(Type.Number({ description: "Seconds to wait for result (result; default 0)" })),
		}),
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			if (disabled()) {
				throw new Error("Command offloading is disabled (PI_OFFLOAD=off)");
			}
			const sessionFile = ctx?.sessionManager?.getSessionFile?.() ?? null;
			switch (params.action) {
				case "submit": {
					if (!params.command) {
						throw new Error("submit needs a command");
					}
					const id = await tasks.submit(
						sessionFile,
						params.cwd || ctx?.cwd || process.cwd(),
						params.command,
					);
					return {
						content: [{
							type: "text",
							text: `ticket ${id} queued: ${params.command}\n` +
								`The result will be delivered automatically when it finishes; ` +
								`daemon_tasks status/result ${id} checks or fetches it sooner.`,
						}],
						details: { ticketId: id },
					};
				}
				case "status": {
					if (!params.id) throw new Error("status needs a ticket id");
					const ticket = await tasks.status(params.id);
					return {
						content: [{ type: "text", text: formatResult(ticket, "") }],
						details: { ticket },
					};
				}
				case "result": {
					if (!params.id) throw new Error("result needs a ticket id");
					const blocking = (params.wait ?? 0) > 0;
					if (blocking) tasks.setSessionState("waiting");
					const { ticket, output } = await tasks.result(
						params.id, params.wait ?? 0);
					if (blocking) tasks.setSessionState("busy");
					if (ticket.status === "running") {
						return {
							content: [{
								type: "text",
								text: `ticket ${ticket.id} still running: ${ticket.command}\n` +
									`Use wait to block for it, watch to follow its output, ` +
									`or continue and the result will be delivered when done.`,
							}],
							details: { ticket },
						};
					}
					return {
						content: [{ type: "text", text: formatResult(ticket, output) }],
						details: { ticket },
					};
				}
				case "watch": {
					if (!params.id) throw new Error("watch needs a ticket id");
					let lastText = "";
					tasks.setSessionState("waiting");
					let watched: { ticket: Ticket; output: string };
					try {
						watched = await tasks.watch(params.id, (tail) => {
							const text = formatResult({ ...ticket, status: "running" }, tail);
							if (text !== lastText) {
								lastText = text;
								onUpdate?.({
									content: [{ type: "text", text }],
									details: { ticketId: params.id, running: true },
								});
							}
						});
					} finally {
						tasks.setSessionState("busy");
					}
					const { ticket, output } = watched;
					return {
						content: [{ type: "text", text: formatResult(ticket, output) }],
						details: { ticket },
					};
				}
				case "cancel": {
					if (!params.id) throw new Error("cancel needs a ticket id");
					const ticket = await tasks.cancel(params.id);
					return {
						content: [{
							type: "text",
							text: `ticket ${ticket.id} ${ticket.status}` +
								(ticket.error ? ` (${ticket.error})` : ""),
						}],
						details: { ticket },
					};
				}
			case "remove": {
					if (!params.id) throw new Error("remove needs a ticket id");
					await tasks.client.remove(params.id);
					return {
						content: [{ type: "text", text: `ticket ${params.id} removed` }],
						details: undefined,
					};
				}
				case "reset": {
					const { cancelled, removed } = await tasks.client.resetAll();
					return {
						content: [{
							type: "text",
							text: `ticketing reset: ${cancelled} running ticket(s) cancelled, ` +
								`${removed} record(s) wiped; the id counter restarted at t-1`,
						}],
						details: undefined,
					};
				}
				case "list": {
					const tickets = await tasks.list(sessionFile);
					if (!tickets.length) {
						return {
							content: [{ type: "text", text: "no tickets for this session" }],
							details: undefined,
						};
					}
					const lines = tickets.map((t) =>
						`${t.id}\t${t.status}\t${t.command.length > 80 ? t.command.slice(0, 77) + "..." : t.command}`);
					return {
						content: [{ type: "text", text: lines.join("\n") }],
						details: { tickets },
					};
				}
			}
			throw new Error(`unknown action: ${String(params.action)}`);
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		await tasks.rearm(ctx?.sessionManager?.getSessionFile?.() ?? null);
	});
	pi.on("session_shutdown", async () => {
		tasks.stopWatching();
	});

	// User-facing dock: see every daemon ticket, cancel running ones,
	// remove finished ones. Cancelling notifies the owning session's own
	// watcher (a follow-up message) whenever that session still exists.
	pi.registerCommand("daemon-tasks", {
		description:
			"Browse daemon tickets (all sessions); cancel running, remove finished",
		handler: async (_args, cmdCtx) => {
			if (cmdCtx.mode !== "tui") {
				cmdCtx.ui?.notify?.("daemon-tasks requires the interactive TUI", "warning");
				return;
			}
			// Identity comes from the command context, not the shell-only
			// PI_SESSION_FILE env: a non-hosted session has no env key, so
			// the dock would otherwise look for its tickets under
			// "standalone" while the tool listed them under the file stem.
			await new DaemonTasksDock(
				tasks.client,
				sessionKeyOf(cmdCtx.sessionManager?.getSessionFile?.()),
			).run(cmdCtx.ui);
		},
	});

}
