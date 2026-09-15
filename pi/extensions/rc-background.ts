/**
 * Remote-control backgrounding for Pi.
 *
 * Inside a tmux-hosted session (the pi-background-service systemd service, or
 * any tmux), the /bg command detaches the tmux client: the pi process keeps
 * running hosted until the user exits it or deletes the session through pi's
 * own session manager. Outside tmux /bg reports that there is nothing to
 * background.
 *
 * Ctrl+D cannot be used for this: pi refuses extension shortcuts that
 * conflict with a built-in binding (app.exit is Ctrl+D) — registration is
 * skipped with a startup diagnostic. /bg is the supported backgrounding
 * action; stock Ctrl+D behavior is untouched everywhere.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	async function detach(ctx: any) {
		if (!process.env.TMUX) {
			ctx?.ui?.notify?.(
				"Nothing to background: pi is not running inside tmux.",
				"warning",
			);
			return;
		}
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

	pi.registerCommand("bg", {
		description: "Background this session (detach the terminal)",
		handler: async (_args, ctx) => {
			await detach(ctx);
		},
	});
}
