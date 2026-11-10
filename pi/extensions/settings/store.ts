/**
 * pi-daemon — settings file store.
 *
 * One responsibility: read and atomically write the `piDaemon` namespace
 * of pi's settings.json. The agent directory is resolved per call (never
 * cached), so tests and one-off runs can redirect it through
 * PI_CODING_AGENT_DIR. A file that is not valid settings JSON is reported
 * as unreadable and never overwritten.
 */

import {
	chmodSync,
	existsSync,
	mkdirSync,
	readFileSync,
	renameSync,
	statSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

/** The top-level settings namespace this store owns. */
export const DAEMON_NAMESPACE = "piDaemon";

export class SettingsStore {
	constructor(private readonly namespace: string = DAEMON_NAMESPACE) {}

	/** The settings file path, resolved now (honors PI_CODING_AGENT_DIR). */
	path(): string {
		const root = process.env.PI_CODING_AGENT_DIR
			|| join(homedir(), ".pi", "agent");
		return join(root, "settings.json");
	}

	/** The parsed settings object, {} when absent, null when unreadable. */
	load(): Record<string, unknown> | null {
		const file = this.path();
		if (!existsSync(file)) return {};
		try {
			const parsed = JSON.parse(readFileSync(file, "utf8"));
			if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
				return null;
			}
			return parsed as Record<string, unknown>;
		} catch {
			return null;
		}
	}

	/** The `piDaemon` section, {} when absent or unreadable. */
	section(): Record<string, unknown> {
		const settings = this.load();
		const value = settings ? settings[this.namespace] : undefined;
		return value && typeof value === "object" && !Array.isArray(value)
			? value as Record<string, unknown> : {};
	}

	/** Set one key, dotted for nested objects, preserving every other
	 *  key. Throws, writing nothing, when the file cannot be parsed. */
	set(keyPath: string, value: unknown): void {
		const settings = this.load();
		if (settings === null) {
			throw new Error(`${this.path()} is not valid settings JSON; ` +
				"refusing to overwrite it");
		}
		const keys = keyPath.split(".");
		let cursor = this.ensureObject(settings, this.namespace);
		for (let index = 0; index < keys.length - 1; index++) {
			cursor = this.ensureObject(cursor, keys[index]);
		}
		cursor[keys[keys.length - 1]] = value;
		this.save(settings);
	}

	/** Delete the whole `piDaemon` namespace, preserving every other
	 *  top-level key, the file mode, and the atomic write path. Throws,
	 *  writing nothing, when the file cannot be parsed. */
	reset(): void {
		const settings = this.load();
		if (settings === null) {
			throw new Error(`${this.path()} is not valid settings JSON; ` +
				"refusing to overwrite it");
		}
		delete settings[this.namespace];
		this.save(settings);
	}

	/** The named child object, replaced when absent or not an object. */
	private ensureObject(
		parent: Record<string, unknown>,
		key: string,
	): Record<string, unknown> {
		const value = parent[key];
		const child = value && typeof value === "object"
			&& !Array.isArray(value)
			? value as Record<string, unknown> : {};
		parent[key] = child;
		return child;
	}

	/** Atomic write: a uniquely named sibling temporary renamed over the
	 *  original, so a reader never sees a partial file. A replacement that
	 *  cannot land is reported and the temporary discarded: the original
	 *  is never deleted to make room for a write that may still fail. */
	private save(settings: Record<string, unknown>): void {
		const file = this.path();
		const dir = dirname(file);
		mkdirSync(dir, { recursive: true });
		const tmp = join(dir,
			`settings.json.tmp-${process.pid}-${randomUUID()}`);
		const mode = existsSync(file) ? statSync(file).mode : undefined;
		try {
			writeFileSync(tmp, JSON.stringify(settings, null, 2) + "\n",
				{ flag: "wx" });
			if (mode !== undefined) chmodSync(tmp, mode);
			renameSync(tmp, file);
		} catch (error) {
			this.discard(tmp);
			throw error;
		}
	}

	/** Remove a temporary that never became the target. */
	private discard(tmp: string): void {
		try {
			unlinkSync(tmp);
		} catch {
			// Nothing to remove: the write never created it.
		}
	}
}
