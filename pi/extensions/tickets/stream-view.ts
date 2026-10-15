/**
 * pi-daemon — the TUI view of a live `daemon_tasks` result.
 *
 * A partial result draws every stream as a marker band - a `▌` glyph,
 * the ticket id, its state and command - with that ticket's output
 * indented beneath it, a fair share of the tail each, so several tickets
 * stay visible at once and output cannot be mistaken for a marker. A
 * final result reproduces pi's default tool renderer exactly (the first
 * ten lines, then an expand hint) because a throwing renderer falls back
 * to that and the fallback must not depend on us. Used only by the
 * interactive renderer: print and rpc modes never call it, so the
 * model-facing result text is untouched.
 */

import {
	keyHint,
	type AgentToolResult,
	type Theme,
	type ToolRenderResultOptions,
} from "@earendil-works/pi-coding-agent";
import { type Component, Text } from "@earendil-works/pi-tui";
import type { StreamDetail, TicketState } from "./ticket-feed.ts";

/** pi's collapsed result preview: FALLBACK_PREVIEW_LINES in its
 *  interactive tool-execution component. */
const FALLBACK_PREVIEW_LINES = 10;
/** Lines the live view shares across every stream. */
const PARTIAL_LINES = 12;
/** Streams the view draws before folding the rest onto a summary line. */
const MAX_STREAMS = 8;
const COMMAND_CHARS = 60;

type OutcomeRole = "error" | "warning" | "success" | "dim";

/** The part of pi's render context this view reads. */
export interface StreamViewSlot {
	lastComponent?: unknown;
}

/** The outcome color shared with the delivery card: error for a nonzero
 *  exit or a failure, warning for cancelled or lost, success for a
 *  clean finish, and dim while running. */
export function outcomeColor(state: TicketState, exit: number | null): OutcomeRole {
	if (state === "failed" || (exit !== null && exit !== 0)) return "error";
	if (state === "cancelled" || state === "lost") return "warning";
	if (state === "done") return "success";
	return "dim";
}

export class TicketStreamView {
	/** Render one result into a reusable Text component. */
	render(
		result: AgentToolResult<unknown>,
		options: ToolRenderResultOptions,
		theme: Theme,
		slot: StreamViewSlot,
	): Component {
		const component = slot.lastComponent instanceof Text
			? slot.lastComponent
			: new Text("", 0, 0);
		component.setText(this.body(result, options, theme));
		return component;
	}

	private body(
		result: AgentToolResult<unknown>,
		options: ToolRenderResultOptions,
		theme: Theme,
	): string {
		const streams = (result.details as { streams?: StreamDetail[] } | undefined)
			?.streams;
		if (options.isPartial && streams && streams.length > 0) {
			return this.partial(streams, options.expanded, theme);
		}
		return this.final(result, options.expanded, theme);
	}

	/** Every stream as a marker band plus its share of the tail, the
	 *  output indented under its own marker; the hint rides the first
	 *  header so it never costs a section its line. */
	private partial(
		streams: readonly StreamDetail[],
		expanded: boolean,
		theme: Theme,
	): string {
		const rendered = streams.slice(0, MAX_STREAMS);
		const per = expanded
			? Infinity
			: Math.max(1, Math.floor(PARTIAL_LINES / rendered.length) - 1);
		const out: string[] = [];
		let hidden = streams.length > MAX_STREAMS;
		for (const stream of rendered) {
			out.push([
				theme.fg("dim", "▌"),
				theme.fg("accent", theme.bold(stream.id)),
				theme.fg(outcomeColor(stream.state, stream.exit), this.stateLabel(stream)),
				theme.fg("dim", stream.command.slice(0, COMMAND_CHARS)),
			].join(" "));
			const lines = liveLines(stream.tail);
			const shown = per === Infinity ? lines : lines.slice(-per);
			if (shown.length < lines.length || stream.behind) hidden = true;
			for (const line of shown) out.push(`  ${theme.fg("toolOutput", line)}`);
		}
		if (streams.length > MAX_STREAMS) {
			const extra = streams.slice(MAX_STREAMS)
				.map((stream) => `${stream.id}(${stream.state})`);
			out.push(theme.fg("muted", `+${extra.length} more: ${extra.join(", ")}`));
		}
		if (hidden && !expanded) {
			out[0] += ` ${theme.fg("muted", "(")}` +
				`${keyHint("app.tools.expand", "to expand")}${theme.fg("muted", ")")}`;
		}
		return out.join("\n");
	}

	private stateLabel(stream: StreamDetail): string {
		switch (stream.state) {
			case "running": {
				const base = `running ${Math.round(stream.runtimeSeconds)}s`;
				return stream.behind ? `${base} ... catching up` : base;
			}
			case "done":
				return stream.exit !== null ? `done exit ${stream.exit}` : "done";
			case "failed":
				return stream.exit !== null ? `failed exit ${stream.exit}` : "failed";
			case "cancelled":
				return "cancelled";
			case "lost":
				return "lost";
		}
	}

	/** pi's default: the first ten lines, then an expand hint naming the
	 *  lines left. Expanded shows the whole result. */
	private final(
		result: AgentToolResult<unknown>,
		expanded: boolean,
		theme: Theme,
	): string {
		const lines = plainText(result).split("\n");
		const shown = expanded ? lines : lines.slice(0, FALLBACK_PREVIEW_LINES);
		const remaining = lines.length - shown.length;
		let text = shown.map((line) => theme.fg("toolOutput", line)).join("\n");
		if (remaining > 0) {
			text += `${theme.fg("muted", `\n... (${remaining} more lines,`)} ` +
				`${keyHint("app.tools.expand", "to expand")}${theme.fg("muted", ")")}`;
		}
		return text;
	}
}

/** The visible lines of a live tail: the newest text after a carriage
 *  return wins on each line, and a trailing newline adds no blank. */
function liveLines(tail: string): string[] {
	const lines = tail.split("\n").map((line) => {
		const cr = line.lastIndexOf("\r");
		return cr >= 0 ? line.slice(cr + 1) : line;
	});
	if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
	return lines.map(displayText);
}

/** A local copy of pi's text-output pipeline; neither stripAnsi nor
 *  sanitizeBinaryOutput is exported from the package root. */
function plainText(result: AgentToolResult<unknown>): string {
	return result.content
		.filter((block): block is { type: "text"; text: string } => block.type === "text")
		.map((block) => displayText(block.text ?? ""))
		.join("\n");
}

function displayText(text: string): string {
	return stripAnsi(text)
		.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\uFFF9-\uFFFB]/g, "")
		.replace(/\r/g, "");
}

function stripAnsi(text: string): string {
	if (!text.includes("\u001B") && !text.includes("\u009B")) return text;
	return text.replace(ansiPattern(), "");
}

/** ansi-regex's OSC + CSI pattern, as pi's own stripAnsi uses. */
function ansiPattern(): RegExp {
	const terminator = "(?:\\u0007|\\u001B\\u005C|\\u009C)";
	const osc = `(?:\\u001B\\][\\s\\S]*?${terminator})`;
	const csi = "[\\u001B\\u009B][[\\]()#;?]*(?:\\d{1,4}(?:[;:]\\d{0,4})*)?[\\dA-PR-TZcf-nq-uy=><~]";
	return new RegExp(`${osc}|${csi}`, "g");
}
