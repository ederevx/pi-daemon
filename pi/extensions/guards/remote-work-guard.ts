/**
 * pi-daemon - the remote-work guard.
 *
 * One responsibility: refuse a shell command that uses ssh to do work,
 * because ssh from this session is for moving files only. Coordination
 * with another host goes through pi-teams (team_spawn/team_send) where
 * it is installed. It reads the command only and never runs it.
 *
 * The guard is a `tool_call` handler, so it also covers nested calls
 * (another tool issuing `bash` with `ctx.executeTool`) and `daemon_tasks
 * submit`, which would otherwise run the same command later. It stays
 * active with command offloading disabled. `PI_REMOTE_GUARD=off` or
 * `piDaemon.remoteGuard.enabled=false` disables it.
 */

import type {
	ExtensionAPI,
	ToolCallEvent,
	ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";

import { RemotePolicy } from "../settings/remote-policy.ts";
import {
	RemoteWorkClassifier,
	type RemoteVerdict,
} from "./remote-work-classifier.ts";

/** Owns the pi-daemon verdict policy and the refusal text. */
export class RemoteWorkGuard {
	/** Verdicts pi-daemon refuses: a remote shell and remote work.
	 *  Transfers, ssh queries, and tunnels stay allowed as transport. */
	static readonly BLOCKED: ReadonlySet<RemoteVerdict> = new Set([
		"ssh-shell", "ssh-exec",
	]);

	constructor(
		private readonly policy: Pick<RemotePolicy, "enabled"> =
			new RemotePolicy(),
		private readonly classifier: RemoteWorkClassifier =
			new RemoteWorkClassifier(),
	) {}

	register(pi: ExtensionAPI): void {
		pi.on("tool_call", (event) => this.gate(event));
	}

	/** Block the call when the command uses ssh for work. */
	gate(event: ToolCallEvent): ToolCallEventResult | undefined {
		if (!this.policy.enabled()) return undefined;
		const command = this.commandOf(event);
		if (command === undefined) return undefined;
		const verdict = this.classifier.classify(command)
			.find((value) => RemoteWorkGuard.BLOCKED.has(value));
		if (verdict === undefined) return undefined;
		return { block: true, reason: this.reason() };
	}

	/** The shell text a tool call would run, or undefined. */
	private commandOf(event: ToolCallEvent): string | undefined {
		const input = (event.input ?? {}) as Record<string, unknown>;
		if (event.toolName === "bash" || event.toolName === "powershell") {
			const command = input.command;
			return typeof command === "string" && command.trim() !== ""
				? command : undefined;
		}
		if (event.toolName !== "daemon_tasks" || input.action !== "submit") {
			return undefined;
		}
		const command = input.command;
		return typeof command === "string" && command.trim() !== ""
			? command : undefined;
	}

	private reason(): string {
		return "[pi-daemon] refused ssh remote work: ssh from this session " +
			"is for moving files only - use scp, sftp, rsync -e ssh, or a " +
			"cat/tar pipe. Delegate work on another host through pi-teams " +
			"(team_spawn/team_send) if installed. Not run locally or " +
			"submitted to the daemon. (PI_REMOTE_GUARD=off disables.)";
	}
}
