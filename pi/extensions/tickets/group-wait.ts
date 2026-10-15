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

/** The client surface the loop needs; TicketClient satisfies it. */
export interface WaitClient extends OutputSource {
	list(): Promise<FeedTicket[]>;
	wait(id: string, timeoutSeconds: number): Promise<FeedTicket>;
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
		const started = Date.now();
		const elapsed = (): number => (Date.now() - started) / 1000;
		for (;;) {
			const records = await this.observe(options.ids);
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
			if (satisfied
				|| elapsed() >= options.bound
				|| (options.signal?.aborted ?? false)
				|| (options.shouldYield?.() ?? false)) {
				return { records, completed, running, streams: this.streams };
			}
			if (options.feed) {
				await Promise.allSettled(running.map((id) =>
					this.stream(id).pull(this.client, TicketGroupWait.PULL_CHUNKS)));
				options.feed.update(
					options.ids.map((id) => records.get(id)!),
					this.streams,
					elapsed(),
				);
				options.feed.deliverIfChanged();
			}
			await this.sleep(this.pollMs, options.signal);
		}
	}

	/** One list for every status, with a non-blocking probe only for an
	 *  id the list no longer carries, so the probe's error surfaces. */
	private async observe(ids: readonly string[]): Promise<Map<string, FeedTicket>> {
		const listed = new Map<string, FeedTicket>();
		for (const ticket of await this.client.list()) listed.set(ticket.id, ticket);
		const records = new Map<string, FeedTicket>();
		for (const id of ids) {
			records.set(id, listed.get(id) ?? await this.client.wait(id, 0));
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
