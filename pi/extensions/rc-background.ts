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
 * After a successful detach the extension queues a continuation prompt
 * (follow-up when the agent is mid-turn) so the session keeps working on
 * its tasks headless instead of idling at the next stop; the daemon
 * drains the detached PTY so that output never stalls the child.
 *
 * Outside hosting (non-wrapped starts: pi launched with arguments or
 * anything that bypassed the auto-hosting wrapper), /bg hands the session
 * over to the service: `pi-rc handover --check` asks the daemon for a
 * verdict ("target:<name>" = will host under that name; "hosted:<name>"
 * = already hosted, attach instead), then `pi-rc handover --after-exit
 * --message <text>` makes the daemon background its own
 * wait-for-exit-then-host thread and reply immediately — no detached
 * helper needed. If the agent is mid-turn, the turn is aborted only
 * after the handover is accepted, so a failed request never interrupts
 * the running turn; pi then shuts down gracefully. The daemon hosts the
 * session as `pi --session <file> <message>`, resuming with the
 * continuation prompt as the initial input. Reattach
 * later with `pi-rc attach`. Ephemeral sessions (--no-session) cannot be
 * handed over.
 *
 * Ctrl+D cannot be used for this: pi refuses extension shortcuts that
 * conflict with a built-in binding (app.exit is Ctrl+D) — registration is
 * skipped with a startup diagnostic. /bg is the supported backgrounding
 * action; stock Ctrl+D behavior is untouched everywhere.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Sent after every successful backgrounding: as a queued follow-up (or
// the handover session's initial prompt) it keeps the agent working on
// its in-flight tasks while no user is attached.
const KICKOFF =
	"[backgrounded] The user detached this session; no one is watching the " +
	"terminal. Continue the tasks you were working on until they are fully " +
	"complete. Do not stop to wait for input: when you would normally ask " +
	"the user a question, pick the most reasonable option, proceed, and " +
	"record the decision. Persist or commit completed work as you go.";

export default function (pi: ExtensionAPI) {
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
		// Kick the headless agent so it continues its tasks instead of
		// idling. deliverAs "followUp" queues behind an in-flight turn
		// and triggers a fresh turn when idle. A pending message already
		// covering the continuation (e.g. a repeated /bg) skips the kick.
		// Note: sendUserMessage lives on the extension API (pi), not on
		// command contexts; it is fire-and-forget — errors surface through
		// the runner's error event, so failure here cannot wedge /bg.
		try {
			if (!ctx?.hasPendingMessages?.()) {
			pi.sendUserMessage(KICKOFF, { deliverAs: "followUp" });
			}
		} catch (err) {
			ctx?.ui?.notify?.(
				`Detached, but the continuation prompt failed: ${err instanceof Error ? err.message : String(err)}`,
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
		// hosts the exact session file as `pi --session <file> <message>`,
		// so the hosted session starts by continuing the tasks. No setsid
		// helper is needed.
		const spawn = await pi.exec(piRc, [
			"handover",
			sessionFile,
			dir,
			"--after-exit",
			String(process.pid),
			"--message",
			KICKOFF,
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
		// with the continuation prompt, so the work continues there.
		if (!ctx?.isIdle?.()) {
			ctx.abort?.();
		}

		ctx?.ui?.notify?.(
			`Handing this session to the background service as ${name}; pi will exit and the work continues there. Reattach later with: pi-rc attach ${name.replace(/^pi-/, "")}`,
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
}
