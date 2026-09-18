/**
 * DaemonSubagents views — /daemon-subagents, a faithful port of the
 * /subagents UI (selector-view.ts, detail-view.ts, viewer-chrome.ts,
 * conservative-width.ts, wheel-input.ts from the pi-adp-subagent extension)
 * with the daemon ticket store as the data source.
 *
 * Data is NOT pushed by a live registry: the daemon owns the adopted
 * workers, so both views poll. The selector refreshes the ticket list once
 * a second while mounted (storm-guarded: one in-flight fetch, single
 * timer); the detail view polls the transcript + ticket snapshot every two
 * seconds only while open. Everything else — layout, grouping, windowing,
 * mouse/wheel semantics, the fullscreen layout-root swap, the instruction
 * borders, the lazy bottom-anchored transcript rendering — matches the
 * /subagents extension 1:1.
 *
 * Adaptations vs the reference (all naming/data, no behavior):
 *  - Row label is `#<id> <agent>` where <agent> already carries the tier
 *    (agentRowMeta resolves "quick-worker" etc. from the prompt copy), so
 *    the reference's extra "(tier)" parens are omitted.
 *  - The list value column and the detail status line are derived from
 *    ticket fields (status · turns · elapsed) instead of the ADP
 *    SingleResult; no cost figure is shown in the list (not recorded).
 *  - The detail transcript is rebuilt from the ticket's NDJSON log
 *    artifact (message_end events) with message usage folded into the same
 *    stats line the reference shows (turns/limit · tokens · model).
 *  - No turn-budget / error decorations: the daemon grants native turn
 *    budgets and records exit codes, not error messages.
 *  - The detail poll replaces the reference's live listener subscription;
 *    polling stops on close (bounded timer, cleared in the close path).
 *  - "c" cancel/remove is gone from the selector: it now matches /subagents
 *    exactly (Enter/Space opens the detail view; Esc cancels).
 */

import type { AssistantMessage, Message } from "@earendil-works/pi-ai";
import {
	AssistantMessageComponent,
	getMarkdownTheme,
	getSettingsListTheme,
	ToolExecutionComponent,
	DynamicBorder,
	type KeybindingsManager,
	type Theme,
} from "@earendil-works/pi-coding-agent";
import {
	Container,
	isViewportTUI,
	matchesKey,
	Spacer,
	stripTerminalSequences,
	Text,
	truncateToWidth,
	visibleWidth,
	type Component,
	type TUI,
	type TuiMouseEvent,
	type TuiMouseEventResult,
	type ViewportTUI,
} from "@earendil-works/pi-tui";

// ---------------------------------------------------------------------------
// Data adapter (supplied by offload.ts over the daemon client)
// ---------------------------------------------------------------------------

/** The daemon-side view of one adopted subagent ticket. */
export interface AdoptedSubagent {
	id: string;
	/** Display name, tier-fused (agentRowMeta: "quick-worker" etc.). */
	agent: string;
	task: string;
	status: "running" | "done" | "failed" | "cancelled" | "lost" | (string & {});
	/** Epoch seconds. */
	started: number;
	/** Epoch seconds; absent while running. */
	finished?: number;
	exit?: number | null;
	turns?: number;
	max_turns?: number;
	cwd?: string;
}

export interface DaemonSubagentsData {
	/** This session's adopted subagent tickets, list order newest-first. */
	list(): Promise<AdoptedSubagent[]>;
	/** Full NDJSON transcript of a ticket (the child pi --mode json stream). */
	transcript(id: string): Promise<string>;
	/** Current snapshot of one ticket (immediate; never blocks). */
	refresh(id: string): Promise<AdoptedSubagent | null>;
}

// ---------------------------------------------------------------------------
// Formatting helpers (ports of format.ts, restricted to what the views use)
// ---------------------------------------------------------------------------

function formatTokens(count: number): string {
	if (count < 1000) return count.toString();
	if (count < 10000) return `${(count / 1000).toFixed(1)}k`;
	if (count < 1000000) return `${Math.round(count / 1000)}k`;
	return `${(count / 1000000).toFixed(1)}M`;
}

/** Same usage line as the /subagents detail view. */
export function formatUsageStats(
	usage: {
		input: number;
		output: number;
		cacheRead: number;
		cacheWrite: number;
		cost: number;
		contextTokens?: number;
		turns?: number;
	},
	model?: string,
	turnLimit?: number,
): string {
	const parts: string[] = [];
	if (usage.turns)
		parts.push(
			turnLimit
				? `${usage.turns}/${turnLimit} turns`
				: `${usage.turns} turn${usage.turns > 1 ? "s" : ""}`,
		);
	if (usage.input) parts.push(`↑${formatTokens(usage.input)}`);
	if (usage.output) parts.push(`↓${formatTokens(usage.output)}`);
	if (usage.cacheRead) parts.push(`R${formatTokens(usage.cacheRead)}`);
	if (usage.cacheWrite) parts.push(`W${formatTokens(usage.cacheWrite)}`);
	if (usage.cost) parts.push(`$${usage.cost.toFixed(4)}`);
	if (usage.contextTokens && usage.contextTokens > 0) {
		parts.push(`ctx:${formatTokens(usage.contextTokens)}`);
	}
	if (model) parts.push(model);
	return parts.join(" ");
}

function formatElapsed(ms: number): string {
	const seconds = Math.floor(ms / 1000);
	if (seconds < 60) return `${seconds}s`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m${seconds % 60}s`;
	return `${Math.floor(minutes / 60)}h${minutes % 60}m`;
}

/** One-line status in the /subagents selector value-column shape. */
function statusOf(t: AdoptedSubagent): string {
	const turns = t.turns ?? 0;
	if (t.status === "running") return `running (${turns} turn${turns === 1 ? "" : "s"})`;
	if (t.status === "failed" || t.status === "cancelled") return t.status;
	return `finished (${turns} turn${turns === 1 ? "" : "s"})`;
}

function elapsedOf(t: AdoptedSubagent): string {
	const endMs = t.finished && t.finished > 0 ? t.finished * 1000 : Date.now();
	return formatElapsed(endMs - t.started * 1000);
}

// ---------------------------------------------------------------------------
// ConservativeWidth (verbatim port)
// ---------------------------------------------------------------------------

const EXT_PICTOGRAPHIC = /\p{Extended_Pictographic}/u;

class ConservativeWidth {
	private readonly segmenter = new Intl.Segmenter();

	/** Terminal-column width under the conservative Windows model. */
	measure(line: string): number {
		const stripped = stripTerminalSequences(line);
		let w = 0;
		for (const { segment } of this.segmenter.segment(stripped)) {
			const model = visibleWidth(segment);
			w += model === 1 && EXT_PICTOGRAPHIC.test(segment) ? 2 : model;
		}
		return w;
	}

	truncate(line: string, width: number, ellipsis?: string): string {
		let out = truncateToWidth(line, width, ellipsis);
		let over = this.measure(out) - width;
		for (let round = 0; over > 0 && round < 4; round++) {
			out = truncateToWidth(out, width - over, ellipsis, false);
			over = this.measure(out) - width;
		}
		return out;
	}
}

// ---------------------------------------------------------------------------
// ViewerChrome (verbatim port)
// ---------------------------------------------------------------------------

class ViewerChrome {
	private cachedWidth: number | null = null;

	constructor(
		private readonly theme: Theme,
	) {}

	layout(width: number): void {
		if (this.cachedWidth === width) return;
		this.cachedWidth = width;
	}

	contentWindowHeight(rows: number): number {
		return Math.max(1, rows - 2);
	}

	appendTop(out: string[], width: number, _rows: number): void {
		const instructions = this.theme.fg("muted", " esc back · ↑↓/PgUp/PgDn scroll ");
		const infoWidth = 30;
		if (infoWidth + 4 > width) {
			out.push(truncateToWidth(this.theme.fg("border", "─".repeat(Math.max(1, width))), width));
			return;
		}
		const visible = width - 4;
		const left = Math.floor((visible - infoWidth) / 2);
		const right = visible - left - infoWidth;
		const dashes = this.theme.fg("border", "─");
		out.push(truncateToWidth(
			dashes.repeat(2) + instructions + dashes.repeat(Math.max(0, right)) + dashes.repeat(2),
			width,
		));
	}

	statsBorder(out: string[], width: number, text: string | undefined): void {
		const info = text ? this.theme.fg("muted", ` ${text} `) : "";
		const infoWidth = text ? visibleWidth(info) : 0;
		if (infoWidth + 4 > width) {
			out.push(truncateToWidth(this.theme.fg("border", "─".repeat(Math.max(1, width))), width));
			return;
		}
		const visible = width - 4;
		const left = Math.floor((visible - infoWidth) / 2);
		const right = visible - left - infoWidth;
		const dashes = this.theme.fg("border", "─");
		out.push(truncateToWidth(
			dashes.repeat(2) + info + dashes.repeat(Math.max(0, right)) + dashes.repeat(2),
			width,
		));
	}

	clipFrame(out: string[], rows: number): string[] {
		while (out.length > rows) out.pop();
		return out;
	}

	invalidate(): void {
		this.cachedWidth = null;
	}
}

// ---------------------------------------------------------------------------
// Selector dock (port of selector-view.ts)
// ---------------------------------------------------------------------------

interface SelectorRow {
	entry: AdoptedSubagent;
	groupTitle: string;
	groupCount: number;
}

const LIST_POLL_MS = 1000;

export class DaemonSubagentsDock {
	private readonly st = getSettingsListTheme();
	private readonly widthSafe = new ConservativeWidth();
	private readonly rows: SelectorRow[];
	private readonly maxLabelWidth: number;
	private readonly maxVisible: number;
	private readonly border: DynamicBorder;
	private readonly empty: Container;
	private selected = 0;
	// y offsets of the entry rows from the last render, for mouse;
	// +1 because the component's top border shifts event.y down.
	private rowMap: { y: number; index: number }[] = [];

	// Live list: poll the daemon while mounted (storm-guarded: one in-flight
	// fetch, single timer, cleared in stop()).
	private pollTimer: ReturnType<typeof setTimeout> | undefined;
	private pollInFlight = false;
	private stopped = false;

	constructor(
		private readonly data: DaemonSubagentsData,
		private readonly tui: TUI,
		private readonly theme: Theme,
		private readonly done: (entry: AdoptedSubagent | null) => void,
	) {
		const groups = [
			{ title: "Active", entries: [] as AdoptedSubagent[] },
			{ title: "Inactive", entries: [] as AdoptedSubagent[] },
		];
		this.rows = [];
		this.maxLabelWidth = 0;
		this.maxVisible = 0;
		for (const g of groups) void g;
		this.border = new DynamicBorder((s: string) => theme.fg("border", s));
		this.empty = new Container();
		this.empty.addChild(new DynamicBorder((s: string) => theme.fg("border", s)));
		this.empty.addChild(new Text(this.st.hint("  No daemon subagents in this session."), 0, 0));
		this.empty.addChild(new Text(this.st.hint("  Workers adopted by the daemon appear here."), 0, 0));
		this.empty.addChild(new Text(this.st.hint("  ↑↓/mouse select · Enter/Space open · Esc closes"), 0, 0));
		this.empty.addChild(new DynamicBorder((s: string) => theme.fg("border", s)));
		this.rows = this.groupRows([]);
		this.maxLabelWidth = 0;
		this.maxVisible = 0;
		void this.poll();
	}

	stop(): void {
		this.stopped = true;
		if (this.pollTimer) clearTimeout(this.pollTimer);
		this.pollTimer = undefined;
	}

	/** Refresh the ticket list; rebuild the grouped rows in place. */
	private async poll(): Promise<void> {
		if (this.stopped || this.pollInFlight) return;
		this.pollInFlight = true;
		let tickets: AdoptedSubagent[] = [];
		try {
			tickets = await this.data.list();
		} catch {
			/* daemon hiccup: keep the last list */
		}
		// Rebuild rows and derived widths from the fresh list.
		const rows = this.groupRows(tickets);
		this.rows.length = 0;
		this.rows.push(...rows);
		this.pollInFlight = false;
		if (this.selected >= this.rows.length) {
			this.selected = Math.max(0, this.rows.length - 1);
		}
		this.tui.requestRender();
		if (!this.stopped) {
			this.pollTimer = setTimeout(() => void this.poll(), LIST_POLL_MS);
		}
	}

	private groupRows(tickets: AdoptedSubagent[]): SelectorRow[] {
		const newestFirst = [...tickets].sort((a, b) => b.started - a.started);
		const grouped = [
			{ title: "Active", entries: newestFirst.filter((t) => t.status === "running") },
			{ title: "Inactive", entries: newestFirst.filter((t) => t.status !== "running") },
		].filter((g) => g.entries.length > 0);
		return grouped.flatMap((g) =>
			g.entries.map((entry) => ({ entry, groupTitle: g.title, groupCount: g.entries.length })),
		);
	}

	// ------------------------------------------------------------------
	// Component surface
	// ------------------------------------------------------------------

	render(width: number): string[] {
		if (this.rows.length === 0) return this.empty.render(width);
		const lines = [...this.border.render(width), ...this.renderBody(width), ...this.border.render(width)];
		return lines;
	}

	invalidate(): void {}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
			this.stop();
			this.done(null);
			return;
		}
		if (this.rows.length === 0) return;
		if (matchesKey(data, "up")) this.selected = (this.selected - 1 + this.rows.length) % this.rows.length;
		else if (matchesKey(data, "down")) this.selected = (this.selected + 1) % this.rows.length;
		else if (matchesKey(data, "enter") || data === " ") {
			this.stop();
			this.done(this.rows[this.selected].entry);
			return;
		} else return;
		this.tui.requestRender();
	}

	handleMouse(event: TuiMouseEvent): TuiMouseEventResult {
		if (this.rows.length === 0) return { handled: false };
		if (event.type === "wheel") {
			this.selected =
				(this.selected + (event.wheelDelta && event.wheelDelta < 0 ? -1 : 1) + this.rows.length) %
				this.rows.length;
		} else if (event.type === "press" || event.type === "click") {
			const row = this.rowMap.find((r) => r.y === event.y);
			if (!row) return { handled: false };
			this.selected = row.index;
			if (event.type === "click") {
				this.stop();
				this.done(this.rows[this.selected].entry);
				return { handled: true };
			}
		} else return { handled: false };
		this.tui.requestRender();
		return { handled: true };
	}

	// ------------------------------------------------------------------
	// Rendering
	// ------------------------------------------------------------------

	private labelOf(entry: AdoptedSubagent): string {
		return `#${entry.id} ${entry.agent}`;
	}

	private renderBody(width: number): string[] {
		const maxLabelWidth = Math.min(36, Math.max(...this.rows.map((r) => visibleWidth(this.labelOf(r.entry)))));
		const maxVisible = Math.min(this.rows.length, 10);
		const startIndex = Math.max(
			0,
			Math.min(this.selected - Math.floor(maxVisible / 2), this.rows.length - maxVisible),
		);
		const endIndex = Math.min(startIndex + maxVisible, this.rows.length);
		const lines: string[] = [];
		this.rowMap = [];
		let prevGroup = "";
		for (let i = startIndex; i < endIndex; i++) {
			const row = this.rows[i];
			if (row.groupTitle !== prevGroup) {
				if (prevGroup !== "") lines.push("");
				lines.push(
					this.widthSafe.truncate(
						this.theme.fg("accent", this.theme.bold(`${row.groupTitle} (${row.groupCount})`)),
						width,
					),
				);
				prevGroup = row.groupTitle;
			}
			const isSelected = i === this.selected;
			const prefix = isSelected ? this.st.cursor : "  ";
			const label = this.labelOf(row.entry);
			const labelPadded = label + " ".repeat(Math.max(0, maxLabelWidth - visibleWidth(label)));
			const separator = "  ";
			const usedWidth = visibleWidth(prefix) + maxLabelWidth + visibleWidth(separator);
			const valueMaxWidth = Math.max(0, width - usedWidth - 2);
			const valueText = this.st.value(
				this.widthSafe.truncate(statusOf(row.entry), valueMaxWidth, ""),
				isSelected,
			);
			lines.push(
				this.widthSafe.truncate(prefix + this.st.label(labelPadded, isSelected) + separator + valueText, width),
			);
			this.rowMap.push({ y: lines.length, index: i });
		}
		if (startIndex > 0 || endIndex < this.rows.length) {
			lines.push(this.st.hint(this.widthSafe.truncate(`  (${this.selected + 1}/${this.rows.length})`, width - 2, "")));
		}
		lines.push("");
		lines.push(
			this.st.hint(this.widthSafe.truncate("  ↑↓ navigate · Enter/Space to open · Esc to cancel", width, "")),
		);
		return lines;
	}
}

// ---------------------------------------------------------------------------
// Detail view (port of detail-view.ts)
// ---------------------------------------------------------------------------

const DETAIL_POLL_MS = 2000;

/**
 * Full-screen transcript viewer for one adopted subagent. Same rendering
 * pipeline as /subagents (AssistantMessageComponent / ToolExecutionComponent
 * over the NDJSON message stream, lazy bottom-anchored span, sticky
 * followTail, wheel bridge, fullscreen layout-root swap). The live stream is
 * a bounded 2s poll of the ticket's transcript artifact + snapshot while the
 * view owns the screen; the poll stops in close().
 */
export class DaemonSubagentsDetailView {
	private readonly entry: AdoptedSubagent;
	private readonly tui: TUI;
	private readonly theme: Theme;
	private readonly done: (result: null) => void;
	private readonly cwd: string;
	private readonly mdTheme = getMarkdownTheme();
	private readonly chrome: ViewerChrome;

	// Transcript rebuilt from the ticket log artifact (NDJSON of the child
	// pi stream): message_end events fold into items; usage accumulates like
	// the ADP run lifecycle so the stats line matches /subagents.
	private readonly msgs: Message[] = [];
	private usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 };
	private model: string | undefined;

	private readonly items: Component[] = [];
	private itemLines: (string[] | undefined)[] = [];
	private spanFrom = 0;
	private renderedTo = 0;
	private spanLines: string[] = [];
	private cachedWidth: number | null = null;
	private builtMessages = 0;
	private dirtyItem: number | null = null;
	private appendedBudget = false;
	private readonly pendingTools = new Map<string, ToolExecutionComponent>();

	private scrollOffset = 0;
	private followTail = true;
	private tookLayoutRoot = false;
	private savedUserBindings: Record<string, unknown> | null = null;

	// Live transcript poll (replaces the reference's registry listeners).
	private pollTimer: ReturnType<typeof setTimeout> | undefined;
	private pollInFlight = false;
	private closedFlag = false;
	private parsedLines = 0;

	private readonly viewportTui: ViewportTUI | undefined;
	private readonly widthSafe = new ConservativeWidth();

	constructor(
		entry: AdoptedSubagent,
		tui: TUI,
		theme: Theme,
		done: (result: null) => void,
		private readonly keybindings: KeybindingsManager,
		private readonly data: DaemonSubagentsData,
	) {
		this.entry = entry;
		this.tui = tui;
		this.theme = theme;
		this.done = done;
		this.cwd = entry.cwd ?? process.cwd();
		this.viewportTui = isViewportTUI(tui) ? tui : undefined;
		this.chrome = new ViewerChrome(theme);
	}

	// ------------------------------------------------------------------
	// Lifecycle
	// ------------------------------------------------------------------

	open(): void {
		this.startPolling();
		if (this.viewportTui) {
			this.tookLayoutRoot = true;
			this.viewportTui.setLayoutRoot(this);
			this.tui.requestRender(true);
		}
		this.savedUserBindings = this.keybindings.getUserBindings();
		this.keybindings.setUserBindings({
			...this.savedUserBindings,
			"tui.altScreen.pageUp": [],
			"tui.altScreen.pageDown": [],
			"tui.altScreen.top": [],
			"tui.altScreen.bottom": [],
		} as Parameters<KeybindingsManager["setUserBindings"]>[0]);
	}

	/** Idempotent teardown: stop the poll and hand the screen back. */
	close(): void {
		if (!this.closedFlag) this.closedFlag = true;
		if (this.pollTimer) clearTimeout(this.pollTimer);
		this.pollTimer = undefined;
		if (this.tookLayoutRoot) {
			this.tookLayoutRoot = false;
			this.viewportTui?.setLayoutRoot(undefined);
		}
		if (this.savedUserBindings) {
			this.keybindings.setUserBindings(this.savedUserBindings as Parameters<KeybindingsManager["setUserBindings"]>[0]);
			this.savedUserBindings = null;
		}
	}

	/** Poll the daemon every DETAIL_POLL_MS while open: fold new transcript
	 * lines into messages and refresh the ticket snapshot. Bounded: cleared
	 * in close(), guarded against overlap, and skipped once closed. */
	private startPolling(): void {
		this.closedFlag = false;
		void this.poll();
	}

	private async poll(): Promise<void> {
		if (this.closedFlag || this.pollInFlight) return;
		this.pollInFlight = true;
		try {
			const [transcript, ticket] = await Promise.all([
				this.data.transcript(this.entry.id),
				this.data.refresh(this.entry.id).catch(() => null),
			]);
			this.appendTranscript(transcript);
			if (ticket) {
				if (ticket.started > 0) this.entry.started = ticket.started;
				if (ticket.finished) this.entry.finished = ticket.finished;
				if (ticket.status !== this.entry.status) this.entry.status = ticket.status;
				if (ticket.turns !== undefined) this.entry.turns = ticket.turns;
				if (ticket.exit !== undefined) this.entry.exit = ticket.exit;
			}
		} catch {
			/* daemon hiccup: keep the last transcript state */
		}
		this.pollInFlight = false;
		if (!this.closedFlag) {
			this.pollTimer = setTimeout(() => void this.poll(), DETAIL_POLL_MS);
		}
		this.tui.requestRender();
	}

	/** Fold any NEW NDJSON lines from the transcript artifact. */
	private appendTranscript(transcript: string): void {
		const lines = transcript.split("\n");
		for (let i = this.parsedLines; i < lines.length; i++) {
			this.processLine(lines[i]);
		}
		this.parsedLines = Math.max(this.parsedLines, lines.length);
	}

	/** One message_end event line (mirrors the ADP run stream parsing). */
	private processLine(line: string): void {
		if (!line.trim()) return;
		let event: any;
		try {
			event = JSON.parse(line);
		} catch {
			return;
		}
		if (event.type !== "message_end" || !event.message) return;
		const msg = event.message as Message;
		this.msgs.push(msg);
		if (msg.role === "assistant") {
			this.usage.turns++;
			const usage = msg.usage;
			if (usage) {
				this.usage.input += usage.input || 0;
				this.usage.output += usage.output || 0;
				this.usage.cacheRead += usage.cacheRead || 0;
				this.usage.cacheWrite += usage.cacheWrite || 0;
				this.usage.cost += usage.cost?.total || 0;
				this.usage.contextTokens = usage.totalTokens || 0;
			}
			if (!this.model && msg.model) this.model = msg.model;
		}
	}

	// ------------------------------------------------------------------
	// Component surface (also the fullscreen layout root)
	// ------------------------------------------------------------------

	render(width: number): string[] {
		const rows = this.tui.terminal.rows;
		const windowHeight = this.windowHeight();
		const widthChanged = this.cachedWidth !== width;
		this.ensureItems(width);
		if (widthChanged) this.resetLines(width, windowHeight);
		this.syncLines();
		this.growToWindow(windowHeight);
		this.clampScroll(windowHeight);

		const out: string[] = [];
		out.push(this.widthSafe.truncate(this.titleLine(width, windowHeight), width));
		this.chrome.appendTop(out, width, rows);
		this.appendContentWindow(out, windowHeight);
		if (this.tookLayoutRoot) {
			this.chrome.statsBorder(out, width, this.statsLine(width));
		} else {
			out.push(this.widthSafe.truncate(this.statsLine(width), width));
		}
		return this.chrome.clipFrame(out, rows);
	}

	invalidate(): void {
		this.cachedWidth = null;
		this.itemLines = [];
		this.spanLines = [];
		this.spanFrom = this.items.length;
		this.renderedTo = this.items.length;
		this.dirtyItem = null;
		this.chrome.invalidate();
	}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) {
			this.done(null);
			return;
		}
		const windowHeight = this.windowHeight();
		if (matchesKey(data, "up")) {
			this.followTail = false;
			this.scrollTo(this.scrollOffset - 1, windowHeight);
		} else if (matchesKey(data, "down")) {
			this.scrollTo(this.scrollOffset + 1, windowHeight);
		} else if (matchesKey(data, "pageUp")) {
			this.followTail = false;
			this.scrollTo(this.scrollOffset - (windowHeight - 1), windowHeight);
		} else if (matchesKey(data, "pageDown")) {
			this.scrollTo(this.scrollOffset + (windowHeight - 1), windowHeight);
		} else if (matchesKey(data, "home")) {
			this.followTail = false;
			this.scrollToHome();
		} else if (matchesKey(data, "end")) {
			this.followTail = true;
			this.scrollTo(this.maxOffset(windowHeight), windowHeight);
		} else {
			return;
		}
		this.tui.requestRender();
	}

	handleMouse(event: TuiMouseEvent): { handled: boolean } | undefined {
		if (event.type !== "wheel") return undefined;
		if (this.cachedWidth === null) return undefined;
		return { handled: this.applyWheelDelta(event.wheelDelta ?? 0) };
	}

	applyWheelDelta(delta: number): boolean {
		if (this.cachedWidth === null) return false;
		const windowHeight = this.windowHeight();
		const wasFollowing = this.followTail;
		if (delta < 0) this.followTail = false;
		this.scrollTo(this.scrollOffset + delta, windowHeight);
		if (this.scrollOffset >= this.maxOffset(windowHeight)) this.followTail = true;
		this.tui.requestRender();
		return wasFollowing || this.followTail || delta !== 0;
	}

	// ------------------------------------------------------------------
	// Scrolling
	// ------------------------------------------------------------------

	private maxOffset(windowHeight: number): number {
		return Math.max(0, this.spanLines.length - windowHeight);
	}

	private clampScroll(windowHeight: number): void {
		const maxOffset = this.maxOffset(windowHeight);
		if (this.followTail) this.scrollOffset = maxOffset;
		this.scrollOffset = Math.min(Math.max(0, this.scrollOffset), maxOffset);
	}

	private scrollTo(target: number, windowHeight: number): void {
		if (target < 0 && this.spanFrom > 0) {
			const added = this.growUpLines(windowHeight);
			this.scrollOffset += added;
			target += added;
		}
		const maxOffset = this.maxOffset(windowHeight);
		const clamped = Math.min(Math.max(0, target), maxOffset);
		if (clamped !== this.scrollOffset) {
			this.scrollOffset = clamped;
			this.tui.requestRender();
		}
	}

	private scrollToHome(): void {
		while (this.spanFrom > 0) {
			this.spanFrom--;
			this.itemLines[this.spanFrom] = this.renderItem(this.spanFrom);
		}
		this.rebuildSpanLines();
		this.followTail = false;
		this.scrollOffset = 0;
		this.tui.requestRender();
	}

	// ------------------------------------------------------------------
	// Frame assembly
	// ------------------------------------------------------------------

	private windowHeight(): number {
		return Math.max(1, this.chrome.contentWindowHeight(this.tui.terminal.rows) - 1);
	}

	private titleLine(width: number, windowHeight: number): string {
		const entry = this.entry;
		const parts = [
			this.theme.fg("accent", `#${entry.id} ${entry.agent} — adopted`),
			this.theme.fg("dim", ` ${statusOf(entry)} · ${elapsedOf(entry)}`),
		];
		if (this.spanLines.length > 0) {
			const position =
				`lines ${this.scrollOffset + 1}–${Math.min(this.spanLines.length, this.scrollOffset + windowHeight)} of ${this.spanLines.length}${this.spanFrom > 0 ? "+" : ""}`;
			parts.push(this.theme.fg("dim", ` · ${position}`));
		}
		return this.widthSafe.truncate(parts.join(""), width);
	}

	private appendContentWindow(out: string[], windowHeight: number): void {
		const slice = this.spanLines.slice(this.scrollOffset, this.scrollOffset + windowHeight);
		for (let i = 0; i < windowHeight; i++) {
			out.push(this.widthSafe.truncate(slice[i] ?? "", this.tui.terminal.columns));
		}
	}

	private statsLine(width: number): string {
		const usageStr = formatUsageStats(this.usage, this.model, this.entry.max_turns);
		return this.theme.fg("muted", usageStr || "no usage yet");
	}

	// ------------------------------------------------------------------
	// Item building
	// ------------------------------------------------------------------

	/** Fold every message that arrived since the last build into items. */
	private ensureItems(width: number): void {
		for (; this.builtMessages < this.msgs.length; this.builtMessages++) {
			const msg = this.msgs[this.builtMessages];
			if (msg.role === "assistant") {
				if (this.items.length > 0) this.items.push(new Spacer(1));
				this.items.push(new AssistantMessageComponent(msg, false, this.mdTheme));
				for (const block of msg.content ?? []) {
					if (block.type !== "toolCall") continue;
					const component = new ToolExecutionComponent(
						block.name,
						block.id,
						block.arguments,
						undefined,
						undefined,
						this.tui,
						this.cwd,
					);
					this.items.push(component);
					this.pendingTools.set(block.id, component);
				}
			} else if (msg.role === "toolResult") {
				const pending = this.pendingTools.get(msg.toolCallId);
				if (!pending) continue;
				pending.updateResult(msg);
				this.pendingTools.delete(msg.toolCallId);
				const itemIndex = this.items.indexOf(pending);
				if (itemIndex >= 0) {
					this.dirtyItem = this.dirtyItem === null
						? itemIndex
						: Math.min(this.dirtyItem, itemIndex);
				}
			}
		}
		// Reserved for parity with the reference's tail decorations.
		if (this.appendedBudget) void width;
	}

	// ------------------------------------------------------------------
	// Lazy line cache
	// ------------------------------------------------------------------

	private resetLines(width: number, windowHeight: number): void {
		this.cachedWidth = width;
		this.itemLines = new Array(this.items.length);
		this.spanFrom = this.items.length;
		this.spanLines = [];
		this.followTail = true;
		this.scrollOffset = Number.MAX_SAFE_INTEGER;
		this.fillBottom(windowHeight * 2);
		this.clampScroll(windowHeight);
	}

	private growUpLines(targetLines: number): number {
		let added = 0;
		while (this.spanFrom > 0 && added < targetLines) {
			this.spanFrom--;
			this.itemLines[this.spanFrom] = this.renderItem(this.spanFrom);
			added += (this.itemLines[this.spanFrom] as string[]).length;
		}
		this.rebuildSpanLines();
		return added;
	}

	private fillBottom(fillTarget: number): void {
		let have = this.spanLines.length;
		while (this.spanFrom > 0 && have < fillTarget) {
			this.spanFrom--;
			this.itemLines[this.spanFrom] = this.renderItem(this.spanFrom);
			have += (this.itemLines[this.spanFrom] as string[]).length;
		}
		this.rebuildSpanLines();
	}

	private renderItem(i: number): string[] {
		const raw = (this.items[i] as Component).render(this.cachedWidth as number);
		return raw.map((line) => this.widthSafe.truncate(line, this.cachedWidth as number));
	}

	private rebuildSpanLines(): void {
		const lines: string[] = [];
		for (let i = this.spanFrom; i < this.items.length; i++) {
			lines.push(...(this.itemLines[i] ?? []));
		}
		this.spanLines = lines;
	}

	private syncLines(): void {
		const start = this.dirtyItem ?? this.renderedTo;
		for (let i = Math.min(start, this.spanFrom); i < this.items.length; i++) {
			if (this.itemLines[i] === undefined || i >= start) {
				this.itemLines[i] = this.renderItem(i);
			}
		}
		this.renderedTo = this.items.length;
		this.dirtyItem = null;
		this.rebuildSpanLines();
	}

	private growToWindow(windowHeight: number): void {
		if (this.spanFrom > 0 && this.scrollOffset < 1) {
			const added = this.growUpLines(windowHeight);
			this.scrollOffset += added;
		}
	}
}

// ---------------------------------------------------------------------------
// Wheel bridge (verbatim port of wheel-input.ts)
// ---------------------------------------------------------------------------

const SGR_MOUSE = [/^\x1b\[<(\d+);(\d+);(\d+)[Mm]/, /^\x1b\[<[\d;]*[Mm]?/] as const;
const X10_MOUSE = [/^\x1b\[M[\s\S]{3}/, /^\x1b\[M[\s\S]{0,2}/] as const;
const WHEEL_BUFFER_LIMIT = 32;
const MOUSE_ENABLE = "\x1b[?1000h\x1b[?1006h";
const MOUSE_DISABLE = "\x1b[?1006l\x1b[?1000l";

interface WheelTarget {
	applyWheelDelta(delta: number): boolean;
}

class WheelInputDecoder {
	private buffer = "";

	feed(data: string): { delta: number; consume: boolean } {
		this.buffer += data;
		if (this.buffer.length > WHEEL_BUFFER_LIMIT) return this.settle(0, false);
		let delta = 0;
		let consumedAny = false;
		for (;;) {
			const sgr = SGR_MOUSE[0].exec(this.buffer);
			if (sgr) {
				delta += this.wheelDelta(Number.parseInt(sgr[1], 10));
				consumedAny = true;
				this.buffer = this.buffer.slice(sgr[0].length);
				continue;
			}
			const x10 = X10_MOUSE[0].exec(this.buffer);
			if (x10) {
				delta += this.wheelDelta(this.buffer.charCodeAt(3) - 32);
				consumedAny = true;
				this.buffer = this.buffer.slice(x10[0].length);
				continue;
			}
			break;
		}
		const partial = SGR_MOUSE[1].test(this.buffer) || X10_MOUSE[1].test(this.buffer);
		if (partial && this.buffer.length > 0) return { delta, consume: consumedAny };
		return this.settle(delta, consumedAny);
	}

	private wheelDelta(button: number): number {
		if ((button & 64) === 0) return 0;
		return (button & 3) === 0 ? -1 : (button & 3) === 1 ? 1 : 0;
	}

	private settle(delta: number, consume: boolean): { delta: number; consume: boolean } {
		this.buffer = "";
		return { delta, consume };
	}
}

interface TerminalUi {
	onTerminalInput(handler: (data: string) => { consume?: boolean } | undefined): () => void;
}

export class DaemonDetailViewWheelBridge {
	private readonly decoder = new WheelInputDecoder();
	private tui: TUI | null = null;
	private unsubscribe: (() => void) | null = null;
	private detached = false;

	constructor(private readonly ui: TerminalUi) {}

	attach(tui: TUI, target: WheelTarget): void {
		if (tui.mode !== "regular") return;
		this.tui = tui;
		tui.terminal.write(MOUSE_ENABLE);
		this.unsubscribe = this.ui.onTerminalInput((data) => {
			const result = this.decoder.feed(data);
			if (result.consume && result.delta !== 0) target.applyWheelDelta(result.delta);
			return result.consume ? { consume: true } : undefined;
		});
	}

	detach(): void {
		if (this.detached) return;
		this.detached = true;
		this.unsubscribe?.();
		if (this.tui) this.tui.terminal.write(MOUSE_DISABLE);
	}
}

// ---------------------------------------------------------------------------
// Browser loop (port of the /subagents navigation: selector → detail → back)
// ---------------------------------------------------------------------------

class DaemonDetailViewSession {
	private view: DaemonSubagentsDetailView | undefined;

	constructor(private readonly data: DaemonSubagentsData) {}

	mount(ui: any, entry: AdoptedSubagent): Promise<null> {
		const wheel = new DaemonDetailViewWheelBridge(ui);
		return (ui.custom as (
			cb: (tui: any, theme: any, kb: KeybindingsManager, done: (result: null) => void) => any,
		) => Promise<null>)(
			(tui, theme, kb, done) => {
				this.view = new DaemonSubagentsDetailView(entry, tui, theme, done, kb, this.data);
				this.view.open();
				wheel.attach(tui, this.view);
				return this.view;
			},
		).finally(() => {
			wheel.detach();
			this.view?.close();
		});
	}
}

/** The /daemon-subagents navigation loop: selector → detail → back. */
export class DaemonSubagentsBrowser {
	private readonly sessions: DaemonDetailViewSession;

	constructor(private readonly data: DaemonSubagentsData) {
		this.sessions = new DaemonDetailViewSession(data);
	}

	async run(ui: any): Promise<void> {
		try {
			while (true) {
				const selected = await this.openList(ui);
				if (!selected) return;
				await this.sessions.mount(ui, selected);
			}
		} catch {
			/* selector or viewer unavailable/canceled mid-loop */
		}
	}

	private openList(ui: any): Promise<AdoptedSubagent | null> {
		return ui.custom(
			(tui: any, theme: any, _kb: any, done: any) =>
				new DaemonSubagentsDock(this.data, tui, theme, done),
		) as Promise<AdoptedSubagent | null>;
	}
}