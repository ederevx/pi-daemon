/**
 * Command offloading for Pi — shell commands become daemon-owned tickets.
 *
 * The extension overrides the built-in `bash` tool with a variant whose
 * execution backend submits the command to pi-daemon as a ticket and
 * returns a successful claim at once: the daemon always owns execution,
 * no task holds the agent, and the call never returns the stock tool's
 * null-exit hand-off error. The submit-armed delivery watcher waits on
 * the daemon-owned ticket and steers the full result in before the next
 * model call, so the agent never has to bridge the deferral itself.
 * Output is delivered in ONE GO when the command finishes (nothing is
 * streamed into the tool result while it runs), so agents read a
 * complete result exactly once. Two departures from the stock backend,
 * both deliberate:
 *
 * - The daemon owns the process from the first instant; a bash `timeout`
 *   supplied to the tool is forwarded to the daemon, which enforces it
 *   daemon-side, so a timed-out ticket is reported failed when its
 *   deferred result arrives.
 * - If the daemon is unreachable, execution falls back to pi's local
 *   shell backend transparently — the agent only ever sees normal bash
 *   behavior. PI_OFFLOAD=off (or `piDaemon.offload.enabled=false`)
 *   disables offloading entirely, and the shell then runs locally with no
 *   onboarding prerequisite.
 *
 * With offloading enabled the extension's bash tool is gated exactly like
 * the daemon tools: until `pre_daemon` has been called in the session, the
 * call is blocked instead of being submitted to the daemon or run locally.
 * That gate belongs to the pre-daemon extension, which asks this module's
 * OffloadPolicy whether offloading is on.
 *
 * Tickets survive everything else: they outlive the submitting session
 * (daemon-owned execution), are persisted in the daemon's tickets.json,
 * and a session that crashes or restarts re-arms watchers on
 * session_start so pending results still get delivered. The `daemon_tasks`
 * tool exposes the same machinery explicitly: background submission,
 * status, one-go result fetch, an active result wait that returns as
 * soon as the ticket finishes (interruptible, steerable, live elapsed
 * status), live watch (incremental output via partial updates),
 * cancel, and per-session listing.
 */

import type {
	AgentToolUpdateCallback,
	ExtensionAPI,
	ExecOptions,
} from "@earendil-works/pi-coding-agent";
import {
	createBashTool,
	createLocalBashOperations,
	truncateTail,
	DEFAULT_MAX_BYTES,
	DEFAULT_MAX_LINES,
	DynamicBorder,
	getSettingsListTheme,
	getShellConfig,
	keyHint,
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
import { OffloadPolicy } from "./settings/offload-policy.ts";
import { TicketFeed, type StreamDetail } from "./tickets/ticket-feed.ts";
import { TicketGroupWait, type GroupOutcome, WaitCancelled } from "./tickets/group-wait.ts";
import { outcomeColor, TicketStreamView } from "./tickets/stream-view.ts";

/** pi-rc exit codes: 4 = unreachable before any request landed (safe to
 *  run the task locally); 7 = the request was sent but the outcome is
 *  unknown — the daemon may have acted on it, so callers must adopt any
 *  matching ticket instead of re-running. */
const EXIT_NO_DAEMON = 4;
const EXIT_AMBIGUOUS = 7;

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
	timeout?: number | null;
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
export function formatResult(ticket: Ticket, output: string): string {
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

/** The most tickets one call may block on: each waits on its own stream
 *  and the view shares a fixed line budget between them. */
const MAX_TICKET_IDS = 8;

/** The canonical ids for one call: `ids` when non-empty, else `[id]`,
 *  deduped in order and capped so the per-tick fan-out stays bounded. */
function resolveTicketIds(params: { id?: string; ids?: string[] }): string[] {
	const raw = params.ids && params.ids.length > 0
		? params.ids
		: (params.id ? [params.id] : []);
	const seen = new Set<string>();
	const ids: string[] = [];
	for (const id of raw) {
		if (seen.has(id)) continue;
		seen.add(id);
		ids.push(id);
	}
	if (ids.length === 0) throw new Error("a ticket id or ids is required");
	if (ids.length > MAX_TICKET_IDS) {
		throw new Error(`at most ${MAX_TICKET_IDS} ticket ids per call`);
	}
	return ids;
}

/** The single-ticket release text, byte-for-byte unchanged. */
function stillRunningText(ticket: Ticket): string {
	return `ticket ${ticket.id} still running: ${ticket.command}\n` +
		`The result will still be delivered automatically when it finishes, or ` +
		`re-call daemon_tasks result ${ticket.id} with wait to block again ` +
		`(the wait is interruptible and yields early when a message is queued).`;
}

/** The multi-ticket release summary for the ids still running. */
function stillRunningMulti(
	ids: readonly string[],
	records: Map<string, Ticket>,
): string {
	const names = ids.map((id) => {
		const ticket = records.get(id)!;
		return `${ticket.id} (${ticket.command})`;
	}).join(", ");
	return `Still running: ${names}\n` +
		`Their results will still be delivered automatically when they finish, ` +
		`or re-call daemon_tasks result with ids ${ids.join(",")} and wait to ` +
		`block again (the wait is interruptible and yields early when a message ` +
		`is queued).`;
}

/** A completed ticket whose output fetch failed: delivery is re-armed. */
function fetchFailedText(ticket: Ticket, error: unknown): string {
	const reason = error instanceof Error ? error.message : String(error);
	return `ticket ${ticket.id} ${ticket.status}: output fetch failed ` +
		`(${reason}); its result will still be delivered automatically.`;
}

/** A live feed whose frames become the tool's partial update. */
function streamFeed(
	ids: readonly string[],
	onUpdate: AgentToolUpdateCallback<any> | undefined,
): TicketFeed {
	return new TicketFeed(ids, (frame) => {
		onUpdate?.({
			content: [{ type: "text", text: frame.text }],
			details: { streams: frame.streams },
		});
	});
}

/** Runs a blocking wait: the group loop streams the feed and stops on
 *  mode, the bound, the signal, or a queued message; the feed closes in
 *  every case, including an error. */
async function runBlockingWait(
	tasks: DaemonTasks,
	ids: readonly string[],
	options: {
		mode: "first" | "all";
		bound: number;
		signal?: AbortSignal;
		yieldNow: () => boolean;
		feed: TicketFeed;
	},
): Promise<GroupOutcome> {
	try {
		return await new TicketGroupWait(tasks.client).run({
			ids,
			mode: options.mode,
			bound: options.bound,
			signal: options.signal,
			shouldYield: options.yieldNow,
			feed: options.feed,
			onObserved: (ticket) => tasks.consume(ticket.id),
		});
	} finally {
		options.feed.close();
	}
}

/** The `result` wait=0 path: one probe per id, no feed, no blocking. */
async function probeAll(
	tasks: DaemonTasks,
	ids: readonly string[],
): Promise<GroupOutcome> {
	const records = new Map<string, Ticket>();
	for (const id of ids) records.set(id, await tasks.probe(id));
	return {
		records,
		completed: ids.filter((id) => records.get(id)!.status !== "running"),
		running: ids.filter((id) => records.get(id)!.status === "running"),
		streams: new Map(),
	};
}

/** The result-action release: every completed ticket's full output, then
 *  the still-running summary. A single id keeps the legacy wording; a
 *  failed fetch re-arms delivery instead of losing the result. */
async function resultRelease(
	tasks: DaemonTasks,
	ids: readonly string[],
	outcome: GroupOutcome,
): Promise<{ content: Array<{ type: string; text: string }>; details: unknown }> {
	const blocks: string[] = [];
	for (const id of outcome.completed) {
		const ticket = outcome.records.get(id)!;
		try {
			blocks.push(formatResult(ticket, await tasks.client.outputAll(id)));
		} catch (error) {
			tasks.unconsume(id, ticket.command);
			if (ids.length === 1) throw error;
			blocks.push(fetchFailedText(ticket, error));
		}
	}
	if (outcome.running.length > 0) {
		blocks.push(ids.length === 1
			? stillRunningText(outcome.records.get(ids[0])!)
			: stillRunningMulti(outcome.running, outcome.records));
	}
	return {
		content: [{ type: "text", text: blocks.join("\n") }],
		details: ids.length === 1
			? { ticket: outcome.records.get(ids[0]) }
			: { tickets: ids.map((id) => outcome.records.get(id)) },
	};
}

/** The watch release: completed tickets from `outputAll`, still-running
 *  ones from the stream tail, plus the early-release note. */
async function watchRelease(
	tasks: DaemonTasks,
	ids: readonly string[],
	outcome: GroupOutcome,
): Promise<{ content: Array<{ type: string; text: string }>; details: unknown }> {
	const blocks: string[] = [];
	for (const id of outcome.completed) {
		const ticket = outcome.records.get(id)!;
		try {
			blocks.push(formatResult(ticket, await tasks.client.outputAll(id)));
		} catch (error) {
			tasks.unconsume(id, ticket.command);
			if (ids.length === 1) throw error;
			blocks.push(fetchFailedText(ticket, error));
		}
	}
	for (const id of outcome.running) {
		const ticket = outcome.records.get(id)!;
		blocks.push(formatResult(ticket, outcome.streams.get(id)?.tail() ?? ""));
	}
	let text = blocks.join("\n");
	if (outcome.running.length > 0) {
		text += "\n[watch released early; the ticket keeps running and " +
			"its result will still be delivered when it finishes]";
	}
	return {
		content: [{ type: "text", text }],
		details: ids.length === 1
			? { ticket: outcome.records.get(ids[0]) }
			: { tickets: ids.map((id) => outcome.records.get(id)) },
	};
}

/** One daemon-ticket completion as pi renders it: pi's custom-message
 *  background, collapsed by default to a single line colored by the
 *  ticket's outcome, and showing the delivered result once the user
 *  expands it (ctrl+o). The replayed key hint comes from pi, so a
 *  remapped expand key still reads correctly. */
class TicketNotification {
	constructor(
		private readonly ticket: Ticket,
		private readonly content: string,
		private readonly theme: {
			fg: (role: string, text: string) => string;
			bg: (role: string, text: string) => string;
			bold: (text: string) => string;
		},
	) {}

	/** The outcome color, shared with the live stream view. */
	private outcomeColor(): string {
		return outcomeColor(this.ticket.status, this.ticket.exit);
	}

	/** The one-line summary: label, ticket, outcome-colored status and
	 *  exit code, finish time, and the expand/collapse hint. */
	private headline(expanded: boolean): string {
		const t = this.ticket;
		const when = new Date((t.finished ?? t.created) * 1000)
			.toLocaleTimeString("en-GB");
		const outcome = t.exit !== null
			? `${t.status} (exit ${t.exit})`
			: t.status;
		const hint = keyHint("app.tools.expand",
			expanded ? "to collapse" : "to expand");
		return [
			this.theme.fg("customMessageLabel", this.theme.bold("[daemon-task]")),
			this.theme.fg("accent", t.id),
			this.theme.fg("muted", "-"),
			this.theme.fg(this.outcomeColor(), outcome),
			this.theme.fg("dim", when),
			`${this.theme.fg("muted", "(")}${hint}${this.theme.fg("muted", ")")}`,
		].join(" ");
	}

	/** The card body: the headline, then the full result the agent
	 *  received once expanded. The host owns transcript spacing. */
	component(expanded: boolean, outputPad: number): Component {
		const box = new Box(outputPad, 1, (text) =>
			this.theme.bg("customMessageBg", text));
		box.addChild(new Text(this.headline(expanded), 0, 0));
		if (expanded) {
			box.addChild(new Text(this.theme.fg("dim", `\n${this.content}`), 0, 0));
		}
		return box;
	}
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
	 *  unreachable daemon (exit 4) or a failed spawn. The daemon caps a
	 *  ticket-wait round trip itself (TICKET_MAX_WAIT), so no
	 *  client-side timeout is layered on top. */
	private async run(args: string[], options?: ExecOptions): Promise<string> {
		let result: Awaited<ReturnType<ProcessRunner["run"]>>;
		try {
			result = await this.runner.run(this.piRc, args, options);
		} catch (exc) {
			// The caller's own cancellation is not a daemon failure: the
			// bound or the abort ended the request, so it surfaces as a
			// cancellation the wait loop understands.
			if (options?.signal?.aborted) throw new WaitCancelled();
			throw new DaemonUnavailable(`pi-rc failed: ${String(exc)}`);
		}
		if (options?.signal?.aborted) throw new WaitCancelled();
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
		timeout?: number,
	): Promise<string> {
		const args = ["ticket-submit", "--session", session, "--cwd", cwd];
		for (const [key, value] of Object.entries(extraEnv)) {
			args.push("--env", `${key}=${value}`);
		}
		if (typeof timeout === "number" && Number.isFinite(timeout) && timeout > 0) {
			args.push("--timeout", String(timeout));
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
	async wait(
		id: string,
		timeoutSeconds: number,
		signal?: AbortSignal,
	): Promise<Ticket> {
		const args = ["ticket-wait", id];
		if (timeoutSeconds > 0) args.push(String(timeoutSeconds));
		const out = await this.run(args, signal ? { signal } : undefined);
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

	/** Reads one bounded chunk of the output log from `offset`; returns
	 *  the raw bytes and the offset to resume from. The offset reply is
	 *  base64 on the wire (pi-rc prints it verbatim), so binary output
	 *  survives the UTF-8 exec channel byte-faithfully and client
	 *  offsets stay true byte offsets. An empty chunk means the log
	 *  grew by nothing, so an incremental reader can poll for new bytes
	 *  without ever re-reading what it already has. */
	async outputFrom(
		id: string,
		offset: number,
		signal?: AbortSignal,
	): Promise<{ data: Buffer; next: number }> {
		const out = await this.run(
			["ticket-output", id, String(offset)],
			signal ? { signal } : undefined);
		const data = Buffer.from(out.trim(), "base64");
		return { data, next: offset + data.length };
	}

	/** Reads the whole output log in bounded chunks. */
	async outputAll(id: string, signal?: AbortSignal): Promise<string> {
		const parts: Buffer[] = [];
		let offset = 0;
		for (let i = 0; i < 256; i++) {
			const { data, next } = await this.outputFrom(id, offset, signal);
			if (data.length === 0) break;
			parts.push(data);
			offset = next;
		}
		return Buffer.concat(parts).toString("utf8");
	}

	/** Drops a finished ticket (and its artifacts) from the store. */
	async remove(id: string): Promise<string> {
		await this.run(["ticket-remove", id]);
		return id;
	}

	async list(session?: string, signal?: AbortSignal): Promise<Ticket[]> {
		const args = ["ticket-list"];
		if (session) args.push(session);
		const out = await this.run(args, signal ? { signal } : undefined);
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

	/** ticket id -> live watcher; owning object mutates this only. */
	private watchers = new Map<string, { stopped: boolean }>();

	/** Pi's shell, resolved once; the daemon runs the ticket in it. */
	private readonly shell = piShell();

	/** Tickets whose result was already fetched by an explicit call. */
	private fetched = new Set<string>();

	constructor(
		runner: ProcessRunner,
		send: DaemonTasks["send"],
	) {
		this.client = new TicketClient(runner);
		this.send = send;
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
		timeout?: number,
	): Promise<string> {
		const env = this.shell
			? { ...extraEnv, PI_SHELL: this.shell }
			: extraEnv;
		const id = await this.client.submit(
			this.sessionKey(sessionFile), cwd, command, env, timeout);
		this.armDelivery(id, command);
		return id;
	}

	status(id: string): Promise<Ticket> {
		return this.client.wait(id, 0);
	}

	/** One non-blocking status probe for `result` with wait=0. A ticket
	 *  already complete is marked consumed here so its armed delivery
	 *  watcher never steers a duplicate. */
	async probe(id: string): Promise<Ticket> {
		const ticket = await this.client.wait(id, 0);
		if (ticket.status !== "running") this.fetched.add(id);
		return ticket;
	}

	/** Marks an observed-complete ticket consumed so its armed delivery
	 *  watcher stays silent. */
	consume(id: string): void {
		this.fetched.add(id);
	}

	/** Undoes a consume whose output fetch failed: drop the marker and
	 *  make sure a watcher delivers. armDelivery dedups on a live watcher,
	 *  so an in-flight one just sees the cleared marker, while a finished
	 *  one is replaced and delivers the persisted result. */
	unconsume(id: string, command: string): void {
		this.fetched.delete(id);
		this.armDelivery(id, command);
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
				// which let agents duplicate work. The message is visible:
				// its renderer draws a card collapsed to one outcome-
				// colored line, expanding to the full result the agent
				// receives.
				this.send(
					{
						customType: "daemon-task",
						content: `Background task finished: ${command}\n${formatResult(ticket, output)}`,
						display: true,
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

	/** session_shutdown: stop in-process watchers and drop the consumed
	 *  markers; the tickets keep running daemon-side and a later
	 *  session_start re-arms. The markers only exist to silence live
	 *  delivery watchers, so keeping them across a shutdown would let
	 *  the set grow without bound over a long-lived session. */
	stopWatching(): void {
		for (const watcher of this.watchers.values()) {
			watcher.stopped = true;
		}
		this.watchers.clear();
		this.fetched.clear();
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

/** Pi's own shell, or empty when it cannot be resolved. An offloaded
 *  command must run in the same shell the local bash tool would, so the
 *  owner resolves this once and sends it to the daemon as PI_SHELL. */
function piShell(): string {
	try {
		return getShellConfig().shell;
	} catch {
		return "";
	}
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

/** Refuses a bash call whose point is to wait. A handed-off command is
 *  already daemon-owned and its result is steered in automatically, so a
 *  blocking wait belongs to `daemon_tasks result ... wait`, never to a
 *  `sleep`. Only command positions count, so a `sleep` word in an
 *  argument (`grep sleep file`) is not a wait and stays allowed. */
class SleepGuard {
	/** Words that still leave `sleep` the first real command word. */
	private static readonly LAUNCHERS = new Set([
		"sudo", "nohup", "time", "env", "command", "exec", "timeout",
	]);

	/** True when any command in the compound line is a sleep call. */
	blocks(command: string): boolean {
		return command
			.split(/\n|[;&|()]+/)
			.some((segment) => this.sleeps(segment.trim().split(/\s+/)));
	}

	/** True when the segment's first real word is `sleep`, skipping
	 *  leading assignments, options, durations, shell keywords, and
	 *  launchers. */
	private sleeps(words: string[]): boolean {
		for (const word of words) {
			if (!word) continue;
			if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(word)) continue;
			if (/^-/.test(word)) continue;
			if (/^[0-9.]+[smhd]?$/.test(word)) continue;
			if (word === "do" || word === "then" || word === "else") continue;
			if (SleepGuard.LAUNCHERS.has(word)) continue;
			return word === "sleep";
		}
		return false;
	}
}

/** The shared guard: stateless, so one instance serves every call. */
const SLEEP_GUARD = new SleepGuard();

/** The offloading bash backend: submits every command to the daemon and
 *  returns a successful claim at once, so the daemon always owns
 *  execution and no task holds the agent. The submit-armed delivery
 *  watcher waits on the ticket and reports the full result as one steer;
 *  a bash timeout travels with the ticket and is enforced daemon-side.
 *  Falls back to the local shell only when the daemon was never in play.
 *  Owns its fallback backend and its per-call state. */
class OffloadedBash implements BashOperations {
	constructor(
		private readonly localBash: BashOperations,
		private readonly tasks: DaemonTasks,
		private readonly policy: Pick<OffloadPolicy, "enabled">,
	) {}

	private offloadDisabled(): boolean {
		return !this.policy.enabled();
	}

	exec: BashOperations["exec"] = async (command, cwd, { onData, signal, timeout, env }) => {
		if (this.offloadDisabled()) {
			return this.localBash.exec(command, cwd, { onData, signal, timeout, env });
		}
		if (SLEEP_GUARD.blocks(command)) {
			// A waiting command is a hand-off the agent is trying to bridge
			// itself. Refuse it before the daemon sees it and name the two
			// real continuations: keep calling tools, or block with the
			// task tool.
			onData(Buffer.from(
				`[pi-daemon] refused \`sleep\`: a handed-off command is already ` +
				`daemon-owned, so its result is steered in automatically - keep ` +
				`calling tools instead. To block for a ticket, call ` +
				`daemon_tasks result <id> wait=<seconds> (daemon_tasks list ` +
				`finds the id).\n`));
			return { exitCode: 1 };
		}
		const sessionFile =
			typeof env?.PI_SESSION_FILE === "string" ? env.PI_SESSION_FILE : null;
		let id: string;
		try {
			id = await this.tasks.submit(
				sessionFile, cwd, command, sessionEnvExtra(env), timeout);
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
					`list to locate the ticket]\n`));
				return { exitCode: 1 };
			}
			id = adopted;
		}
		// Handed off: the ticket is daemon-owned from here. The delivery
		// watcher armed by submit() waits on it and steers the full result
		// in before the next model call, so the agent never bridges the
		// deferral itself. The claim is a success - never the stock tool's
		// null exit code, which renders as both a failure and a hand-off
		// the agent could respond to. The claim also advertises the ticket
		// and its two continuations, so an agent that reaches for a sleep
		// instead learns it may simply keep calling tools (the result is
		// steered in) or block explicitly with daemon_tasks result.
		onData(Buffer.from(
			`[pi-daemon] ticket ${id} taken by the daemon; the result is ` +
			`steered in automatically.\n` +
			`Block with daemon_tasks result ${id} wait=<seconds>, or keep ` +
			`calling commands.\n`));
		return { exitCode: 0 };
	};
}
export default function (pi: ExtensionAPI) {
	const exec = (
		file: string,
		args: string[],
		options?: ExecOptions,
	) => pi.exec(file, args, options);
	const tasks = new DaemonTasks(new ProcessRunner(exec), (message, options) => {
		void pi.sendMessage(message, options);
	});
	const streamView = new TicketStreamView();
	// Completion card: pi's custom-message shell, one line collapsed
	// (outcome-colored) and the full result when the user expands it.
	pi.registerMessageRenderer("daemon-task", (message, { expanded, outputPad }, theme) => {
		const ticket = (message.details as { ticket?: Ticket } | undefined)?.ticket;
		if (!ticket) return undefined;
		const content = typeof message.content === "string" ? message.content : "";
		return new TicketNotification(ticket, content, theme)
			.component(expanded, outputPad);
	});
	const localBash: BashOperations = createLocalBashOperations();
	const policy = new OffloadPolicy();

	const disabled = () => !policy.enabled();

	// -- bash offloading -------------------------------------------------
	// The offload backend lives in the OffloadedBash class (module level);
	// this bootstrap composes it with the local fallback, the daemon, and
	// the same offload policy the onboarding gate consults.
	const backend = new OffloadedBash(localBash, tasks, policy);


	// bash tool override: same schema, renderers, prompt snippet, and
	// result shape as the built-in; only the execution backend changes,
	// plus one guideline telling agents about the background hand-off.
	const bashTool = createBashTool(process.cwd(), { operations: backend });
	Object.assign(bashTool, {
		promptGuidelines: [
			"You can inspect PI_* environment variables for current model and session details.",
			"When offloading is enabled, call pre_daemon once per session before " +
				"using bash.",
			"When offloading is enabled, every bash call is handed to pi-daemon as a " +
				"ticket whose result is steered in automatically, so keep calling tools " +
				"instead of sleeping or polling - a `sleep` call is refused; to block for " +
				"a ticket before the next step, call daemon_tasks result <id> " +
				"wait=<seconds>.",
		],
	});
	pi.registerTool(bashTool);

	// -- explicit task management tool -----------------------------------

	pi.registerTool({
		name: "daemon_tasks",
		label: "daemon tasks",
		description:
			"Manage pi-daemon tickets (submit, status, result, watch, " +
			"cancel, remove, list). Call pre_daemon for the " +
			"catalog and conventions.",
		promptGuidelines: [
			"Prefer daemon_tasks submit for builds, test suites, downloads " +
				"and other long-running commands: you keep working " +
				"immediately and the full result is delivered to you when " +
				"the task finishes. A blocking result wait is interruptible " +
				"and steerable; the ticket keeps running and its result is " +
				"still delivered on completion.",
			"A command whose point is to wait - a `sleep` or a polling " +
				"loop - is refused for submit too: submit real work and let " +
				"its result steer in, or block with `daemon_tasks result " +
				"<id> wait=<seconds>`.",
		],
		parameters: Type.Object({
			action: StringEnum(["submit", "status", "result", "watch", "cancel", "remove", "list"] as const),
			command: Type.Optional(Type.String({ description: "Shell command (submit)" })),
			cwd: Type.Optional(Type.String({ description: "Working directory (submit; default session cwd)" })),
			timeout: Type.Optional(Type.Number({ description: "Daemon-side timeout in seconds (submit)" })),
			id: Type.Optional(Type.String({ description: "Ticket id (status/result/watch/cancel)" })),
			ids: Type.Optional(Type.Array(Type.String(), {
				maxItems: 8,
				description: "Ticket ids for a multi-ticket result/watch (up to 8; ids wins over id)",
			})),
			wait: Type.Optional(Type.Number({ description: "Seconds to actively wait for result (result; default 0)" })),
		}),
		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			if (disabled()) {
				throw new Error("Command offloading is disabled (PI_OFFLOAD=off or piDaemon.offload.enabled=false)");
			}
			const sessionFile = ctx?.sessionManager?.getSessionFile?.() ?? null;
			switch (params.action) {
				case "submit": {
					if (!params.command) {
						throw new Error("submit needs a command");
					}
					if (SLEEP_GUARD.blocks(params.command)) {
						throw new Error(
							"[pi-daemon] refused `sleep` in daemon_tasks submit: " +
							"a ticket whose point is to wait has no work for the " +
							"daemon to own - submit real work and let its result " +
							"steer in, or block with daemon_tasks result <id> " +
							"wait=<seconds>.",
						);
					}
					const id = await tasks.submit(
						sessionFile,
						params.cwd || ctx?.cwd || process.cwd(),
						params.command,
						{},
						params.timeout,
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
					const ids = resolveTicketIds(params);
					const blocking = (params.wait ?? 0) > 0;
					if (blocking) tasks.setSessionState("waiting");
					try {
						const outcome = blocking
							? await runBlockingWait(tasks, ids, {
								mode: "first",
								bound: params.wait ?? 0,
								signal,
								yieldNow: () =>
									Boolean(ctx?.hasPendingMessages?.()),
								feed: streamFeed(ids, onUpdate),
							})
							: await probeAll(tasks, ids);
						return await resultRelease(tasks, ids, outcome);
					} finally {
						if (blocking) tasks.setSessionState("busy");
					}
				}
				case "watch": {
					const ids = resolveTicketIds(params);
					tasks.setSessionState("waiting");
					try {
						const outcome = await runBlockingWait(tasks, ids, {
							mode: "all",
							bound: Infinity,
							signal,
							yieldNow: () =>
								Boolean(ctx?.hasPendingMessages?.()),
							feed: streamFeed(ids, onUpdate),
						});
						return await watchRelease(tasks, ids, outcome);
					} finally {
						tasks.setSessionState("busy");
					}
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
		renderResult: (result, options, theme, context) =>
			streamView.render(result, options, theme, context),
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
