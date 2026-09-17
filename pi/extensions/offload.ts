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
	type BashOperations,
} from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/** pi-rc exit code when the control socket is unreachable. */
const EXIT_NO_DAEMON = 4;

/** Default hand-off bound for bash offloading, in seconds. */
const DEFAULT_WAIT_SECONDS = 120;

/** Longest single ticket-wait round trip, in seconds. */
const WAIT_CHUNK_SECONDS = 300;

/** One ticket record as the daemon stores and returns it. */
interface Ticket {
	id: string;
	session: string;
	cwd: string;
	command: string;
	status: "running" | "done" | "failed" | "cancelled" | "lost";
	created: number;
	started: number;
	finished: number | null;
	exit: number | null;
	term: number | null;
	truncated: boolean;
	error: string | null;
}

/** Error thrown when the daemon cannot be reached or misbehaves. */
class DaemonUnavailable extends Error {}

/** Formats one completed ticket + its output as agent-facing text. */
function formatResult(ticket: Ticket, output: string): string {
	const header = [
		`ticket ${ticket.id} ${ticket.status}`,
		ticket.exit !== null ? `exit ${ticket.exit}` : null,
		ticket.error ? `(${ticket.error})` : null,
	]
		.filter(Boolean)
		.join(" ");
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

/** The pi-rc binary to shell out to: a repo-checkout sibling wins (so a
 *  working tree validates against its own client), then the installed
 *  copy the installer manages. */
function resolvePiRc(): string {
	const here = path.dirname(fileURLToPath(import.meta.url));
	const candidates = [
		path.join(here, "..", "bin", "pi-rc"),
		`${process.env.HOME || "."}/.local/bin/pi-rc`,
	];
	for (const candidate of candidates) {
		try {
			fs.accessSync(candidate, fs.constants.X_OK);
			return candidate;
		} catch {
			// try the next candidate
		}
	}
	return candidates[candidates.length - 1];
}

/** The pi-rc subprocess surface: one method per client command. */
class TicketClient {
	private readonly exec: (
		file: string,
		args: string[],
	) => Promise<{ code: number; stdout: string; stderr: string; killed: boolean }>;

	private readonly piRc: string;

	constructor(
		exec: TicketClient["exec"],
	) {
		this.exec = exec;
		this.piRc = resolvePiRc();
	}

	/** Runs pi-rc and returns its stdout; throws DaemonUnavailable on an
	 *  unreachable daemon (exit 4) or a failed spawn. */
	private async run(args: string[], timeoutSeconds?: number): Promise<string> {
		let result: Awaited<ReturnType<TicketClient["exec"]>>;
		try {
			result = await this.exec(this.piRc, args);
		} catch (exc) {
			throw new DaemonUnavailable(`pi-rc failed: ${String(exc)}`);
		}
		if (result.code === EXIT_NO_DAEMON) {
			throw new DaemonUnavailable("background service is not running");
		}
		if (result.code !== 0) {
			const detail = (result.stderr || result.stdout || "").trim();
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
		await this.run(["ticket-cancel", id]);
		return this.wait(id, 0);
	}
}

/**
 * Session-side task tracking: the watcher registry. Watchers poll a
 * ticket to completion and deliver its full output as one follow-up
 * message; a session restart re-arms watchers for its still-running
 * tickets; a result fetched through the tool suppresses the duplicate
 * notification.
 */
class DaemonTasks {
	/** The pi-rc surface; exposed for the bash backend's wait/output. */
	readonly client: TicketClient;
	private readonly send: (
		message: { customType: string; content: string; display: boolean; details?: unknown },
		options: { triggerTurn: boolean; deliverAs: "followUp" },
	) => void;

	/** ticket id -> live watcher; owning object mutates this only. */
	private watchers = new Map<string, { stopped: boolean }>();

	/** Tickets whose result was already fetched by an explicit call. */
	private fetched = new Set<string>();

	constructor(
		exec: TicketClient["exec"],
		send: DaemonTasks["send"],
	) {
		this.client = new TicketClient(exec);
		this.send = send;
	}

	/** The owning session's stable key: the hosted session's short name
	 *  when hosted, else the conversation file's stem, else standalone. */
	sessionKey(sessionFile?: string | null): string {
		const hosted = process.env.PI_HOSTED_SESSION;
		if (hosted) return hosted.replace(/^pi-/, "");
		const file = sessionFile || process.env.PI_SESSION_FILE;
		if (file) return file.replace(/\.jsonl$/, "").split("/").pop() || "standalone";
		return "standalone";
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
				this.send(
					{
						customType: "daemon-task",
						content: `Background task finished: ${command}\n${formatResult(ticket, output)}`,
						display: true,
						details: { ticket },
					},
					{ triggerTurn: true, deliverAs: "followUp" },
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

export default function (pi: ExtensionAPI) {
	const exec = (file: string, args: string[]) => pi.exec(file, args);
	const tasks = new DaemonTasks(exec, (message, options) => {
		void pi.sendMessage(message, options);
	});
	const localBash: BashOperations = createLocalBashOperations();

	const disabled = () => process.env.PI_OFFLOAD === "off";
	const waitBoundSeconds = () => {
		const raw = Number(process.env.PI_OFFLOAD_WAIT);
		return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_WAIT_SECONDS;
	};

	// -- transparent bash offloading -------------------------------------

	const offloadOps: BashOperations = {
		exec: async (command, cwd, { onData, signal, timeout, env }) => {
			if (disabled()) {
				return localBash.exec(command, cwd, { onData, signal, timeout, env });
			}
			const sessionFile =
				typeof env?.PI_SESSION_FILE === "string" ? env.PI_SESSION_FILE : null;
			let id: string;
			try {
				id = await tasks.submit(
					sessionFile, cwd, command, sessionEnvExtra(env));
			} catch {
				// Daemon unreachable: the command never started anywhere,
				// so clean fallback to local execution is safe.
				return localBash.exec(command, cwd, { onData, signal, timeout, env });
			}
			const deadline = Date.now() + (timeout ?? waitBoundSeconds()) * 1000;
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
					tasks.client.wait(id, remaining),
					aborted.then(() => null),
				]);
				if (ticket === null) {
					// User abort: kill the daemon-side process too.
					await tasks.cancel(id).catch(() => {});
					throw new Error("aborted");
				}
				if (ticket.status === "running" && Date.now() >= deadline) {
					// Hand off: the ticket keeps running in the daemon,
					// the agent is freed now and notified on completion.
					let partial = "";
					try {
						partial = await tasks.client.outputAll(id);
					} catch {
						// partial output is best-effort
					}
					tasks.armDelivery(id, command);
					onData(Buffer.from(
						`${partial}\n[pi-daemon ticket ${id} still running: ` +
						`continuing in the background; the full result will ` +
						`be delivered here when it finishes ` +
						`(daemon_tasks result ${id} fetches it sooner)]\n`,
					));
					return { exitCode: null };
				}
			}
			let output = "";
			try {
				output = await tasks.client.outputAll(id);
			} catch {
				// output fetch is best-effort; the exit code still stands
			}
			if (ticket.status === "lost") {
				output += `\n[pi-daemon ticket ${id} was interrupted ` +
					`(daemon restart); re-run if it is safe to repeat]`;
			}
			onData(Buffer.from(output));
			return { exitCode: ticket.exit };
		},
	};

	// bash tool override: same schema, renderers, prompt snippet, and
	// result shape as the built-in; only the execution backend changes,
	// plus one guideline telling agents about the background hand-off.
	const bashTool = createBashTool(process.cwd(), { operations: offloadOps });
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
			action: StringEnum(["submit", "status", "result", "watch", "cancel", "list"] as const),
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
					const { ticket, output } = await tasks.result(
						params.id, params.wait ?? 0);
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
					const { ticket, output } = await tasks.watch(params.id, (tail) => {
						const text = formatResult({ ...ticket, status: "running" }, tail);
						if (text !== lastText) {
							lastText = text;
							onUpdate?.({
								content: [{ type: "text", text }],
								details: { ticketId: params.id, running: true },
							});
						}
					});
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
}
