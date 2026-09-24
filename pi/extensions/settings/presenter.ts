/**
 * pi-daemon — settings dock presenter.
 *
 * One responsibility: own the `piDaemon` setting rows and present them in
 * pi's two-column settings layout, persisting accepted changes through
 * SettingsStore. Every row shows the effective value (env var, then the
 * settings file, then the built-in default) and is marked `(env-pinned)`
 * when its environment variable is set, because the file edit cannot win.
 * Flag rows cycle in place; number, text, and roots rows open an editor
 * seeded with the current value and validate on submit. Non-TUI modes
 * print the same rows to stderr.
 */

import { ExtensionInputComponent } from "@earendil-works/pi-coding-agent";
import { delimiter, join } from "node:path";
import { homedir } from "node:os";
import type { Component } from "@earendil-works/pi-tui";
import type { ExtensionUIContext } from "@earendil-works/pi-coding-agent";
import { DaemonSettingsView } from "./view.ts";
import { SettingsStore } from "./store.ts";

/** The value shape a row edits, and how a submitted string is parsed. */
export type SettingKind = "flag" | "number" | "text" | "list";

/** One editable setting's identity and resolution data. */
export interface SettingSpec {
	id: string;
	label: string;
	description: string;
	key: string;
	kind: SettingKind;
	defaultValue: unknown;
	env: string;
	legacyEnv?: string;
	/** "off-word": any non-empty env value wins and only "off" disables
	 *  (the offload extension's PI_OFFLOAD contract). */
	envMode?: "off-word";
}

/** One rendered row: a flag cycles `values`, anything else has `submenu`. */
export interface SettingRow {
	id: string;
	label: string;
	description: string;
	value: string;
	values?: string[];
	submenu?: (currentValue: string, done: SubmenuDone) => Component;
}

/** The SettingsList submenu completion callback. */
export type SubmenuDone = (
	selectedValue?: string,
	options?: { navigateTo?: string },
) => void;

/** Reports an accepted in-place value change for one row. */
export type SettingsChange = (id: string, value: string) => void;

/** How the presenter surfaces validation errors. */
export type Notify = (
	message: string,
	kind: "info" | "warning" | "error",
) => void;

/** The subset of pi's Theme the view styles with. */
export interface ViewTheme {
	fg(color: "accent" | "muted" | "dim" | "border", text: string): string;
}

/** The result of applying one change: the row label, or an error. */
export type ApplyResult = { label: string } | { error: string };

/** Settings owned by the daemon: they take effect on its next restart. */
export class DaemonSettingsPresenter {
	constructor(private readonly store: SettingsStore = new SettingsStore()) {}

	/** Every piDaemon setting, in dock order. */
	specs(): SettingSpec[] {
		const agent = process.env.PI_CODING_AGENT_DIR
			|| join(homedir(), ".pi", "agent");
		return [
			this.number("idleReapHours", "Idle reap (h)",
				"End detached model-idle sessions after this many hours; 0 disables",
				"PI_PTYD_IDLE_REAP_HOURS", 12),
			this.number("idleWarnGraceHours", "Idle warning grace (h)",
				"Hours before the reap; delete the file to stay alive",
				"PI_PTYD_IDLE_WARN_HOURS", 1),
			this.number("daemonIdleTimeoutHours", "Daemon idle timeout (h)",
				"Self-shutdown after this many idle hours; 0 disables",
				"PI_DAEMON_IDLE_TIMEOUT_HOURS", 12),
			this.number("minReviveLifeSeconds", "Min revive life",
				"A hosted pi that lived shorter than this is never revived",
				"PI_PTYD_MIN_REVIVE_LIFE", 30),
			this.number("reloadGuardGraceSeconds", "Reload guard grace",
				"No-spawn guard after an in-place reload",
				"PI_PTYD_RELOAD_GUARD_GRACE", 60),
			this.number("reloadSignalGraceSeconds", "Reload signal grace",
				"Wait for an in-session reload signal before typing /reload",
				"PI_PTYD_RELOAD_SIGNAL_GRACE", 2),
			this.number("ticketTtlHours", "Ticket TTL (h)",
				"Finished tickets expire after this many hours",
				"PI_PTYD_TICKET_TTL_HOURS", 72),
			this.number("ticketGcSeconds", "Ticket GC tick",
				"Ticket garbage-collection cadence",
				"PI_PTYD_TICKET_GC", 60),
			{
				id: "extWatch", label: "Extension watch",
				description: "Watch extension roots and reload hosted sessions",
				key: "extWatch", kind: "flag", defaultValue: true,
				env: "PI_PTYD_EXT_WATCH",
			},
			this.number("extWatchIntervalSeconds", "Extension watch interval",
				"Extension-root poll cadence",
				"PI_PTYD_EXT_WATCH_INTERVAL", 3),
			this.number("extWatchDebounceSeconds", "Extension watch debounce",
				"Minimum gap between reload bursts",
				"PI_PTYD_EXT_WATCH_DEBOUNCE", 4),
			{
				id: "extWatchRoots", label: "Extension watch roots",
				description: `Roots to watch, joined by "${delimiter}"`,
				key: "extWatchRoots", kind: "list",
				defaultValue: [
					join(agent, "extensions"), join(agent, "npm"),
					join(agent, "git"), join(agent, "settings.json"),
				],
				env: "PI_PTYD_EXT_WATCH_ROOTS",
			},
			{
				id: "offload.enabled", label: "Offload enabled",
				description: "Enable bash offloading",
				key: "offload.enabled", kind: "flag", defaultValue: true,
				env: "PI_OFFLOAD", envMode: "off-word",
			},
			this.number("offload.waitSeconds", "Offload wait",
				"Hand-off bound before a command becomes a background ticket",
				"PI_OFFLOAD_WAIT", 120),
		];
	}

	/** The rows, each showing its effective value and env-pin marker. */
	rows(notify: Notify = DaemonSettingsPresenter.stderrNotify): SettingRow[] {
		return this.specs().map((spec) => this.row(spec, notify));
	}

	/** Persist one accepted row value, or report why it was rejected. */
	apply(id: string, value: string): ApplyResult {
		const spec = this.specs().find((candidate) => candidate.id === id);
		if (!spec) return { error: `unknown pi-daemon setting: ${id}` };
		const parsed = DaemonSettingsPresenter.parse(spec.kind, value);
		if (parsed === null) {
			return { error: `${spec.label}: ${DaemonSettingsPresenter.hint(spec.kind)}` };
		}
		try {
			this.store.set(spec.key, parsed);
		} catch (err) {
			return { error: err instanceof Error ? err.message : String(err) };
		}
		return { label: spec.label };
	}

	/** Present the rows in pi's settings view when a TUI is available,
	 *  otherwise list them on stderr. A render failure falls back to the
	 *  listing so the command never breaks a session. */
	async present(
		ui: ExtensionUIContext | undefined,
		mode: string | undefined,
		onChange: SettingsChange,
	): Promise<void> {
		const notify: Notify = ui?.notify
			? (message, kind) => ui.notify(message, kind)
			: DaemonSettingsPresenter.stderrNotify;
		const rows = this.rows(notify);
		if (mode === "tui" && typeof ui?.custom === "function") {
			try {
				await ui.custom((_tui, theme, _keybindings, done) =>
					new DaemonSettingsView(rows, theme, onChange,
						() => done(undefined)));
				return;
			} catch {
				// fall through to the stderr listing
			}
		}
		this.printRows(rows);
	}

	/** One row: flags expose cycle values, everything else an editor. */
	private row(spec: SettingSpec, notify: Notify): SettingRow {
		const base = {
			id: spec.id,
			label: this.pinned(spec) ? `${spec.label} (env-pinned)` : spec.label,
			description: spec.description,
			value: DaemonSettingsPresenter.display(spec.kind,
				this.effective(spec)),
		};
		if (spec.kind === "flag") return { ...base, values: ["off", "on"] };
		return {
			...base,
			submenu: (currentValue, done) =>
				this.editor(spec, currentValue, done, notify),
		};
	}

	/** The seeded text editor for one non-flag row. */
	private editor(
		spec: SettingSpec,
		currentValue: string,
		done: SubmenuDone,
		notify: Notify,
	): Component {
		const submit = (raw: string): void => {
			const parsed = DaemonSettingsPresenter.parse(spec.kind, raw);
			if (parsed === null) {
				notify(`${spec.label}: ${DaemonSettingsPresenter.hint(spec.kind)}`,
					"error");
				return;
			}
			done(DaemonSettingsPresenter.display(spec.kind, parsed));
		};
		return new ExtensionInputComponent(spec.label, spec.description, submit,
			() => done(undefined), { initialValue: currentValue });
	}

	/** The effective value: env, then the settings file, then a legacy
	 *  env var, then the built-in default, each parsed and validated. */
	private effective(spec: SettingSpec): unknown {
		const envRaw = this.envValue(spec);
		if (envRaw !== undefined) {
			if (spec.envMode === "off-word") return envRaw !== "off";
			const parsed = DaemonSettingsPresenter.parse(spec.kind, envRaw);
			if (parsed !== null) return parsed;
		}
		const stored = DaemonSettingsPresenter.lookup(this.store.section(),
			spec.key);
		if (stored !== undefined && stored !== "") {
			const parsed = DaemonSettingsPresenter.parse(spec.kind, stored);
			if (parsed !== null) return parsed;
		}
		const legacyRaw = this.legacyValue(spec);
		if (legacyRaw !== undefined) {
			const parsed = DaemonSettingsPresenter.parse(spec.kind, legacyRaw);
			if (parsed !== null) return parsed;
		}
		return spec.defaultValue;
	}

	/** Whether the row's environment variable currently wins. */
	private pinned(spec: SettingSpec): boolean {
		for (const raw of [this.envValue(spec), this.legacyValue(spec)]) {
			if (raw === undefined) continue;
			if (spec.envMode === "off-word") return true;
			if (DaemonSettingsPresenter.parse(spec.kind, raw) !== null) {
				return true;
			}
		}
		return false;
	}

	/** The spec's primary env value, undefined when unset or empty. */
	private envValue(spec: SettingSpec): string | undefined {
		const raw = process.env[spec.env];
		return raw === undefined || raw === "" ? undefined : raw;
	}

	/** The spec's legacy env value, undefined when absent or empty. */
	private legacyValue(spec: SettingSpec): string | undefined {
		if (!spec.legacyEnv) return undefined;
		const raw = process.env[spec.legacyEnv];
		return raw === undefined || raw === "" ? undefined : raw;
	}

	/** Print the rows to stderr for non-UI modes. */
	private printRows(rows: SettingRow[]): void {
		console.error("pi-daemon-settings:\n  "
			+ rows.map((row) => DaemonSettingsPresenter.formatRow(row))
				.join("\n  "));
	}

	/** A spec for a numeric row (the common case). */
	private number(
		id: string,
		label: string,
		description: string,
		env: string,
		defaultValue: number,
		legacyEnv?: string,
		key: string = id,
	): SettingSpec {
		return {
			id, label, description, key, kind: "number",
			defaultValue, env, legacyEnv,
		};
	}

	/** Parse a submitted or stored value, null when invalid. Numbers
	 *  must be finite and non-negative; text must be non-empty; lists
	 *  split on the platform path separator and drop empty entries. */
	static parse(kind: SettingKind, raw: unknown): unknown {
		if (kind === "flag") {
			if (typeof raw === "boolean") return raw;
			const text = String(raw).trim().toLowerCase();
			if (["1", "true", "yes", "on"].includes(text)) return true;
			if (["0", "false", "no", "off"].includes(text)) return false;
			return null;
		}
		if (kind === "number") {
			if (raw === null || raw === undefined || typeof raw === "boolean") {
				return null;
			}
			const value = Number(raw);
			return Number.isFinite(value) && value >= 0 ? value : null;
		}
		if (kind === "text") {
			return typeof raw === "string" && raw !== "" ? raw : null;
		}
		if (typeof raw === "string") {
			const items = raw.split(delimiter).filter(Boolean);
			return items.length > 0 ? items : null;
		}
		if (Array.isArray(raw)) {
			const items = raw.filter(
				(item) => typeof item === "string" && item !== "");
			return items.length > 0 ? items : null;
		}
		return null;
	}

	/** A parsed value rendered for display (and for the list editor). */
	static display(kind: SettingKind, value: unknown): string {
		if (kind === "flag") return value ? "on" : "off";
		if (kind === "list" && Array.isArray(value)) {
			return value.join(delimiter);
		}
		return String(value);
	}

	/** The validation message for an invalid submission. */
	static hint(kind: SettingKind): string {
		if (kind === "number") return "enter a finite number >= 0";
		if (kind === "text") return "enter a non-empty value";
		if (kind === "list") return `enter paths separated by "${delimiter}"`;
		return "enter on or off";
	}

	/** One row in the fallback listing's single-line layout. */
	static formatRow(row: SettingRow): string {
		return `${row.label}: ${row.description} — current: ${row.value}`;
	}

	/** Read a dotted key out of a nested object. */
	static lookup(
		section: Record<string, unknown>,
		key: string,
	): unknown {
		let cursor: unknown = section;
		for (const part of key.split(".")) {
			if (!cursor || typeof cursor !== "object" || Array.isArray(cursor)) {
				return undefined;
			}
			cursor = (cursor as Record<string, unknown>)[part];
		}
		return cursor;
	}

	/** The default error surface outside the TUI. */
	private static stderrNotify: Notify = (message) => {
		console.error(`pi-daemon-settings: ${message}`);
	};
}
