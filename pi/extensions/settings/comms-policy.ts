/**
 * pi-daemon — effective agent-comms policy.
 *
 * One responsibility: answer the presentation and reception tuning the
 * comms surface runs with, resolving the `piDaemon.comms` settings
 * section on every read and never caching it — the tools, the receiver
 * and the acknowledgement ledger are separate modules and must not
 * disagree about a setting that changed after one of them read it.
 */

import { SettingsStore } from "./store.ts";

/** The comms surface's effective settings. */
export interface CommsSettings {
	/** Off disables the whole surface: receiver, delivery and gate. */
	enabled: boolean;
	/** The collapsed one-line preview length. */
	previewChars: number;
	/** The foreground wait's tool-window update cadence. */
	waitUpdateMs: number;
	/** The background hold the receiver asks for; the service caps it. */
	holdMs: number;
	/** The receiver's first retry delay after a failing hold. */
	retryInitialMs: number;
	/** The receiver's retry-delay ceiling. */
	retryMaxMs: number;
}

/**
 * The floor each tunable stands on: a preview narrower than its own
 * ellipsis and header line cannot carry a collapsed payload, a wait
 * update faster than the TUI redraw budget only churns the window, a
 * receiver retry faster than the CLI's own failure cadence spins child
 * processes, and a retry ceiling below its initial step would never
 * bound anything.
 */
export const COMMS_FLOORS = {
	previewChars: 12,
	waitUpdateMs: 50,
	retryInitialMs: 100,
	retryMaxMs: 1000,
} as const;

const DEFAULTS: CommsSettings = {
	enabled: true,
	previewChars: 96,
	waitUpdateMs: 250,
	holdMs: 60000,
	retryInitialMs: 1000,
	retryMaxMs: 30000,
};

export class CommsPolicy {
	constructor(
		private readonly store: Pick<SettingsStore, "section"> = new SettingsStore(),
	) {}

	/** The effective settings: `piDaemon.comms.*`, defaulting missing or
	 *  invalid values so a partial section degrades field by field. */
	settings(): CommsSettings {
		const comms = this.store.section().comms;
		const values = comms && typeof comms === "object"
			&& !Array.isArray(comms)
			? comms as Record<string, unknown>
			: {};
		const number = (value: unknown, fallback: number): number =>
			typeof value === "number" && Number.isFinite(value) && value >= 0
				? value
				: fallback;
		return {
			enabled: typeof values.enabled === "boolean"
				? values.enabled
				: DEFAULTS.enabled,
			previewChars: Math.max(
				number(values.previewChars, DEFAULTS.previewChars),
				COMMS_FLOORS.previewChars),
			waitUpdateMs: Math.max(
				number(values.waitUpdateMs, DEFAULTS.waitUpdateMs),
				COMMS_FLOORS.waitUpdateMs),
			holdMs: number(values.holdMs, DEFAULTS.holdMs),
			retryInitialMs: Math.max(
				number(values.retryInitialMs, DEFAULTS.retryInitialMs),
				COMMS_FLOORS.retryInitialMs),
			retryMaxMs: Math.max(
				number(values.retryMaxMs, DEFAULTS.retryMaxMs),
				COMMS_FLOORS.retryMaxMs),
		};
	}
}
