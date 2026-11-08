/**
 * Control-endpoint discovery for the daemon extensions: an explicit
 * environment override, the live `session-host` descriptor, then the
 * legacy runtime path. Resolution happens at use, never at module load.
 *
 * The services-root and staleness rules mirror pi/lib/pi_services.py:
 * `$PI_SERVICES_DIR`, else `$XDG_RUNTIME_DIR/pi-services` on POSIX,
 * else `<tempdir>/pi-services` (Windows never uses XDG). A descriptor
 * is usable only while its protocol matches, its pid is alive, and its
 * endpoint file publishes dialable coordinates.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

const SERVICE = "session-host";
const PROTOCOL = "pi-pty-host/1";

interface Descriptor {
	version?: unknown;
	protocol?: unknown;
	pid?: unknown;
	endpoint_file?: unknown;
}

function isWindows(platform: string): boolean {
	return platform.startsWith("win");
}

/** The services root, with the platform injected the same way as
 *  pi_services.ServiceDirectory: an injected "win32" never reads XDG. */
export function servicesRoot(
	env: NodeJS.ProcessEnv = process.env,
	platform: string = process.platform,
): string {
	const explicit = env.PI_SERVICES_DIR;
	if (explicit) return explicit;
	if (!isWindows(platform) && env.XDG_RUNTIME_DIR) {
		return join(env.XDG_RUNTIME_DIR, "pi-services");
	}
	return join(tmpdir(), "pi-services");
}

/** Whether a descriptor's pid names a live process. Any probe error,
 *  including EPERM, counts as gone, matching ProcessControl.pid_alive.
 *  On POSIX a zombie is gone too. */
function pidAlive(pid: unknown): boolean {
	if (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0) {
		return false;
	}
	try {
		process.kill(pid, 0);
		if (process.platform === "win32") return true;
		try {
			const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
			return !stat.slice(stat.lastIndexOf(")") + 1).trimStart()
				.startsWith("Z");
		} catch {
			return true;
		}
	} catch {
		return false;
	}
}

/** Whether an endpoint file publishes dialable coordinates, matching
 *  ServiceDirectory._endpoint_reachable without dialing. */
function dialable(path: unknown): boolean {
	if (typeof path !== "string" || !path) return false;
	try {
		if (!statSync(path).isFile()) return false;
		const data = JSON.parse(readFileSync(path, "utf8")) as {
			host?: unknown;
			port?: unknown;
		};
		return Boolean(data?.host) && Boolean(data?.port);
	} catch {
		return false;
	}
}

/** The highest-version live descriptor's endpoint under `root`, or
 *  undefined when none is usable. Stale descriptors are left in place:
 *  only the provider sweeps them. */
export function discoverEndpoint(root: string): string | undefined {
	const directory = join(root, SERVICE);
	let names: string[];
	try {
		names = readdirSync(directory).sort();
	} catch {
		return undefined;
	}
	const found: Array<{ version: number; endpoint: string }> = [];
	for (const name of names) {
		if (!name.endsWith(".json")) continue;
		let data: Descriptor;
		try {
			data = JSON.parse(
				readFileSync(join(directory, name), "utf8")) as Descriptor;
		} catch {
			continue;
		}
		if (!data || typeof data !== "object") continue;
		if (data.protocol !== PROTOCOL) continue;
		if (typeof data.version !== "number"
			|| !Number.isInteger(data.version)) {
			continue;
		}
		if (!pidAlive(data.pid) || !dialable(data.endpoint_file)) continue;
		found.push({
			version: data.version,
			endpoint: data.endpoint_file as string,
		});
	}
	found.sort((a, b) => b.version - a.version);
	return found[0]?.endpoint;
}

/** The pre-discovery runtime path, unchanged: XDG_RUNTIME_DIR first on
 *  every platform, then the Windows temp base, then the POSIX user
 *  runtime dir. */
function legacyEndpointPath(platform: string): string {
	if (process.env.XDG_RUNTIME_DIR) {
		return join(process.env.XDG_RUNTIME_DIR, "pi-pty-host.sock");
	}
	if (platform === "win32") {
		const base = process.env.TEMP || process.env.TMP || homedir();
		return join(base, "pi-daemon", "pi-pty-host.sock");
	}
	const uid = typeof process.getuid === "function" ? process.getuid() : 0;
	return join("/run/user", String(uid), "pi-pty-host.sock");
}

/** The control endpoint path: explicit, PI_DAEMON_ENDPOINT, the live
 *  session-host descriptor, then the legacy path. */
export function resolveEndpointPath(
	explicit?: string,
	platform: string = process.platform,
): string {
	if (explicit) return explicit;
	const override = process.env.PI_DAEMON_ENDPOINT;
	if (override) return override;
	return discoverEndpoint(servicesRoot(process.env, platform))
		?? legacyEndpointPath(platform);
}
