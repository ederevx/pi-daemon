/**
 * Provisioning reconciliation for the pi-daemon package.
 *
 * pi runs `npm install` — and with it `scripts/postinstall.mjs` — only when
 * a git checkout's HEAD moves, so a release tag cut on the commit already
 * checked out leaves the daemon, client, wrapper, unit, and seam modules on
 * the previous version. This extension closes that window: on every session
 * start it compares the clone against the sources `scripts/install.sh`
 * recorded in its manifest and re-runs package-mode provisioning in the
 * background when they differ.
 *
 * Only the package ships this file, so provisioning never runs from a manual
 * extension copy, which has no `scripts/` beside it.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
	PackageProvisioner,
	ProvisioningGuard,
	ProvisionStamp,
} from "../../scripts/package-provisioner.mjs";

/** The package clone: this module lives at `<clone>/pi/extensions/`. */
export const repoRoot = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

export default function (pi: ExtensionAPI) {
	pi.on("session_start", async (_event, ctx) => {
		const guard = new ProvisioningGuard(
			new ProvisionStamp(repoRoot),
			new PackageProvisioner(repoRoot),
		);
		guard.run((failure) => {
			// Another session holds the installer lock and is provisioning.
			if (failure === undefined) return;
			ctx.ui.notify(
				failure === null
					? "pi-daemon: provisioned the daemon and helpers from this " +
						"package; /daemon-reload adopts a new daemon."
					: `pi-daemon: provisioning failed (${failure}); see ` +
						`${guard.logPath}.`,
				failure === null ? "info" : "warning");
		});
	});
}
