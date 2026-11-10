/**
 * pi-daemon — the agent-comms tool contract.
 *
 * The tools are a thin surface over `pi/bin/pi-comms`, the documented
 * operator CLI for the `agent-comms` service (docs/pi-services.md,
 * docs/rebuild.md). This module owns the request shapes, the CLI's exit
 * vocabulary, and the argv each tool produces; it never speaks the
 * socket profile itself.
 */

/** `comms_send`: one direct message. */
export interface CommsSendParams {
	to: string;
	payload: unknown;
	node?: string;
	kind?: string;
	requestId?: string;
}

/** `comms_wait`: hold for mail, bounded by the service's own timeout. */
export interface CommsWaitParams {
	after?: number;
	limit?: number;
	timeoutMs?: number;
}

/** `comms_replay`: read the mailbox from a cursor, or every page. */
export interface CommsReplayParams {
	after?: number;
	limit?: number;
	all?: boolean;
}

/** `comms_ack`: acknowledge delivered mail. */
export interface CommsAckParams {
	messageIds: string[];
}

/** `comms_spawn`: launch another agent through the daemon's launcher, on
 *  an explicit argv or on one task inside a fresh `pi`. */
export interface CommsSpawnParams {
	agentId?: string;
	requestId?: string;
	dir: string;
	argv?: string[];
	task?: string;
}

/** The CLI's exit codes; they are the failure vocabulary of the profile. */
export class CommsExit {
	static readonly OK = 0;
	static readonly USAGE = 1;
	static readonly DECLINED = 2;
	static readonly TIMED_OUT = 3;
	static readonly NO_SERVICE = 4;
	static readonly RPC_ERROR = 5;
	static readonly UNAUTHORIZED = 6;
	static readonly AMBIGUOUS = 7;

	/** A human label for one exit code. */
	static label(code: number | null): string {
		switch (code) {
			case CommsExit.OK: return "ok";
			case CommsExit.USAGE: return "usage";
			case CommsExit.DECLINED: return "declined";
			case CommsExit.TIMED_OUT: return "empty wait";
			case CommsExit.NO_SERVICE: return "no service";
			case CommsExit.RPC_ERROR: return "remote error";
			case CommsExit.UNAUTHORIZED: return "unauthorized";
			case CommsExit.AMBIGUOUS: return "ambiguous";
			default: return "unknown";
		}
	}
}

/** The argv for each command, without the global `--format json` prefix. */
export class CommsArgv {
	/** A string payload travels through `--text` so it stays a string; any
	 *  other JSON value is the positional the CLI decodes as JSON. */
	static send(params: CommsSendParams & { requestId: string }): string[] {
		const argv = ["send", params.to];
		if (params.node) argv.push(`--node=${params.node}`);
		if (params.kind) argv.push(`--kind=${params.kind}`);
		argv.push(`--request-id=${params.requestId}`);
		if (typeof params.payload === "string") {
			argv.push(`--text=${params.payload}`);
		} else {
			argv.push(JSON.stringify(params.payload));
		}
		return argv;
	}

	static wait(params: CommsWaitParams): string[] {
		const argv = ["wait"];
		if (params.after !== undefined) argv.push(`--after=${params.after}`);
		if (params.limit !== undefined) argv.push(`--limit=${params.limit}`);
		if (params.timeoutMs !== undefined) {
			argv.push(`--timeout-ms=${params.timeoutMs}`);
		}
		return argv;
	}

	static replay(params: CommsReplayParams): string[] {
		const argv = ["replay"];
		if (params.after !== undefined) argv.push(`--after=${params.after}`);
		if (params.limit !== undefined) argv.push(`--limit=${params.limit}`);
		if (params.all) argv.push("--all");
		return argv;
	}

	static ack(params: CommsAckParams): string[] {
		return ["ack", ...params.messageIds];
	}

	static spawn(params: CommsSpawnParams): string[] {
		const argv = ["spawn"];
		if (params.agentId) argv.push(`--agent-id=${params.agentId}`);
		if (params.requestId) argv.push(`--request-id=${params.requestId}`);
		if (params.task !== undefined) argv.push(`--task=${params.task}`);
		argv.push(params.dir);
		if (params.task === undefined && params.argv) {
			argv.push("--", ...params.argv);
		}
		return argv;
	}

	static peers(): string[] {
		return ["peer", "list"];
	}
}
