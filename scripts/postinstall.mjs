#!/usr/bin/env node
/**
 * Provision a pi package install to the manual paths.
 *
 * `pi install` (and a pinned-ref update) runs `npm install` in the clone,
 * which runs this script. It invokes `scripts/install.sh --package` so the
 * systemd unit, pi-rc client, pi wrapper, daemon, and seam modules land in
 * the same locations the manual installer uses, while the extensions keep
 * loading from the package itself.
 *
 * This must never fail the enclosing `npm install`: pi's git installer
 * deletes a freshly cloned package when `npm install` exits nonzero, so a
 * provisioning problem is reported as a warning and the install still
 * succeeds. The manual installer remains available for a retry.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { SettingsReconciler } from "./settings-reconciler.mjs";

/** Runs the manual installer in its package mode and owns the fail-soft
 *  reporting that keeps npm's install step from aborting. */
class PackageProvisioner {
	constructor(repoRoot) {
		this.repoRoot = repoRoot;
		this.installer = join(repoRoot, "scripts", "install.sh");
	}

	/** The bash the installer needs: an explicit override, /bin/bash on
	 *  POSIX, else `bash` from PATH (Git Bash on Windows). */
	shell() {
		if (process.env.PI_DAEMON_BASH) return process.env.PI_DAEMON_BASH;
		if (process.platform !== "win32" && existsSync("/bin/bash")) {
			return "/bin/bash";
		}
		return "bash";
	}

	run() {
		const result = spawnSync(this.shell(), [this.installer, "--package"], {
			cwd: this.repoRoot,
			stdio: "inherit",
		});
		if (result.error || result.status !== 0) {
			const reason = result.error
				? result.error.message
				: `install.sh exited ${result.status}`;
			process.stderr.write(
				`pi-daemon: package provisioning skipped (${reason}); ` +
				`run "bash scripts/install.sh" manually.\n`,
			);
		}
		// Always succeed: a nonzero exit makes pi delete the fresh clone.
		return 0;
	}
}

/** Fails soft like the provisioner: a reconciliation problem is a
 *  warning, never an install failure. */
class SettingsGuard {
	constructor(version) {
		this.version = version;
		this.reconciler = new SettingsReconciler(
			SettingsReconciler.defaultPath(), version, "pi-daemon");
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

const provisioner = new PackageProvisioner(repoRoot);
const status = provisioner.run();
new SettingsGuard(
	JSON.parse(readFileSync(join(repoRoot, "package.json"), "utf8")).version,
).run();
process.exit(status);
process.exit(new PackageProvisioner(repoRoot).run());