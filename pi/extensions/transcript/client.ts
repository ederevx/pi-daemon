/**
 * pi-daemon control transport for the transcript tool.
 *
 * One authenticated JSONL request per connection: read the loopback
 * endpoint file, send the mandatory `hello`+token handshake, send the
 * command, read the single reply, close. Failures are returned as
 * `{ok:false, error}` values, never thrown, so a missing or busy daemon
 * degrades to a tool message instead of breaking the turn.
 */

import { readFileSync } from "node:fs";
import { createConnection } from "node:net";

import { endpointPath } from "../daemon.ts";

/** A control reply, or a locally synthesized failure. */
export type ControlResponse = Record<string, unknown>;

/** The requester the tool depends on; `ControlClient` is the real one
 *  and tests inject a stub. */
export interface ControlRequester {
	request(cmd: Record<string, unknown>): Promise<ControlResponse>;
}

/** The daemon's loopback endpoint as written by `RuntimeLayout`. */
interface Endpoint {
	host: string;
	port: number;
	token: string;
}

/** One-shot authenticated client of the daemon control socket. */
export class ControlClient implements ControlRequester {
	private readonly path: string;
	private readonly timeoutMs: number;

	constructor(path: string = endpointPath(), timeoutMs = 4000) {
		this.path = path;
		this.timeoutMs = timeoutMs;
	}

	/** Send one command and return its reply, or a failure value. */
	async request(cmd: Record<string, unknown>): Promise<ControlResponse> {
		const endpoint = this.readEndpoint();
		if (endpoint === null) return { ok: false, error: "no-daemon" };
		try {
			return await this.exchange(endpoint, cmd);
		} catch (error) {
			return {
				ok: false,
				error: "daemon-unavailable",
				message: error instanceof Error ? error.message : String(error),
			};
		}
	}

	/** Parse and validate the endpoint file; null when unusable. */
	private readEndpoint(): Endpoint | null {
		let raw: Endpoint;
		try {
			raw = JSON.parse(readFileSync(this.path, "utf8")) as Endpoint;
		} catch {
			return null;
		}
		if (typeof raw?.host !== "string" || typeof raw?.port !== "number") {
			return null;
		}
		return raw;
	}

	/** Perform the handshake and the command on one connection. */
	private exchange(
		endpoint: Endpoint,
		cmd: Record<string, unknown>,
	): Promise<ControlResponse> {
		return new Promise<ControlResponse>((resolve, reject) => {
			const socket = createConnection({
				host: endpoint.host,
				port: endpoint.port,
			});
			socket.setNoDelay(true);
			socket.setTimeout(this.timeoutMs);
			let buffer = "";
			let greeted = false;
			let settled = false;
			const finish = (value: ControlResponse): void => {
				if (settled) return;
				settled = true;
				socket.destroy();
				resolve(value);
			};
			const fail = (error: Error): void => {
				if (settled) return;
				settled = true;
				socket.destroy();
				reject(error);
			};
			socket.on("connect", () => {
				socket.write(
					JSON.stringify({ cmd: "hello", token: endpoint.token }) + "\n",
				);
			});
			socket.on("data", (chunk: Buffer) => {
				buffer += String(chunk);
				for (;;) {
					const index = buffer.indexOf("\n");
					if (index < 0) return;
					const line = buffer.slice(0, index);
					buffer = buffer.slice(index + 1);
					if (!line.trim()) continue;
					let message: ControlResponse;
					try {
						message = JSON.parse(line) as ControlResponse;
					} catch {
						fail(new Error("bad-control-line"));
						return;
					}
					if (!greeted) {
						if (message.ok !== true) {
							finish({ ok: false, error: "handshake-rejected" });
							return;
						}
						greeted = true;
						socket.write(JSON.stringify(cmd) + "\n");
						continue;
					}
					finish(message);
					return;
				}
			});
			socket.on("timeout", () => fail(new Error("daemon-timeout")));
			socket.on("error", (error: Error) => fail(error));
			socket.on("close", () => fail(new Error("daemon-closed")));
		});
	}
}
