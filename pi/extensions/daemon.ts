/**
 * Remote-control backgrounding for Pi.
 *
 * Inside a hosted session (the pi-daemon sets PI_HOSTED and
 * PI_HOSTED_SESSION in every hosted child's env), /bg is an instantaneous
 * detach: it asks the daemon to drop the client bridge (pi-rc detach).
 * The pi process keeps running headless in the daemon until the user exits
 * it or deletes the session through pi's own session manager; reattach
 * with `pi-rc attach`. Extension commands execute immediately, even while
 * the agent is mid-turn, so the detach happens the moment /bg is entered.
 * The daemon drains the detached PTY so that output never stalls the
 * child. No continuation prompt is sent: a backgrounded session simply
 * idles once its in-flight work settles, and the user resumes it by
 * reattaching or starting a fresh `pi`.
 *
 * Outside hosting (non-wrapped starts: pi launched with arguments or
 * anything that bypassed the auto-hosting wrapper), /bg hands the session
 * over to the service: `pi-rc handover --check` asks the daemon for a
 * verdict ("target:<name>" = will host under that name; "hosted:<name>"
 * = already hosted, attach instead), then `pi-rc handover --after-exit`
 * makes the daemon background its own wait-for-exit-then-host thread and
 * reply immediately — no detached helper needed. /bg NEVER aborts a
 * running operation: if the agent is mid-turn, pi prints a notice, waits
 * for the agent to fully settle (automatic retries, auto-compaction
 * retries and queued follow-ups included) and only then shuts down
 * gracefully, flushing the session; the daemon's wait thread has no
 * timeout, so it hosts the session as `pi --session <file>` whenever the
 * process goes away. Reattach later with `pi-rc attach`. Ephemeral
 * sessions (--no-session) cannot be handed over.
 *
 * The extension also announces the session file to the daemon on every
 * session start (and again if it changes): the daemon stores it per
 * hosted session so that an abnormally dying pi (crash, SIGKILL, OOM)
 * can be revived headless from the same conversation. The announce
 * reply also names any OTHER live hosted session backing the same
 * conversation — possible when pi's resume picker opens a live session
 * from a second terminal. With takeover (always requested) the daemon
 * resolves the duplicate: a BUSY holder wins — the daemon hands this
 * duplicate's bridge viewers to it and this pi shuts down, so resuming
 * a working conversation lands on the live view; holders that are
 * merely idle at the prompt instead get absorbed — this pi keeps the
 * conversation and stays open, so an in-TUI /resume never tears down
 * the view the user is in. Without a handoff or absorption (older
 * daemon, vanished holder) the extension falls back to a warning
 * naming the holder and its attach command, since pi has no
 * cross-process session locking.
 *
 * /new and /resume are carried, never terminated: pi's in-process
 * switch tears the current session down (session.abort() on an
 * in-flight turn). In a hosted session the extension cancels the
 * switch via session_before_switch and asks the daemon to carry
 * instead: /new moves this terminal's bridge onto a fresh hosted
 * session; /resume moves it onto a live holder of the picked
 * conversation or a fresh session resuming the file. Either way this
 * pi keeps running headless with its conversation — detach and attach
 * to the new view, old session backgrounded, in-flight work untouched.
 *
 * Run-state publishing keeps other terminals in sync: before each agent
 * turn the extension reports "busy" to the daemon and when the agent
 * loop settles it reports "idle", so `pi-rc ls` and every attach notice
 * can show whether the model is working without attaching. Both are
 * fire-and-forget; a missing or unreachable daemon only costs the
 * crash-revive safety net and the state display.
 *
 * Ctrl+D cannot be used for this: pi refuses extension shortcuts that
 * conflict with a built-in binding (app.exit is Ctrl+D) — registration is
 * skipped with a startup diagnostic. /bg is the supported backgrounding
 * action; stock Ctrl+D behavior is untouched everywhere.
 */

import {
	closeSync,
	existsSync,
	mkdirSync,
	openSync,
	readFileSync,
	rmSync,
} from "node:fs";
import { createConnection } from "node:net";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Directory of this extension module, when loaded as an ES module. */
function moduleDir(): string {
	try {
		return dirname(fileURLToPath(import.meta.url));
	} catch {
		return "";
	}
}

/** A bundled repo path relative to the extension, or "" when absent. */
function bundledPath(relative: string): string {
	const here = moduleDir();
	if (!here) return "";
	const candidate = join(here, relative);
	return existsSync(candidate) ? candidate : "";
}

/** pi-rc: the package-bundled pi/bin/pi-rc next to the extension, else
 *  the installed ~/.local/bin/pi-rc. */
function resolvePiRc(): string {
	const bundled = bundledPath(join("..", "bin", "pi-rc"));
	if (bundled) return bundled;
	return join(process.env.HOME || homedir(), ".local", "bin", "pi-rc");
}

/** The daemon script: bundled pi/daemon/pi-daemon, else installed. */
function resolveDaemon(): string {
	const bundled = bundledPath(join("..", "daemon", "pi-daemon"));
	if (bundled) return bundled;
	return join(process.env.HOME || homedir(), ".local", "bin", "pi-daemon");
}

/** The control endpoint file, mirroring pi_platform.RuntimeLayout. */
function endpointPath(): string {
	if (process.env.XDG_RUNTIME_DIR) {
		return join(process.env.XDG_RUNTIME_DIR, "pi-pty-host.sock");
	}
	if (process.platform === "win32") {
		const base = process.env.TEMP || process.env.TMP || homedir();
		return join(base, "pi-daemon", "pi-pty-host.sock");
	}
	const uid = typeof process.getuid === "function" ? process.getuid() : 0;
	return join("/run/user", String(uid), "pi-pty-host.sock");
}

/** The daemon's state home for the log path, mirroring RuntimeLayout. */
function stateHome(): string {
	if (process.env.XDG_STATE_HOME) return process.env.XDG_STATE_HOME;
	if (process.platform === "win32" && process.env.LOCALAPPDATA) {
		return process.env.LOCALAPPDATA;
	}
	return join(process.env.HOME || homedir(), ".local", "state");
}

/** Resolve a Python interpreter without a platform branch. */
function resolvePython(): string {
	if (process.env.PYTHON) return process.env.PYTHON;
	for (const candidate of ["python3", "python", "py"]) {
		try {
			const probe = spawnSync(candidate, ["--version"], {
				stdio: "ignore",
				windowsHide: true,
			});
			if (probe.status === 0) return candidate;
		} catch {
			// try the next candidate
		}
	}
	return "python3";
}

/** The GUI-subsystem twin candidates for a Python interpreter, in
 *  preference order. Mirrors pi-teams' WindowlessPython mapping so a
 *  detached daemon never flashes a console window on Windows. */
export function windowlessCandidates(interpreter: string): string[] {
	const lower = interpreter.toLowerCase();
	const candidates: string[] = [];
	if (lower.endsWith("python.exe")) {
		candidates.push(
			interpreter.slice(0, -"python.exe".length) + "pythonw.exe");
	} else if (lower.endsWith("python3.exe")) {
		candidates.push(
			interpreter.slice(0, -"python3.exe".length) + "pythonw.exe");
	}
	if (lower === "python" || lower === "python.exe") {
		candidates.push("pythonw");
	}
	if (lower === "py" || lower === "py.exe") candidates.push("pyw");
	candidates.push("pythonw");
	return candidates;
}

/** The windowless Python to launch the detached daemon with: the
 *  GUI-subsystem twin of the resolved interpreter first, then the
 *  windowless names, then the interpreter itself as a last resort. */
function resolveWindowlessPython(): string {
	if (process.platform !== "win32") return resolvePython();
	const base = resolvePython();
	for (const candidate of windowlessCandidates(base)) {
		if (candidate === base) continue;
		try {
			const probe = spawnSync(candidate, ["-c", "pass"], {
				stdio: "ignore",
				windowsHide: true,
			});
			if (probe.status === 0) return candidate;
		} catch {
			// try the next candidate
		}
	}
	return base;
}

/** One line of the daemon's announce reply, parsed. */
interface Handoff {
	name: string;
	state: string;
}

/** Holds a control connection open until this pi exits: the daemon hosts
 *  the handed-over session when the connection reaches EOF, so liveness
 *  is connection-based and never a pid probe. */
export class HandoverHold {
	private readonly path: string;
	private socket: ReturnType<typeof createConnection> | null = null;

	constructor(path: string = endpointPath()) {
		this.path = path;
	}

	/** Send the hold handover and resolve true once the daemon has
	 *  registered it. The connection stays open; its later close (this
	 *  process exiting) is the daemon's host signal. */
	async open(file: string, dir: string): Promise<boolean> {
		let endpoint: { host?: string; port?: number; token?: string };
		try {
			endpoint = JSON.parse(readFileSync(this.path, "utf8"));
		} catch {
			return false;
		}
		if (!endpoint.host || !endpoint.port) return false;
		return await new Promise<boolean>((resolve) => {
			const sock = createConnection({
				host: endpoint.host,
				port: endpoint.port,
			});
			this.socket = sock;
			let buffer = "";
			let phase = "hello";
			let settled = false;
			const finish = (value: boolean): void => {
				if (settled) return;
				settled = true;
				if (!value) {
					this.socket = null;
					try {
						sock.destroy();
					} catch {
						// already gone
					}
				}
				resolve(value);
			};
			sock.setNoDelay(true);
			sock.on("connect", () => {
				sock.write(JSON.stringify({ cmd: "hello",
					token: endpoint.token }) + "\n");
			});
			sock.on("data", (chunk: Buffer) => {
				buffer += String(chunk);
				while (buffer.includes("\n")) {
					const index = buffer.indexOf("\n");
					const line = buffer.slice(0, index);
					buffer = buffer.slice(index + 1);
					if (!line.trim()) continue;
					let msg: { ok?: boolean };
					try {
						msg = JSON.parse(line);
					} catch {
						finish(false);
						return;
					}
					if (phase === "hello") {
						if (!msg.ok) {
							finish(false);
							return;
						}
						phase = "handover";
						sock.write(JSON.stringify({ cmd: "handover",
							file, dir, hold: true }) + "\n");
						continue;
					}
					finish(Boolean(msg.ok));
					return;
				}
			});
			sock.on("error", () => finish(false));
		});
	}
}

/** Keeps the daemon reachable on platforms without a service manager.
 *  On POSIX the systemd unit owns it, so this is a no-op there. */
export class DaemonSupervisor {
	private readonly daemonPath: string;
	private starting = false;

	constructor(daemonPath: string = resolveDaemon()) {
		this.daemonPath = daemonPath;
	}

	async ensure(): Promise<void> {
		if (process.platform !== "win32") return;
		if (existsSync(endpointPath())) return;
		if (this.starting) return;
		this.starting = true;
		try {
			const logDir = join(stateHome(), "pi-pty-host");
			mkdirSync(logDir, { recursive: true });
			const log = openSync(join(logDir, "daemon.log"), "a");
			try {
				const child = spawn(resolveWindowlessPython(),
					[this.daemonPath], {
						detached: true,
						windowsHide: true,
						stdio: ["ignore", log, log],
					});
				child.unref();
			} finally {
				closeSync(log);
			}
		} catch {
			// Best-effort: an unreachable daemon only costs /bg + revive.
		} finally {
			this.starting = false;
		}
	}
}

export class RcBackground {
	/** Session file already reported to the daemon; skipped announces
	 *  keep the per-prompt hook free of execs. */
	private announced: string | null = null;

	private readonly piRc: string;
	private readonly hold: HandoverHold;
	private readonly supervisor: DaemonSupervisor;

	/** The only mutable dependency, injected: how to run pi-rc. */
	private readonly exec: (file: string, args: string[]) => Promise<any>;

	constructor(
		exec: (file: string, args: string[]) => Promise<any>,
		hold: HandoverHold = new HandoverHold(),
		supervisor: DaemonSupervisor = new DaemonSupervisor(),
	) {
		this.exec = exec;
		this.hold = hold;
		this.supervisor = supervisor;
		this.piRc = resolvePiRc();
	}

	/** Run pi-rc through the OS launcher: POSIX execs the shebang'd
	 *  script directly, Windows needs the Python interpreter. */
	private runPiRc(args: string[]): Promise<any> {
		if (process.platform === "win32") {
			// The windowless twin keeps each short-lived pi-rc call from
			// flashing a console; pi pipes its stdio, so output still lands.
			return this.exec(resolveWindowlessPython(), [this.piRc, ...args]);
		}
		return this.exec(this.piRc, args);
	}

	/** PI_HOSTED_SESSION is the full daemon name ("pi-<base>"); pi-rc's
	 *  session_name() prepends another "pi-", so commands taking a name
	 *  get the short form exactly like detach does. */
	private hostedSession(): string {
		return (process.env.PI_HOSTED_SESSION || "").replace(/^pi-/, "");
	}

	/** Tell the daemon which session file backs this hosted pi so an
	 *  abnormal death can be revived as `pi --session <file>`. The daemon
	 *  also rewrites its registry argv with it, so a daemon restart
	 *  respawns the same conversation instead of a blank one, and replies
	 *  with any other live holder of the same conversation plus, with
	 *  takeover, its resolution of the duplicate. No-op outside hosting
	 *  and for ephemeral sessions; failures are retried on the next
	 *  prompt (the announced marker is only set on success). */
	/** Ensure the daemon is reachable. No-op on POSIX (systemd owns it);
	 *  on Windows it starts the windowless detached daemon. */
	async ensureDaemon(): Promise<void> {
		await this.supervisor.ensure();
	}

	async announce(ctx: any): Promise<void> {
		const session = this.hostedSession();
		if (!session) return;
		const file: string | null | undefined =
			ctx?.sessionManager?.getSessionFile?.();
		if (!file || file === this.announced) return;
		try {
			const result = await this.runPiRc(
				["announce", session, file, "--takeover"]);
			if (result.code !== 0) return;
			this.announced = file;
			const lines: string[] = (result.stdout || "")
				.split("\n")
				.map((l: string) => l.trim());
			const handoff: Handoff | undefined = lines
				.map((l) => /^handoff (.+)\t(\S+)$/.exec(l))
				.filter(Boolean)
				.map((m) => ({ name: m![1], state: m![2] }))[0];
			const tookOver: string[] = lines
				.filter((l) => l.startsWith("took-over\t"))
				.map((l) => l.slice("took-over\t".length))
				.filter(Boolean);
			if (tookOver.length > 0) {
				// Idle holders of the picked conversation were absorbed:
				// this session keeps the conversation and stays open — no
				// shutdown, the user never leaves their view.
				ctx?.ui?.notify?.(
					`Took this conversation over from idle session${tookOver.length > 1 ? "s" : ""} ` +
						`${tookOver.join(", ")}; their views follow here.`,
					"info",
				);
				return;
			}
			if (handoff) {
				if (typeof ctx?.shutdown === "function") {
					ctx?.ui?.notify?.(
						`This conversation is already live in ${handoff.name} ` +
							`(${handoff.state === "busy" ? "model working" : "idle"}); ` +
							`handing this view over to it.`,
						"info",
					);
					ctx.shutdown();
				} else {
					// Without a way to shut this duplicate down it would
					// linger headless beside the holder; point the user at
					// the live view instead.
					ctx?.ui?.notify?.(
						`This conversation is already live in ${handoff.name} ` +
							`(${handoff.state === "busy" ? "model working" : "idle"}); ` +
							`attach there: pi-rc attach ${handoff.name}`,
						"warning",
					);
				}
				return;
			}
			// Multi-terminal sync without a handoff (daemon too old to take
			// over, or the holder vanished): another live hosted session
			// backing this conversation means pi's picker duplicated a live
			// session; the copies would diverge silently (pi has no
			// cross-process session locking).
			const others: string[] = lines
				.filter((l) => l.startsWith("also-live "))
				.map((l) => l.split(/\s+/)[1])
				.filter(Boolean);
			if (others.length > 0) {
				ctx?.ui?.notify?.(
					`This conversation is also live in hosted session${others.length > 1 ? "s" : ""} ${others.join(", ")}; ` +
						`those copies do not share state with this one. Attach the live one instead: pi-rc attach ${others[0]}`,
					"warning",
				);
			}
		} catch {
			// The daemon stores nothing: crash revival is simply unavailable.
		}
	}

	/** Publish whether the model is working so other terminals can see
	 *  the state in `pi-rc ls` and attach notices without attaching.
	 *  Skipped without a session file: hosted `--no-session` children
	 *  (one-shots) inherit this session's name in their env,
	 *  and their start/end would otherwise overwrite the working
	 *  session's daemon-side state. Fire-and-forget: cosmetic only when
	 *  the daemon is unreachable. */
	async setState(state: "busy" | "idle", ctx?: any): Promise<void> {
		const session = this.hostedSession();
		if (!session) return;
		if (ctx && !ctx?.sessionManager?.getSessionFile?.()) return;
		try {
			await this.runPiRc( ["state", session, state]);
		} catch {
			// State display is best-effort.
		}
	}

	/** The daemon closes every attached client bridge for this session;
	 *  the session and its pi process stay alive hosted (milliseconds
	 *  round-trip). With several terminals sharing the view, /bg drops
	 *  all of them — the daemon cannot tell which one asked. The user
	 *  reattaches with: pi-rc attach <name>. */
	async detach(ctx: any): Promise<void> {
		const session = this.hostedSession();
		if (!session) return;
		const result = await this.runPiRc( ["detach", session]);
		if (result.code !== 0 && !result.killed) {
			ctx?.ui?.notify?.(
				`Detach failed: ${(result.stderr || result.stdout || "").trim() || `exit ${result.code}`}`,
				"warning",
			);
			return;
		}
	}

	/** Take the running pi out of the foreground WITHOUT ever touching the
	 *  model: /bg must never stop or abort an in-flight operation. If the
	 *  agent is busy, ctx.waitForIdle() resolves only when the whole agent
	 *  loop has settled (automatic retries, auto-compaction retries and
	 *  queued follow-ups included — the settle signal pi's agent_settled
	 *  event also reports); pi then exits cleanly through ctx.shutdown(),
	 *  which flushes the session before the process goes away. The
	 *  daemon's wait-for-exit thread (pi-daemon handover_thread) polls the
	 *  pid with no timeout, so a long settle simply delays the adoption.
	 *  If the settle wait itself fails, pi stays up untouched: the daemon
	 *  keeps waiting and adoption still happens whenever this pi later
	 *  exits for any reason. */
	async exitAfterSettle(ctx: any): Promise<void> {
		if (!ctx?.isIdle?.()) {
			ctx?.ui?.notify?.(
				"bg: waiting for the current operation to settle before backgrounding...",
				"info",
			);
			if (typeof ctx.waitForIdle === "function") {
				await ctx.waitForIdle();
			}
			// Without waitForIdle (older pi), ctx.shutdown() below still
			// defers until the agent is idle, so the wait degrades to the
			// same graceful behavior rather than an interruption.
		}
		// Graceful: flushes the session before the process goes away; the
		// daemon starts the hosted session only after this process is gone.
		ctx.shutdown();
	}

	/** Take the running pi out of the foreground WITHOUT ever touching
	 *  the model, handing its saved session to the pi-daemon. */
	async handover(ctx: any): Promise<void> {
		const sessionFile: string | null | undefined =
			ctx?.sessionManager?.getSessionFile?.();
		if (!sessionFile) {
			ctx?.ui?.notify?.(
				"Cannot background an ephemeral session (--no-session): nothing is persisted to hand over.",
				"warning",
			);
			return;
		}

		const dir = process.cwd();

		// Preflight: the daemon hosts one session per target name, so ask it
		// for the verdict for this exact session file: "target:<name>" means
		// host under that name (a distinct name when the directory's primary
		// is taken by another conversation); "hosted:<name>" (with exit 3)
		// means this session is already hosted — attach instead of
		// duplicating it. Exit 4 means the daemon is unreachable.
		const check = await this.runPiRc(
			["handover", sessionFile, dir, "--check"]);
		const verdict = (check.stdout || "").trim();
		const match = /^(target|hosted):(.+)$/.exec(verdict);
		if (match && match[1] === "hosted") {
			ctx?.ui?.notify?.(
				`This session is already hosted (${match[2]}); attach with: pi-rc attach ${match[2].replace(/^pi-/, "")}`,
				"warning",
			);
			return;
		}
		if (check.code !== 0 || !match) {
			ctx?.ui?.notify?.(
				`Handover unavailable: ${(check.stderr || check.stdout || "").trim() || `exit ${check.code}`}`,
				"warning",
			);
			return;
		}
		const name = match[2];

		// Register the handover with the daemon and keep the connection
		// open: its EOF when this pi exits is the daemon's host signal
		// (connection liveness, never a pid probe). Fall back to a
		// short-lived pi-rc that holds the same connection itself when the
		// endpoint file is not reachable.
		const held = await this.hold.open(sessionFile, dir);
		if (!held) {
			void this.runPiRc( [
				"handover",
				sessionFile,
				dir,
				"--after-exit",
				String(process.pid),
			]);
		}

		ctx?.ui?.notify?.(
			`Handing this session to the pi-daemon as ${name}; pi will exit and the session lives on there. Reattach later with: pi-rc attach ${name.replace(/^pi-/, "")}`,
			"info",
		);
		// Fire-and-forget so the TUI stays responsive during the settle
		// wait; the handler returns immediately either way.
		void this.exitAfterSettle(ctx).catch((err: unknown) => {
			ctx?.ui?.notify?.(
				`bg: could not finish backgrounding (${err instanceof Error ? err.message : String(err)}); pi stays up and the daemon adopts the session whenever pi exits.`,
				"warning",
			);
		});
	}

	/** /bg: detach when hosted, hand over to the pi-daemon
	 *  otherwise. */
	async background(ctx: any): Promise<void> {
		if (process.env.PI_HOSTED) {
			await this.detach(ctx);
		} else {
			await this.handover(ctx);
		}
	}

	/** /new and /resume must never terminate or abort the session: pi's
	 *  in-process switch tears the current session down
	 *  (session.abort()). In a hosted session, cancel the switch and
	 *  carry instead — /new onto a fresh hosted session, /resume onto a
	 *  live holder of the picked conversation or a fresh session
	 *  resuming the file. Outside hosting — or when the daemon is
	 *  unreachable — pi's native behavior applies untouched. */
	async beforeSwitch(event: any, ctx: any): Promise<{ cancel: boolean } | void> {
		const reason = event?.reason;
		if (reason !== "new" && reason !== "resume") return;
		const session = this.hostedSession();
		if (!session) return;
		try {
			if (reason === "resume") {
				const target = event.targetSessionFile;
				const current = ctx?.sessionManager?.getSessionFile?.();
				if (!target || target === current) return { cancel: true };
				const result = await this.runPiRc(
					["resume", session, target]);
				if (result.code !== 0) return;
			} else {
				const result = await this.runPiRc( ["carry", session]);
				if (result.code !== 0) return;
			}
		} catch {
			return;
		}
		return { cancel: true };
	}
}

export default function (pi: ExtensionAPI) {
	const app = new RcBackground((file, args) => pi.exec(file, args));

	pi.registerCommand("bg", {
		description:
			"Background this session (detach when hosted, hand over to the pi-daemon otherwise)",
		handler: async (_args, ctx) => {
			await app.background(ctx);
		},
	});

	// A brand-new session may not have written its file when session_start
	// fires; re-check on the first prompt (and after /fork etc., which fire
	// their own session_start). The announced-file guard keeps repeats
	// free. The first prompt is also where a turn begins: report busy.
	pi.on("session_start", async (_event, ctx) => {
		await app.ensureDaemon();
		await app.announce(ctx);
		await app.setState("idle", ctx);
	});
	// After an in-place /reload (daemon-triggered extension updates) do
	// the reload silently: consume the daemon's diff stamp so a later
	// manual /reload never repeats a stale report, and never tell the
	// agent what changed. The stamp is written by the daemon
	// (extensions_reload) before it types /reload into this session; a
	// manual /reload has no stamp and skips straight out.
	pi.on("session_start", async (event) => {
		if (event.reason !== "reload") return;
		const stateHome = process.env.XDG_STATE_HOME ||
			`${process.env.HOME || "."}/.local/state`;
		const diffPath = `${stateHome}/pi-pty-host/extensions-diff.json`;
		rmSync(diffPath, { force: true });
	});
	pi.on("before_agent_start", async (_event, ctx) => {
		await app.announce(ctx);
		await app.setState("busy", ctx);
	});
	// The loop ended; agent_settled additionally covers automatic retries,
	// compaction retries and queued follow-ups. Both report the same idle
	// state, so whichever lands last leaves the correct value behind.
	pi.on("agent_end", async (_event, ctx) => {
		await app.setState("idle", ctx);
	});
	pi.on("agent_settled", async (_event, ctx) => {
		await app.setState("idle", ctx);
	});
	pi.on("session_before_switch", async (event, ctx) => {
		return app.beforeSwitch(event, ctx);
	});
}
