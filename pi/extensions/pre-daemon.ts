/**
 * pi-daemon onboarding: the `pre_daemon` catalog tool.
 *
 * The daemon and offload tools are useless to a model that does not know
 * they exist or how they behave, and the surfaces that used to say so
 * (prompt snippets, guidelines, long tool descriptions) bloated every
 * session's prompt. `pre_daemon` now owns that knowledge: one call
 * returns the tool catalog, the conventions, and the feature summary.
 *
 * It is also the gate: until `pre_daemon` has been called in a session,
 * every tool this extension registers is blocked with a reason pointing
 * here. Non-extension tools are never touched. The gate resets on each
 * session start. Tool rows use pi's native collapsed rendering, so the
 * extension blends in; Ctrl+O expands.
 */

import type {
	ExtensionAPI,
	ToolCallEvent,
	ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

/** The name of this extension's onboarding tool. */
const TOOL_NAME = "pre_daemon";

/** Owns the `pre_daemon` tool, its first-call gate, and the collapse
 *  default. One instance per extension runtime. */
export class PreDaemonTool {
	private acknowledged = false;

	/** The daemon tools this extension registers and therefore gates.
	 *  `bash` is excluded: the offload override is transparent, so it
	 *  must keep behaving like the built-in shell. `pre_daemon` is the
	 *  way in and is never gated. */
	private readonly owned = new Set([
		"daemon_tasks",
		"daemon_gc_reap",
		"transcript_read",
	]);

	/** Register the tool, the per-session reset, and the gate. */
	register(pi: ExtensionAPI): void {
		pi.registerTool({
			name: TOOL_NAME,
			label: TOOL_NAME,
			description:
				"Call this once before using any pi-daemon tool in a " +
				"session. Returns the pi-daemon tool catalog, conventions, " +
				"and feature summary.",
			parameters: Type.Object({}),
			annotations: { readOnlyHint: true },
			execute: async () => {
				this.acknowledged = true;
				return {
					content: [{ type: "text", text: this.catalog() }],
					details: { tools: [...this.owned, TOOL_NAME] },
				};
			},
		});
		pi.on("session_start", async () => {
			this.beginSession();
		});
		pi.on("tool_call", (event) => this.gate(event));
	}

	/** Model-facing catalog: tools, conventions, and features. */
	catalog(): string {
		return [
			"pre_daemon - pi-daemon catalog (call again any time)",
			"",
			"Tools:",
			"- pre_daemon: this catalog; call once before any daemon/offload tool.",
			"- bash: every shell command runs as a daemon-owned ticket (full result in one go).",
			"- daemon_tasks: submit/status/result/watch/cancel/remove/reset/list tickets.",
			"- daemon_gc_reap: reap this hosted session when the daemon requests it.",
			"- transcript_read: read this conversation from the daemon index (stat/path/entries/tree/range) instead of loading the whole session file.",
			"",
			"Conventions:",
			"- Call pre_daemon once per session before using daemon_tasks, daemon_gc_reap, or transcript_read; bash stays transparent and is offloaded automatically.",
			"- Prefer daemon_tasks submit for builds, tests, downloads and other long-running commands: you keep working and the full result is delivered when the task finishes.",
			"- A blocking result wait is interruptible and steerable: Escape or a queued message releases it; the ticket keeps running and still delivers on completion.",
			"- Call daemon_gc_reap only when pi-daemon asks this idle detached session to reap, or when you are deliberately done.",
			"",
			"Features:",
			"- Hosted sessions run headless in the pi-daemon and survive terminal exit, SSH logout, and reboot; /bg detaches or hands over, pi-rc attach resumes.",
			"- Offloaded commands become daemon-owned tickets that outlive the submitting session and persist in tickets.json.",
			"- Remote-control helpers: pi-rc attach/ls/state, /daemon-reload, /daemon-settings, /daemon-purge.",
			"- transcript_read serves the daemon's positional index, so long conversations can be paged instead of re-read in full.",
			"- If the daemon is unreachable, bash falls back to the local shell transparently.",
		].join("\n");
	}

	/** True only for the tools this extension registers. */
	owns(name: string): boolean {
		return this.owned.has(name);
	}

	/** Reset per-session acknowledgement. */
	private beginSession(): void {
		this.acknowledged = false;
	}

	/** Block this extension's tools until `pre_daemon` has been called;
	 *  never touch any other tool. */
	private gate(event: ToolCallEvent): ToolCallEventResult | undefined {
		const name = String(event.toolName ?? "");
		if (name === TOOL_NAME) {
			this.acknowledged = true;
			return undefined;
		}
		if (!this.acknowledged && this.owns(name)) {
			return {
				block: true,
				reason:
					"Call pre_daemon first: it returns the pi-daemon " +
					"tool catalog and conventions. Then retry this call.",
			};
		}
		return undefined;
	}
}

export default function (pi: ExtensionAPI) {
	new PreDaemonTool().register(pi);
}
