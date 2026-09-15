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
 * reply immediately — no detached helper needed. If the agent is
 * mid-turn, the turn is aborted only after the handover is accepted, so
 * a failed request never interrupts the running turn; pi then shuts down
 * gracefully and the daemon hosts the session as `pi --session <file>`.
 * Reattach later with `pi-rc attach`. Ephemeral sessions (--no-session)
 * cannot be handed over.
 *
 * The extension also announces the session file to the daemon on every
 * session start (and again if it changes): the daemon stores it per
 * hosted session so that an abnormally dying pi (crash, SIGKILL, OOM)
 * can be revived headless from the same conversation. Deliberate exits
 * (clean quit such as Ctrl+D) and deleted session files are never
 * revived. The announce is fire-and-forget; a missing or unreachable
 * daemon only costs the crash-revive safety net.
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

	// Tell the daemon which session file backs this hosted pi so an
	// abnormal death can be revived as `pi --session <file>`. The daemon
	// also rewrites its registry argv with it, so a daemon restart
	// respawns the same conversation instead of a blank one. No-op
	// outside hosting and for ephemeral sessions; failures are ignored.
	async function announce(ctx: any) {
		const session = process.env.PI_HOSTED_SESSION;
		if (!session) return;
		const file: string | null | undefined =
			ctx?.sessionManager?.getSessionFile?.();
		if (!file || file === announced) return;
		announced = file;
		try {
			await pi.exec(
				`${process.env.HOME || "."}/.local/bin/pi-rc`,
				["announce", session, file],
			);
		} catch {
			// The daemon stores nothing: crash revival is simply unavailable.
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
		const piRc = `${process.env.HOME || "."}/.local/bin/pi-rc`;
		// The daemon closes this session's client bridge; the session and
		// its pi process stay alive hosted (milliseconds round-trip).
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

		const piRc = `${process.env.HOME || "."}/.local/bin/pi-rc`;
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

		// Abort an in-flight turn only after the handover is accepted:
		// ctx.shutdown() defers until the agent is idle, and without this
		// a mid-turn /bg would wait for the whole turn to finish before
		// pi ever exits. Aborting before a failed request would kill the
		// turn with nothing handed over; the daemon resumes the session
		// file, so the conversation continues there.
		if (!ctx?.isIdle?.()) {
			ctx.abort?.();
		}

		ctx?.ui?.notify?.(
			`Handing this session to the background service as ${name}; pi will exit and the session lives on there. Reattach later with: pi-rc attach ${name.replace(/^pi-/, "")}`,
			"info",
		);
		// Graceful: deferred until the agent is idle (immediately after
		// the abort above settles) and flushes the session before the
		// process goes away; the daemon starts the hosted session only
		// after this process is gone.
		ctx.shutdown();
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

	pi.on("session_start", async (_event, ctx) => {
		await announce(ctx);
	});

	// A brand-new session may not have written its file when session_start
	// fires; re-check on the first prompt (and after /fork etc., which fire
	// their own session_start). The announced-file guard keeps repeats free.
	pi.on("before_agent_start", async (_event, ctx) => {
		await announce(ctx);
	});
}
