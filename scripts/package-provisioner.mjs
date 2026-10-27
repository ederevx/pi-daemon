#!/usr/bin/env node
/**
 * Provisioning for the files this package installs outside its clone.
 *
 * pi runs `npm install` — and with it `scripts/postinstall.mjs` — only when
 * a git checkout's HEAD moves, so a release tag that resolves to the commit
 * already checked out runs no provisioning hook at all. Provisioning
 * therefore has two triggers over one path: the postinstall on a fresh
 * install, and the session-start reconciliation in `pi/extensions/provision.ts`
 * whenever the clone no longer matches the sources `scripts/install.sh`
 * recorded in its manifest.
 */

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, openSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

/** Reads what the installer recorded and answers whether this clone still
 *  matches it; the installer owns the source-to-destination map, so only
 *  the recorded sources and their hashes are interpreted here. */
export class ProvisionStamp {
	/** @param repoRoot this package's clone
	 *  @param manifestPath the installer's manifest (default: the agent
	 *  home's) */
	constructor(repoRoot, manifestPath = ProvisionStamp.defaultManifest()) {
		this.repoRoot = repoRoot;
		this.manifestPath = manifestPath;
	}

	/** The agent home's manifest location, honoring pi's config override. */
	static defaultManifest() {
		const home = process.env.PI_CODING_AGENT_DIR
			|| join(homedir(), ".pi", "agent");
		return join(home, ".pi-daemon", "manifest.json");
	}

	/** True when the manifest is a package-mode record whose sources all
	 *  still hash as recorded; a missing, unreadable, manual, or older
	 *  manifest is stale, so it provisions once. */
	isCurrent() {
		let manifest;
		try {
			manifest = JSON.parse(readFileSync(this.manifestPath, "utf8"));
		} catch {
			return false;
		}
		const sources = manifest.sources;
		if (manifest.mode !== "package" || !sources || typeof sources !== "object") {
			return false;
		}
		return Object.entries(sources).every(([rel, sha]) => {
			try {
				const bytes = readFileSync(join(this.repoRoot, rel));
				return createHash("sha256").update(bytes).digest("hex") === sha;
			} catch {
				return false;
			}
		});
	}
}

/** Invokes the manual installer's package mode, which owns every
 *  destination path and the atomic replacement of each file. */
export class PackageProvisioner {
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

	/** The package-mode installer invocation, as an argv pair. */
	command() {
		return [this.shell(), [this.installer, "--package"]];
	}

	/** The fresh-install trigger: runs to completion with the enclosing
	 *  install's own streams, and never fails it — pi deletes a freshly
	 *  cloned package when `npm install` exits nonzero, so a provisioning
	 *  problem is a warning and the session-start reconciliation retries. */
	runSync() {
		const [shell, args] = this.command();
		const result = spawnSync(shell, args, {
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
		return 0;
	}
}

/** Reconciles provisioning at session start. A current clone costs one
 *  manifest read and a hash per recorded source; a stale one provisions in
 *  the background so the session still starts at once. */
export class ProvisioningGuard {
	/** @param stamp the clone-versus-manifest comparison
	 *  @param provisioner the installer invocation */
	constructor(stamp, provisioner) {
		this.stamp = stamp;
		this.provisioner = provisioner;
		this.logPath = join(dirname(stamp.manifestPath), "provision.log");
	}

	/** Provisions when stale and reports through `onDone`: null on success,
	 *  a reason on failure, and undefined when another session holds the
	 *  installer lock. Never throws. */
	run(onDone) {
		try {
			if (!existsSync(this.provisioner.installer) || this.stamp.isCurrent()) {
				return null;
			}
			const log = openSync(this.logPath, "w");
			const [shell, args] = this.provisioner.command();
			const child = spawn(shell, args, {
				cwd: this.provisioner.repoRoot,
				stdio: ["ignore", log, log],
				windowsHide: true,
			});
			child.on("error", (err) => onDone(err.message));
			child.on("exit", (code) => onDone(
				code === 0 ? null : code === 75 ? undefined : `install.sh exited ${code}`));
			return child;
		} catch (err) {
			onDone(String(err?.message ?? err));
			return null;
		}
	}
}
