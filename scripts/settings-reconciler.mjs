#!/usr/bin/env node
/**
 * Reconcile pi settings so this package is installed exactly once.
 *
 * pi treats every `packages` entry as an independent package: a git pin
 * and a local-path install of the same checkout coexist as two entries,
 * both extensions load, and pi aborts startup with tool-conflict errors
 * ("Tool "bash" conflicts with .../offload.ts"). The documented practice
 * (see the project status record) is: with the git package pinned, keep
 * only it. This reconciler runs from postinstall after any pi-daemon
 * install or pinned-ref update and drops duplicate pi-daemon entries,
 * preferring the pin that matches the installed version.
 */

import {
	chmodSync,
	existsSync,
	readFileSync,
	renameSync,
	statSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

/** Owns the one rule: at most one packages entry may install the
 *  package, and the matching git pin wins. */
export class SettingsReconciler {
	/** @param settingsPath path of the pi settings.json to reconcile
	 *  @param version the version being installed (prefers its pin)
	 *  @param packageName directory name the package installs under */
	constructor(settingsPath, version, packageName) {
		this.settingsPath = settingsPath;
		this.version = version;
		this.packageName = packageName;
	}

	/** Default settings location, honoring pi's config-dir override. */
	static defaultPath() {
		const root = process.env.PI_CODING_AGENT_DIR
			|| join(homedir(), ".pi", "agent");
		return join(root, "settings.json");
	}

	/** Parsed settings, or null when absent or unreadable: a broken
	 *  settings file is never rewritten from here. */
	load() {
		if (!existsSync(this.settingsPath)) return null;
		try {
			return JSON.parse(readFileSync(this.settingsPath, "utf8"));
		} catch {
			return null;
		}
	}

	/** Whether a packages entry installs this package: a git pin or a
	 *  path whose final component is the package directory. */
	installsPackage(entry) {
		if (typeof entry !== "string") return false;
		if (entry.startsWith("git:")) return entry.includes(this.packageName);
		const last = entry.split(/[\\/]/).filter(Boolean).pop();
		return last === this.packageName;
	}

	/** Drops every pi-daemon entry but one. Returns the dropped entries,
	 *  empty when nothing needed changing. */
	reconcile() {
		const settings = this.load();
		if (!settings || !Array.isArray(settings.packages)) return [];
		const indexes = settings.packages
			.map((entry, index) => ({ entry, index }))
			.filter(({ entry }) => this.installsPackage(entry));
		if (indexes.length <= 1) return [];
		const keep = this.preferred(indexes);
		const dropped = indexes.filter((x) => x !== keep);
		settings.packages = settings.packages.filter(
			(_, index) => !dropped.some((x) => x.index === index));
		this.save(settings);
		return dropped.map((x) => x.entry);
	}

	/** The entry to keep: the pin matching the installed version, then
	 *  any git pin, then the first entry (dev-only local installs). */
	preferred(indexes) {
		const pinned = indexes.filter((x) => x.entry.startsWith("git:"));
		const mine = pinned.find(
			(x) => x.entry.endsWith(`@v${this.version}`));
		return mine ?? pinned[0] ?? indexes[0];
	}

	/** Writes settings back the scratch-pad way: a temp file in the
	 *  target directory, mode preserved, then moved over the original. */
	save(settings) {
		const dir = dirname(this.settingsPath);
		const tmp = join(dir, `settings.json.tmp-${process.pid}`);
		const mode = statSync(this.settingsPath).mode;
		writeFileSync(tmp, JSON.stringify(settings, null, 2) + "\n");
		chmodSync(tmp, mode);
		try {
			renameSync(tmp, this.settingsPath);
		} catch (err) {
			// Windows cannot rename over an existing file.
			if (existsSync(this.settingsPath)) unlinkSync(this.settingsPath);
			renameSync(tmp, this.settingsPath);
		}
	}
}
