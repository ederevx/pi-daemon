/**
 * pi-daemon — the `tool-completion` capability provider (pi-rpc/1).
 *
 * A consumer that holds an acknowledgment needs one thing: which
 * registered tool resolves it. This service answers that question and
 * nothing else - it never retrieves output and never executes work, so a
 * resolver call still travels through pi's own tool validation, hooks, and
 * permission checks.
 *
 * The in-process profile of docs/pi-services.md applies: requests arrive
 * on `pi-rpc/<service>`, replies leave on `pi-rpc/<service>/reply`, and
 * `rpc.hello` is answered synchronously inside the handler so a consumer
 * that has no reply by the time `emit` returns knows no provider exists.
 */

import type { EventBus } from "@earendil-works/pi-coding-agent";

import {
	COMPLETION_DESCRIBE,
	COMPLETION_SERVICE,
	COMPLETION_VERSION,
	type CompletionBinding,
	type CompletionDescription,
	type CompletionMessage,
} from "./protocol.ts";

export class ToolCompletionService {
	private readonly bus: EventBus;
	private readonly bindings: readonly CompletionBinding[];

	constructor(bus: EventBus, bindings: readonly CompletionBinding[]) {
		this.bus = bus;
		this.bindings = bindings;
	}

	/** Publish the capability; returns the unsubscribe to call on shutdown. */
	start(): () => void {
		return this.bus.on(ToolCompletionService.channel(), (data) => this.handle(data));
	}

	private static channel(): string {
		return `pi-rpc/${COMPLETION_SERVICE}`;
	}

	/** Answer one request. Every reply is produced before an `await`, which
	 *  is what lets a consumer treat a silent `emit` as "no provider". */
	private handle(data: unknown): void {
		const message = data as CompletionMessage | undefined;
		if (!message || message.jsonrpc !== "2.0") return;
		if (typeof message.id !== "string" || typeof message.method !== "string") return;
		if (message.method === "rpc.hello") {
			this.reply(message.id, {
				service: COMPLETION_SERVICE,
				version: COMPLETION_VERSION,
				methods: ["rpc.hello", COMPLETION_DESCRIBE],
				topics: [],
			});
			return;
		}
		if (message.method === COMPLETION_DESCRIBE) {
			const description: CompletionDescription = { bindings: this.bindings };
			this.reply(message.id, description);
			return;
		}
		this.replyError(message.id, -32601, "method not found");
	}

	private reply(id: string, result: unknown): void {
		this.bus.emit(`${ToolCompletionService.channel()}/reply`, {
			jsonrpc: "2.0",
			id,
			result,
		});
	}

	private replyError(id: string, code: number, text: string): void {
		this.bus.emit(`${ToolCompletionService.channel()}/reply`, {
			jsonrpc: "2.0",
			id,
			error: { code, message: text },
		});
	}
}
