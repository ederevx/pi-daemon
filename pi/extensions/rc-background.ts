/**
 * Remote-control backgrounding for Pi.
 *
 * Inside a hosted session (the pi-ptyd daemon sets PI_HOSTED and
 * PI_HOSTED_SESSION in every hosted child's env), /bg is an instantaneous
 * detach: it asks the daemon to drop the client bridge (pi-rc detach).
 * The pi process keeps running headless in the daemon until the user exits
 * it or deletes the session through pi's own session manager; reattach
 * with `pi-rc attach`. The control round-trip is milliseconds.
 *
 * Outside hosting (non-wrapped starts: pi launched with arguments or
 * anything that bypassed the auto-hosting wrapper), /bg hands the session
 * over to the service: `pi-rc handover --check` asks the daemon for a
 * verdict ("target:<name>" = will host under that name; "hosted:<name>"
 * = already hosted, attach instead), then `pi-rc handover --after-exit`
 * makes the daemon background its own wait-for-exit-then-host thread and
 * reply immediately — no detached helper needed — and pi shuts down
 * gracefully. Reattach later with `pi-rc attach`. Ephemeral sessions
 * (--no-session) cannot be handed over.
 *
 * Ctrl+D cannot be used for this: pi refuses extension shortcuts that
 * conflict with a built-in binding (app.exit is Ctrl+D) — registration is
 * skipped with a startup diagnostic. /bg is the supported backgrounding
 * action; stock Ctrl+D behavior is untouched everywhere.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	async function detach(ctx: any) {
		const session = process.env.PI_HOSTED_SESSION || "";
		const piRc = `${process.env.HOME || "."}/.local/bin/pi-rc`;
		// The daemon closes this session's client bridge; the session and
		// its pi process stay alive in the daemon (milliseconds round-trip).
		const result = await pi.exec(piRc, ["detach", session]);
		if (result.code !== 0 && !result.killed) {
			ctx?.ui?.notify?.(
				`Detach failed: ${(result.stderr || result.stdout || "").trim() || `exit ${result.code}`}`,
				"warning",
			);
		}
		// On success this pi keeps running hosted, unhosted-by-client; the
		// user reattaches with: pi-rc attach <name>.
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
		// hosts the exact session file. No setsid helper is needed anymore.
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
			`Handing this session to the background service as ${name}; pi will exit. Reattach later with: pi-rc attach ${name.replace(/^pi-/, "")}`,
			"info",
		);
		// Graceful: deferred until the agent is idle and flushes the session
		// before the process goes away; the daemon starts the hosted session
		// only after this process is gone.
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
