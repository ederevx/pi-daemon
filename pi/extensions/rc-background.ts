/**
 * Remote-control backgrounding for Pi.
 *
 * Inside a hosted session (the pi-ptyd daemon sets PI_HOSTED and
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
 * The announced marker is only set after a successful announce, so a
 * transient daemon outage retries on the next prompt instead of being
 * skipped for the session's lifetime.
 *
 * Run-state publishing keeps other terminals in sync: before each agent
 * turn the extension reports "busy" to the daemon and when the agent
 * loop settles it reports "idle", so `pi-rc ls` and every attach notice
 * can show whether the model is working without attaching. Both are
 * fire-and-forget; a missing or unreachable daemon only costs the
 * crash-revive safety net and the state display.
 *
 * /new is carried, never terminated: pi's in-process switch aborts an
 * in-flight turn, so in a hosted session the extension cancels the
 * switch and asks the daemon to spawn a fresh hosted session and move
 * this terminal's bridge onto it (pi-rc carry). This pi keeps running
 * headless with its conversation — detach and attach to the new
 * session, old session backgrounded, in-flight work untouched.
 *
 * Ctrl+D cannot be used for this: pi refuses extension shortcuts that
 * conflict with a built-in binding (app.exit is Ctrl+D) — registration is
 * skipped with a startup diagnostic. /bg is the supported backgrounding
 * action; stock Ctrl+D behavior is untouched everywhere.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	// Session file already reported to the daemon for this extension
	// instance; skipped announces keep the per-prompt hook free of execs.
	let announced: string | null = null;

	const piRc = `${process.env.HOME || "."}/.local/bin/pi-rc`;

	// PI_HOSTED_SESSION is the full daemon name ("pi-<base>"); pi-rc's
	// session_name() prepends another "pi-", so commands taking a name
	// get the short form exactly like detach does.
	const hostedShort = () =>
		(process.env.PI_HOSTED_SESSION || "").replace(/^pi-/, "");

	// Tell the daemon which session file backs this hosted pi so an
	// abnormal death can be revived as `pi --session <file>`. The daemon
	// also rewrites its registry argv with it, so a daemon restart
	// respawns the same conversation instead of a blank one, and replies
	// with any other live holder of the same conversation. No-op outside
	// hosting and for ephemeral sessions; failures are retried on the
	// next prompt (the announced marker is only set on success).
	async function announce(ctx: any) {
		const session = hostedShort();
		if (!session) return;
		const file: string | null | undefined =
			ctx?.sessionManager?.getSessionFile?.();
		if (!file || file === announced) return;
		try {
			const result = await pi.exec(piRc, ["announce", session, file, "--takeover"]);
			if (result.code !== 0) return;
			announced = file;
			const lines = (result.stdout || "")
				.split("\n")
				.map((l) => l.trim());
			// Resume takeover: this pi is a duplicate holder of a live
			// conversation (the picker opened one another session already
			// backs). The daemon closed this session's bridges toward its
			// holder and this pi must go away: the user's pi-rc attach
			// follows the handoff and lands on the live view, busy state
			// included, instead of an idle duplicate.
			const handoff = lines
				.map((l) => /^handoff (.+)\t(\S+)$/.exec(l))
				.filter(Boolean)
				.map((m) => ({ name: m![1], state: m![2] }))[0];
			const tookOver = lines
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
			const others = lines
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

	// Publish whether the model is working so other terminals can see
	// the state in `pi-rc ls` and attach notices without attaching.
	// Fire-and-forget: cosmetic only when the daemon is unreachable.
	async function setState(state: "busy" | "idle") {
		const session = hostedShort();
		if (!session) return;
		try {
			await pi.exec(piRc, ["state", session, state]);
		} catch {
			// State display is best-effort.
		}
	}

	async function detach(ctx: any) {
		// PI_HOSTED_SESSION is the full daemon name ("pi-<base>") but pi-rc's
		// detach expects the short name and prepends "pi-" itself — passing
		// the full name detached "pi-pi-<base>" (a session that does not
		// exist), the daemon replied ok, and the user's terminal never left
		// the TUI. Strip exactly one prefix so session_name() rebuilds the
		// same name (correct even for "pi-pi-*" sessions from "pi-*" dirs).
		const session = (process.env.PI_HOSTED_SESSION || "").replace(/^pi-/, "");
		// The daemon closes every attached client bridge for this session;
		// the session and its pi process stay alive hosted (milliseconds
		// round-trip). With several terminals sharing the view, /bg drops
		// all of them — the daemon cannot tell which one asked.
		const result = await pi.exec(piRc, ["detach", session]);
		if (result.code !== 0 && !result.killed) {
			ctx?.ui?.notify?.(
				`Detach failed: ${(result.stderr || result.stdout || "").trim() || `exit ${result.code}`}`,
				"warning",
			);
			return;
		}
		// The user reattaches with: pi-rc attach <name>.
	}

	// Take the running pi out of the foreground WITHOUT ever touching the
	// model: /bg must never stop or abort an in-flight operation. If the
	// agent is busy, ctx.waitForIdle() resolves only when the whole agent
	// loop has settled (automatic retries, auto-compaction retries and
	// queued follow-ups included — the settle signal pi's agent_settled
	// event also reports); pi then exits cleanly through ctx.shutdown(),
	// which flushes the session before the process goes away. The
	// daemon's wait-for-exit thread (pi-ptyd handover_thread) polls the
	// pid with no timeout, so a long settle simply delays the adoption.
	// If the settle wait itself fails, pi stays up untouched: the daemon
	// keeps waiting and adoption still happens whenever this pi later
	// exits for any reason.
	async function exitAfterSettle(ctx: any) {
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

	async function handover(ctx: any) {
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
		const check = await pi.exec(piRc, ["handover", sessionFile, dir, "--check"]);
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

		// The daemon replies immediately with the verdict and backgrounds
		// its own wait-for-exit-then-host thread: once this pi exits, it
		// hosts the exact session file as `pi --session <file>`, resuming
		// the conversation idle at the prompt. No setsid helper is needed.
		const spawn = await pi.exec(piRc, [
			"handover",
			sessionFile,
			dir,
			"--after-exit",
			String(process.pid),
		]);
		if (spawn.code !== 0) {
			ctx?.ui?.notify?.(
				`Handover failed: ${(spawn.stderr || spawn.stdout || "").trim() || `exit ${spawn.code}`}`,
				"warning",
			);
			return;
		}

		ctx?.ui?.notify?.(
			`Handing this session to the background service as ${name}; pi will exit and the session lives on there. Reattach later with: pi-rc attach ${name.replace(/^pi-/, "")}`,
			"info",
		);
		// Fire-and-forget so the TUI stays responsive during the settle
		// wait; the handler returns immediately either way.
		void exitAfterSettle(ctx).catch((err: unknown) => {
			ctx?.ui?.notify?.(
				`bg: could not finish backgrounding (${err instanceof Error ? err.message : String(err)}); pi stays up and the daemon adopts the session whenever pi exits.`,
				"warning",
			);
		});
	}

	pi.registerCommand("bg", {
		description:
			"Background this session (detach when hosted, hand over to the background service otherwise)",
		handler: async (_args, ctx) => {
			if (process.env.PI_HOSTED) {
				await detach(ctx);
			} else {
				await handover(ctx);
			}
		},
	});

	pi.on("session_before_switch", async (event, ctx) => {
		// /new in a hosted session must never terminate or abort it: pi's
		// in-process switch calls session.abort() on an in-flight turn.
		// Cancel it and carry instead: the daemon spawns a fresh hosted
		// session, moves this terminal's bridge onto it, and this pi keeps
		// running headless with its conversation (return with
		// `pi-rc attach <old-name>`). Outside hosting — or when the daemon
		// is unreachable — pi's native /new applies untouched.
		if (event?.reason !== "new") return;
		const session = hostedShort();
		if (!session) return;
		try {
			const result = await pi.exec(piRc, ["carry", session]);
			if (result.code !== 0) return;
		} catch {
			return;
		}
		return { cancel: true };
	});

	pi.on("session_start", async (_event, ctx) => {
		await announce(ctx);
		await setState("idle");
	});

	// A brand-new session may not have written its file when session_start
	// fires; re-check on the first prompt (and after /fork etc., which fire
	// their own session_start). The announced-file guard keeps repeats
	// free. The first prompt is also where a turn begins: report busy.
	pi.on("before_agent_start", async (_event, ctx) => {
		await announce(ctx);
		await setState("busy");
	});

	// The loop ended; agent_settled additionally covers automatic retries,
	// compaction retries and queued follow-ups. Both report the same idle
	// state, so whichever lands last leaves the correct value behind.
	pi.on("agent_end", async () => {
		await setState("idle");
	});
	pi.on("agent_settled", async () => {
		await setState("idle");
	});
}
