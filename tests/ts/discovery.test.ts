/**
 * pi-daemon — endpoint discovery tests.
 *
 * The resolver is filesystem-only: services root, descriptor staleness,
 * version order, explicit override, and the legacy fallback, driven in
 * scratch temp dirs.
 */

import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createServer, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test, assert, assertEq, withEnv, scratchDir } from "./harness.ts";
import {
	servicesRoot,
	discoverEndpoint,
	resolveEndpointPath,
} from "../../pi/extensions/services/discovery.ts";
import { endpointPath, HandoverHold } from "../../pi/extensions/daemon.ts";

function endpointFile(path: string, coordinates: object = {
	host: "127.0.0.1", port: 1, token: "t",
}): string {
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, JSON.stringify(coordinates));
	return path;
}

function descriptor(
	root: string,
	provider: string,
	fields: Record<string, unknown>,
): string {
	const directory = join(root, "session-host");
	mkdirSync(directory, { recursive: true });
	const path = join(directory, `${provider}.json`);
	writeFileSync(path, JSON.stringify(fields));
	return path;
}

function baseFields(endpoint: string): Record<string, unknown> {
	return {
		service: "session-host",
		version: 1,
		protocol: "pi-pty-host/1",
		provider: "pi-daemon",
		pid: process.pid,
		endpoint_file: endpoint,
		activation: { kind: "exec", argv: ["pi-rc", "daemon-start"] },
	};
}

test("discovery: services root mirrors pi_services", () => {
	const runtime = join(scratchDir(), "root-runtime");
	assertEq(servicesRoot({ PI_SERVICES_DIR: "/svc" }, "linux"), "/svc");
	assertEq(servicesRoot({ XDG_RUNTIME_DIR: runtime }, "linux"),
		join(runtime, "pi-services"));
	assertEq(servicesRoot({ XDG_RUNTIME_DIR: runtime }, "win32"),
		join(tmpdir(), "pi-services"));
	assertEq(servicesRoot({}, "linux"), join(tmpdir(), "pi-services"));
	assertEq(servicesRoot({}, "win32"), join(tmpdir(), "pi-services"));
});

test("discovery: highest live version wins", () => {
	const root = join(scratchDir(), "highest");
	const older = endpointFile(join(scratchDir(), "older-endpoint.json"));
	const newer = endpointFile(join(scratchDir(), "newer-endpoint.json"));
	descriptor(root, "older", { ...baseFields(older), version: 1 });
	descriptor(root, "newer", { ...baseFields(newer), version: 3 });
	assertEq(discoverEndpoint(root), newer);
});

test("discovery: stale descriptors are skipped", () => {
	const root = join(scratchDir(), "stale");
	const live = endpointFile(join(scratchDir(), "live-endpoint.json"));
	const missing = join(scratchDir(), "never-written.json");
	const empty = endpointFile(join(scratchDir(), "empty-endpoint.json"), {});
	const deadEndpoint = endpointFile(
		join(scratchDir(), "dead-endpoint.json"));
	const protocolEndpoint = endpointFile(
		join(scratchDir(), "protocol-endpoint.json"));
	// A reaped child: its pid is dead, never a process this test could
	// accidentally probe as alive.
	const dead = spawnSync(process.execPath, ["-e", ""]);
	assertEq(dead.status, 0, "child reaped");
	const stale = [
		descriptor(root, "dead", { ...baseFields(deadEndpoint), version: 9,
			pid: dead.pid }),
		descriptor(root, "missing", { ...baseFields(missing), version: 8 }),
		descriptor(root, "empty", { ...baseFields(empty), version: 7 }),
		descriptor(root, "protocol", { ...baseFields(protocolEndpoint),
			version: 6, protocol: "pi-rpc/1" }),
		descriptor(root, "boolean", { ...baseFields(deadEndpoint),
			version: true }),
	];
	const malformed = join(root, "session-host", "malformed.json");
	writeFileSync(malformed, "{not json");
	stale.push(malformed);
	descriptor(root, "live", { ...baseFields(live), version: 4 });
	assertEq(discoverEndpoint(root), live);
	// Skipping is a read: only the provider's own sweep removes them.
	for (const path of stale) assert(existsSync(path), `stale kept: ${path}`);
});

test("discovery: explicit and environment overrides win", async () => {
	const root = join(scratchDir(), "override");
	const live = endpointFile(join(scratchDir(), "override-endpoint.json"));
	descriptor(root, "live", baseFields(live));
	await withEnv({ PI_SERVICES_DIR: root,
		PI_DAEMON_ENDPOINT: "/env/endpoint.json" }, () => {
		assertEq(resolveEndpointPath("/explicit/endpoint.json", "linux"),
			"/explicit/endpoint.json");
		assertEq(resolveEndpointPath(undefined, "linux"),
			"/env/endpoint.json");
	});
});

test("discovery: an empty root falls back to the legacy path", async () => {
	const root = join(scratchDir(), "empty");
	const runtime = join(scratchDir(), "legacy-runtime");
	await withEnv({ PI_SERVICES_DIR: root, PI_DAEMON_ENDPOINT: undefined,
		XDG_RUNTIME_DIR: runtime }, () => {
		assertEq(endpointPath("linux"), join(runtime, "pi-pty-host.sock"));
	});
});

test("discovery: legacy win32 keeps its own layout rule", async () => {
	await withEnv({ PI_SERVICES_DIR: join(scratchDir(), "win-empty"),
		PI_DAEMON_ENDPOINT: undefined, XDG_RUNTIME_DIR: undefined,
		TEMP: "C:\\Temp" }, () => {
		assertEq(endpointPath("win32"),
			join("C:\\Temp", "pi-daemon", "pi-pty-host.sock"));
	});
});

test("discovery: resolution happens at use, not at import", async () => {
	const root = join(scratchDir(), "lazy");
	const live = endpointFile(join(scratchDir(), "lazy-endpoint.json"));
	await withEnv({
		PI_SERVICES_DIR: root,
		PI_DAEMON_ENDPOINT: undefined,
		XDG_RUNTIME_DIR: join(scratchDir(), "lazy-runtime"),
	}, () => {
		const legacy = endpointPath("linux");
		descriptor(root, "late", baseFields(live));
		assertEq(endpointPath("linux"), live, "descriptor published later");
		assert(legacy !== live, "the first call saw no descriptor");
	});
});

test("discovery: HandoverHold reads the endpoint when it opens", async () => {
	const root = join(scratchDir(), "hold");
	const endpoint = join(scratchDir(), "hold-endpoint.json");
	const peers = new Set<Socket>();
	const server = createServer((socket) => {
		peers.add(socket);
		socket.once("close", () => peers.delete(socket));
		let buffer = "";
		socket.on("data", (chunk: Buffer) => {
			buffer += String(chunk);
			while (buffer.includes("\n")) {
				const index = buffer.indexOf("\n");
				const line = buffer.slice(0, index);
				buffer = buffer.slice(index + 1);
				if (!line.trim()) continue;
				JSON.parse(line);
				socket.write(JSON.stringify({ ok: true }) + "\n");
			}
		});
	});
	await new Promise<void>((resolve) =>
		server.listen(0, "127.0.0.1", resolve));
	try {
		const address = server.address() as { port: number };
		endpointFile(endpoint, {
			host: "127.0.0.1", port: address.port, token: "t",
		});
		await withEnv({
			PI_SERVICES_DIR: root,
			PI_DAEMON_ENDPOINT: undefined,
			XDG_RUNTIME_DIR: join(scratchDir(), "hold-runtime"),
		}, async () => {
			const hold = new HandoverHold();
			descriptor(root, "late", baseFields(endpoint));
			assertEq(await hold.open("/x/session.jsonl", "/x"), true);
		});
	} finally {
		for (const socket of peers) socket.destroy();
		await new Promise<void>((resolve, reject) => {
			server.close((error) => error ? reject(error) : resolve());
		});
	}
});
