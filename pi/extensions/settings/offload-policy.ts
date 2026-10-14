/**
 * pi-daemon — effective command-offloading policy.
 *
 * One responsibility: answer whether command offloading is on, resolving
 * `PI_OFFLOAD` and then the `piDaemon.offload.enabled` setting on every
 * decision and never caching either. The offload backend and the
 * onboarding gate are separate extensions, so they must not disagree
 * about a setting that changed after one of them read it; a cached copy
 * in a module they both import cannot make that guarantee.
 */

import { SettingsStore } from "./store.ts";

export class OffloadPolicy {
	constructor(
		private readonly store: Pick<SettingsStore, "section"> = new SettingsStore(),
	) {}

	/** Whether offloading is enabled: `PI_OFFLOAD` (any non-empty value
	 *  other than `off`) then the setting, then enabled. */
	enabled(): boolean {
		const env = process.env.PI_OFFLOAD;
		if (env !== undefined && env !== "") return env !== "off";
		const offload = this.store.section().offload;
		if (!offload || typeof offload !== "object" || Array.isArray(offload)) {
			return true;
		}
		const value = (offload as Record<string, unknown>).enabled;
		return typeof value === "boolean" ? value : true;
	}
}
