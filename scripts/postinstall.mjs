#!/usr/bin/env node
/**
 * Provision a fresh pi package install to the manual paths.
 *
 * `pi install` runs `npm install` in the clone, which runs this script. It
 * invokes `scripts/install.sh --package` so the systemd unit, pi-rc client,
 * pi wrapper, daemon, and seam modules land in the same locations the
 * manual installer uses, while the extensions keep loading from the package
 * itself. pi skips `npm install` when an updated pin resolves to the commit
 * already checked out, so this hook only covers a fresh install: the
 * session-start reconciliation in `pi/extensions/provision.ts` provisions
 * every later update.
 *
 * This must never fail the enclosing `npm install`: pi's git installer
 * deletes a freshly cloned package when `npm install` exits nonzero, so a
 * provisioning problem is reported as a warning and the install still
 * succeeds. The manual installer remains available for a retry.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PackageProvisioner } from "./package-provisioner.mjs";
import { SettingsReconciler } from "./settings-reconciler.mjs";

/** Fails soft like the provisioner: a reconciliation problem is a
 *  warning, never an install failure. */
class SettingsGuard {
	constructor(version, checkoutRoot) {
		this.version = version;
		this.reconciler = new SettingsReconciler(
			SettingsReconciler.defaultPath(), version, "pi-daemon",
			checkoutRoot);
	}

	run() {
		let dropped;
		try {
			dropped = this.reconciler.reconcile();
		} catch (err) {
			process.stderr.write(
				`pi-daemon: settings reconciliation skipped (${err?.message ?? err})\n`,
			);
			return;
		}
		if (dropped.length > 0) {
			process.stderr.write(
				`pi-daemon: removed duplicate package entries that would load ` +
				`its extensions twice: ${dropped.join(", ")}\n`,
			);
		}
	}
}

const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));

const status = new PackageProvisioner(repoRoot).runSync();
new SettingsGuard(
	JSON.parse(readFileSync(join(repoRoot, "package.json"), "utf8")).version,
	repoRoot,
).run();
process.exit(status);
