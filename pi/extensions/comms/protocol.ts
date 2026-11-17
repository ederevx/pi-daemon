/**
 * pi-daemon — the agent-comms tool contract.
 *
 * The tools are a thin surface over `pi/bin/pi-comms`, the documented
 * operator CLI for the `agent-comms` service (docs/pi-services.md,
 * docs/rebuild.md). This module owns the request shapes, the CLI's exit
 * vocabulary, and the argv each tool produces; it never speaks the
 * socket profile itself.
 */

/** A mailbox address, as the service writes it on every envelope. */
export interface CommsAddress {
	/** The owning node; empty for this node's own agents. */
	nodeId?: string;
	agentId: string;
}

/** One envelope as a page carries it. Every field of the service's
 *  projection is kept, so a field added later is not lost by this
 *  reader. */
export interface CommsEnvelope {
	messageId: string;
	from: CommsAddress;
	to: CommsAddress;
	sequence: number;
	kind: string;
	payload: unknown;
	[key: string]: unknown;
}

/** The acknowledgement reply: the one message every delivered
 *  application envelope is answered with, sent back to the sender
 *  before the service acknowledgement is recorded. Protocol
 *  acknowledgement envelopes match it and carry no reply of their own. */
export const ACK_KIND = "ack";
export const ACK_TYPE = "pi-agent-comms-ack";
export const ACK_VERSION = 1;

/** The protocol acknowledgement test: reserved kind plus the exact
 *  payload shape, so ordinary application mail cannot become exempt
 *  merely by saying "ack". */
export function isProtocolAck(envelope: CommsEnvelope): boolean {
	if (envelope.kind !== ACK_KIND || !envelope.payload ||
			typeof envelope.payload !== "object") return false;
	const payload = envelope.payload as Record<string, unknown>;
	return payload.type === ACK_TYPE &&
		payload.version === ACK_VERSION &&
		typeof payload.messageId === "string";
}

/** `comms_send`: one direct message. */
export interface CommsSendParams {
	to: string;
	payload: unknown;
	/** The recipient's node. */
	node?: string;
	/** Run the send where that peer is, as this agent. */
	at?: string;
	kind?: string;
	requestId?: string;
}

/** `comms_wait`: hold for mail, bounded by the service's own timeout. */
export interface CommsWaitParams {
	after?: number;
	limit?: number;
	timeoutMs?: number;
	/** Wait on the mailbox kept for this agent there, not here. */
	node?: string;
}

/** `comms_replay`: read the mailbox from a cursor, or every page. */
export interface CommsReplayParams {
	after?: number;
	limit?: number;
	all?: boolean;
	/** Read the mailbox kept for this agent there, not here. */
	node?: string;
}

/** `comms_ack`: acknowledge delivered mail. */
export interface CommsAckParams {
	messageIds: string[];
	/** Acknowledge mail in the mailbox kept for this agent there. */
	node?: string;
}

/** `comms_spawn`: launch another agent through the daemon's launcher, on
 *  an explicit argv or on one task inside a fresh `pi`. */
export interface CommsSpawnParams {
	agentId?: string;
	requestId?: string;
	dir: string;
	argv?: string[];
	task?: string;
	/** Launch where that peer is; `dir` is then that node's own path. */
	node?: string;
}

/** `peers`: the peers this node is configured to reach. */
export interface CommsPeersParams {
	/** List the peers that node is configured to reach. */
	node?: string;
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
	/** `--at` is a global option, so it precedes the command name: the whole
	 *  command runs where that peer is and prints that peer's answer. */
	private static at(node?: string): string[] {
		return node ? [`--at=${node}`] : [];
	}

	/** A string payload travels through `--text` so it stays a string; any
	 *  other JSON value is the positional the CLI decodes as JSON. */
	static send(params: CommsSendParams & { requestId: string }): string[] {
		const argv = [...CommsArgv.at(params.at), "send", params.to];
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
		const argv = [...CommsArgv.at(params.node), "wait"];
		if (params.after !== undefined) argv.push(`--after=${params.after}`);
		if (params.limit !== undefined) argv.push(`--limit=${params.limit}`);
		if (params.timeoutMs !== undefined) {
			argv.push(`--timeout-ms=${params.timeoutMs}`);
		}
		return argv;
	}

	static replay(params: CommsReplayParams): string[] {
		const argv = [...CommsArgv.at(params.node), "replay"];
		if (params.after !== undefined) argv.push(`--after=${params.after}`);
		if (params.limit !== undefined) argv.push(`--limit=${params.limit}`);
		if (params.all) argv.push("--all");
		return argv;
	}

	static ack(params: CommsAckParams): string[] {
		return [...CommsArgv.at(params.node), "ack", ...params.messageIds];
	}

	static spawn(params: CommsSpawnParams): string[] {
		const argv = [...CommsArgv.at(params.node), "spawn"];
		if (params.agentId) argv.push(`--agent-id=${params.agentId}`);
		if (params.requestId) argv.push(`--request-id=${params.requestId}`);
		if (params.task !== undefined) argv.push(`--task=${params.task}`);
		argv.push(params.dir);
		if (params.task === undefined && params.argv) {
			argv.push("--", ...params.argv);
		}
		return argv;
	}

	static peers(node?: string): string[] {
		return [...CommsArgv.at(node), "peer", "list"];
	}
}
