/**
 * Remote-control backgrounding for Pi.
 *
 * Inside a tmux-hosted session (the pi-background-service systemd service,
 * or any tmux), the /bg command detaches the tmux client: the pi process
 * keeps running hosted until the user exits it or deletes the session
 * through pi's own session manager.
 *
 * Outside tmux (non-wrapped starts: pi launched with arguments or anything
 * that bypassed the auto-hosting wrapper), /bg hands the session over to
 * the persistent service instead: a detached helper waits for this pi to
 * exit, then asks pi-rc to host a pane resuming the exact session file,
 * and pi shuts down gracefully. Reattach later with `pi-rc attach`.
 * Ephemeral sessions (--no-session) cannot be handed over.
 *
 * Ctrl+D cannot be used for this: pi refuses extension shortcuts that
 * conflict with a built-in binding (app.exit is Ctrl+D) — registration is
 * skipped with a startup diagnostic. /bg is the supported backgrounding
 * action; stock Ctrl+D behavior is untouched everywhere.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Quote a value for the shell pi-rc and tmux pane commands run under. */
function shquote(value: string): string {
	return `'${value.replace(/'/g, "'\\''")}'`;
}

export default function (pi: ExtensionAPI) {
	async function detach(ctx: any) {
		// Plain `tmux` resolves the controlling server from $TMUX, so this
		// detaches this very session's client whatever socket it lives on.
		const result = await pi.exec("tmux", ["detach-client"]);
		if (result.code !== 0 && !result.killed) {
			ctx?.ui?.notify?.(
				`Detach failed: ${(result.stderr || result.stdout || "").trim() || `exit ${result.code}`}`,
				"warning",
			);
		}
		// On success the client is simply gone; this pi keeps running hosted.
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

		// Preflight: pi-rc hosts one session per directory name, so ask it
		// for the verdict for this exact session file: "target:<name>" means
		// host under that name (distinct name when the directory's primary
		// is taken by another conversation); "hosted:<name>" means this
		// session is already hosted — attach instead of duplicating it.
		const check = await pi.exec(piRc, ["handover", "--check", sessionFile, dir]);
		const verdict = (check.stdout || "").trim();
		const match = /^(target|hosted):(.+)$/.exec(verdict);
		if (check.code !== 0 || !match) {
			ctx?.ui?.notify?.(
				`Handover unavailable: ${(check.stderr || check.stdout || "").trim() || `exit ${check.code}`}`,
				"warning",
			);
			return;
		}
		const name = match[2];
		if (match[1] === "hosted") {
			ctx?.ui?.notify?.(
				`This session is already hosted (${name}); attach with: pi-rc attach ${name.replace(/^pi-/, "")}`,
				"warning",
			);
			return;
		}

		// Detached helper survives this pi's exit (setsid: new session, so a
		// process-group kill on exit cannot reach it). It waits for this pi
		// to finish flushing and die, then hosts the exact session file.
		const helper =
			`setsid nohup sh -c ${shquote(
				`${piRc} handover ${shquote(sessionFile)} ${shquote(dir)} --after-exit ${process.pid}`,
			)} >/dev/null 2>&1 &`;
		const spawn = await pi.exec("sh", ["-c", helper]);
		if (spawn.code !== 0) {
			ctx?.ui?.notify?.(
				`Failed to start the handover helper: ${(spawn.stderr || spawn.stdout || "").trim() || `exit ${spawn.code}`}`,
				"warning",
			);
			return;
		}

		ctx?.ui?.notify?.(
			`Handing this session to the background service as ${name}; pi will exit. Reattach later with: pi-rc attach ${name.replace(/^pi-/, "")}`,
			"info",
		);
		// Graceful: deferred until the agent is idle and flushes the session
		// before the process goes away; the helper starts the hosted pane
		// only after this process is gone.
		ctx.shutdown();
	}

	pi.registerCommand("bg", {
		description:
			"Background this session (detach when hosted, hand over to the background service otherwise)",
		handler: async (_args, ctx) => {
			if (process.env.TMUX) {
				await detach(ctx);
			} else {
				await handover(ctx);
			}
		},
	});
}
