/**
 * pi-daemon — the multi-ticket wait loop.
 *
 * One call blocks on several tickets at once: each tick makes a single
 * unfiltered `client.list()` (every id's status in one exec), pulls each
 * running ticket's new output concurrently under a per-ticket chunk
 * budget, hands the statuses and streams to the feed, then sleeps
 * abortably until the next tick. It stops on mode `first` (any ticket
 * complete) or `all` (every ticket complete), the bound, the run's abort
 * signal, or a queued user message. An id missing from the list falls
 * back to a non-blocking `client.wait(id, 0)` so its error surfaces. A
 * pull failure is cosmetic: the final result always comes from
 * `outputAll`, so stream lag never truncates it.
 */

import { TicketStream, type OutputSource } from "./ticket-stream.ts";
import type { FeedTicket, TicketFeed } from "./ticket-feed.ts";

export type WaitMode = "first" | "all";

/** The wait ended before a request could complete: the bound, an
 *  abort, or a queued message cancelled it. Not a daemon failure. */
export class WaitCancelled extends Error {
	constructor() {
		super("the wait ended before the daemon replied");
	}
}

/** The client surface the loop needs; TicketClient satisfies it. */
export interface WaitClient extends OutputSource {
	list(session?: string, signal?: AbortSignal): Promise<FeedTicket[]>;
	wait(id: string, timeoutSeconds: number, signal?: AbortSignal): Promise<FeedTicket>;
}

export interface GroupWaitOptions {
	ids: readonly string[];
	mode: WaitMode;
	/** Total seconds to wait; a probe-only call passes 0. */
	bound: number;
	signal?: AbortSignal;
	shouldYield?: () => boolean;
	feed?: TicketFeed;
	/** Called once per id, the moment it is observed complete. */
	onObserved?: (ticket: FeedTicket) => void;
}

export interface GroupOutcome {
	records: Map<string, FeedTicket>;
	completed: string[];
	running: string[];
	streams: Map<string, TicketStream>;
}

export class TicketGroupWait {
	private static readonly PULL_CHUNKS = 16;

	private readonly streams = new Map<string, TicketStream>();

	constructor(
		private readonly client: WaitClient,
		private readonly pollMs = 1000,
	) {}

	async run(options: GroupWaitOptions): Promise<GroupOutcome> {
		const started = performance.now();
		const elapsed = (): number => (performance.now() - started) / 1000;
		// The bound governs the whole loop, not just the check: the
		// sleep asks only for the remaining time, an in-flight status
		// or output request is cancelled at expiry, and the wait ends
		// on the records in hand instead of streaming another frame
		// past it.
		const remaining = (): number =>
			options.bound * 1000 - (performance.now() - started);
		const expired = (): boolean =>
			options.bound !== Infinity && remaining() <= 0;
		const stopped = (): boolean =>
			expired()
			|| (options.signal?.aborted ?? false)
			|| (options.shouldYield?.() ?? false);
		// Cancellation reaches every in-flight request; the caller's
		// own signal rides along, and a stop check at the polling
		// cadence aborts while a request is still pending, so a hung
		// reply cannot outlive the bound. An unbounded watch stops on
		// abort or a queued message alone.
		const cancel = new AbortController();
		const forwardAbort = (): void => cancel.abort();
		let stopTimer: ReturnType<typeof setTimeout> | undefined;
		const checkStop = (): void => {
			if (stopped()) {
				cancel.abort();
				return;
			}
			stopTimer = setTimeout(checkStop,
				Math.min(this.pollMs, remaining(), 0x7fffffff));
		};
		let previous: Map<string, FeedTicket> | null = null;
		try {
			options.signal?.addEventListener("abort", forwardAbort, { once: true });
			if (options.signal?.aborted) forwardAbort();
			checkStop();
			for (;;) {
				let records: Map<string, FeedTicket>;
				try {
					records = await this.observe(options.ids, stopped, cancel.signal);
				} catch (error) {
					// A stop past the first complete observation falls back
					// to it; status must never be fabricated.
					if (!(error instanceof WaitCancelled) || !previous) throw error;
					records = previous;
				}
				for (const id of options.ids) {
					if (!records.has(id) && previous?.has(id)) {
						records.set(id, previous.get(id)!);
					}
				}
				previous = records;
				for (const id of options.ids) {
					const ticket = records.get(id)!;
					if (ticket.status !== "running") options.onObserved?.(ticket);
				}
				const running = options.ids.filter(
					(id) => records.get(id)!.status === "running");
				const completed = options.ids.filter(
					(id) => records.get(id)!.status !== "running");
				const satisfied = options.mode === "first"
					? completed.length > 0
					: running.length === 0;
				if (satisfied || stopped()) {
					return { records, completed, running, streams: this.streams };
				}
				if (options.feed) {
					await Promise.allSettled(running.map((id) =>
						this.stream(id).pull(this.client, TicketGroupWait.PULL_CHUNKS,
							stopped, cancel.signal)));
					if (stopped()) {
						return { records, completed, running, streams: this.streams };
					}
					options.feed.update(
						options.ids.map((id) => records.get(id)!),
						this.streams,
						elapsed(),
					);
					options.feed.deliverIfChanged();
				}
				await this.sleep(
					Math.max(0, Math.min(this.pollMs, remaining())),
					options.signal);
				// An expired sleep must not buy another status round: the
				// bound ends the wait on the records in hand.
				if (satisfied || stopped()) {
					return { records, completed, running, streams: this.streams };
				}
			}
		} finally {
			clearTimeout(stopTimer);
			options.signal?.removeEventListener("abort", forwardAbort);
			cancel.abort();
		}
	}

	/** One list for every status, with a non-blocking probe only for
	 *  an id the list no longer carries, so the probe's error surfaces.
	 *  A stop before any request cancels it: status is never fetched
	 *  past the bound. */
	private async observe(
		ids: readonly string[],
		shouldStop: () => boolean,
		signal?: AbortSignal,
	): Promise<Map<string, FeedTicket>> {
		if (shouldStop()) throw new WaitCancelled();
		const listed = new Map<string, FeedTicket>();
		for (const ticket of await this.client.list(undefined, signal)) {
			listed.set(ticket.id, ticket);
		}
		const records = new Map<string, FeedTicket>();
		for (const id of ids) {
			if (!listed.has(id) && shouldStop()) throw new WaitCancelled();
			records.set(id, listed.get(id) ?? await this.client.wait(id, 0, signal));
		}
		return records;
	}

	private stream(id: string): TicketStream {
		let stream = this.streams.get(id);
		if (!stream) {
			stream = new TicketStream(id);
			this.streams.set(id, stream);
		}
		return stream;
	}

	/** Sleeps up to ms, waking at once on abort; never a busy wait. */
	private sleep(ms: number, signal?: AbortSignal): Promise<void> {
		if (ms <= 0 || signal?.aborted) return Promise.resolve();
		return new Promise((resolve) => {
			let timer: ReturnType<typeof setTimeout>;
			const finish = (): void => {
				clearTimeout(timer);
				signal?.removeEventListener("abort", finish);
				resolve();
			};
			timer = setTimeout(finish, ms);
			signal?.addEventListener("abort", finish, { once: true });
		});
	}
}
