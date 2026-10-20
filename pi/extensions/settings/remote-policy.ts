/**
 * pi-daemon - effective remote-work guard policy.
 *
 * One responsibility: answer whether the remote-work guard is on,
 * resolving `PI_REMOTE_GUARD` and then `piDaemon.remoteGuard.enabled` on
 * every decision and never caching either. The guard is on by default:
 * ssh leaving this host is transport for artifacts, not for work.
 */

import { SettingsStore } from "./store.ts";

export class RemotePolicy {
	constructor(
		private readonly store: Pick<SettingsStore, "section"> =
			new SettingsStore(),
	) {}

	/** Whether the guard is enabled: `PI_REMOTE_GUARD` (any non-empty
	 *  value other than `off`) then the setting, then enabled. */
	enabled(): boolean {
		const env = process.env.PI_REMOTE_GUARD;
		if (env !== undefined && env !== "") return env !== "off";
		const guard = this.store.section().remoteGuard;
		if (!guard || typeof guard !== "object" || Array.isArray(guard)) {
			return true;
		}
		const value = (guard as Record<string, unknown>).enabled;
		return typeof value === "boolean" ? value : true;
	}
}
