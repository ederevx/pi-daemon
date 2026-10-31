/**
 * pi-daemon — per-call completion bookkeeping for a producer tool.
 *
 * A backend that defers its work must report that on the result of the
 * very call it deferred, and concurrent calls must never see each other's
 * operation. The bash backend cannot return `details` (its `exec` contract
 * is an exit code) and the registered tool is shared by concurrent calls,
 * so the mark travels through the call's own async scope instead: one
 * store per call, owned here, never a shared field on the backend.
 */

import { AsyncLocalStorage } from "node:async_hooks";

import {
	CompletionMetadata,
	type CompletionDetails,
	type ToolCompletion,
} from "./protocol.ts";

/** One call's completion record, mutable only through this owner. */
interface CallScope {
	completion: ToolCompletion;
}

export class ToolCompletionCalls {
	private readonly store = new AsyncLocalStorage<CallScope>();

	/** Wrap a registered tool so every outcome carries a completion: the
	 *  result is final unless the backend marked the call pending. The
	 *  tool's own details are preserved. */
	attach(tool: { execute?: unknown }): void {
		const original = tool.execute as
			| ((...args: unknown[]) => Promise<{ details?: unknown }>)
			| undefined;
		if (typeof original !== "function") return;
		tool.execute = (...args: unknown[]) => this.wrap(() => original(...args));
	}

	/** Mark the call currently executing as pending on `payload`. Called by
	 *  the backend from inside the tool's own execution; a call outside a
	 *  wrapped execution has no scope and is left unmarked. */
	pending(payload: unknown): void {
		const scope = this.store.getStore();
		if (scope) scope.completion = CompletionMetadata.pending(payload);
	}

	private async wrap<T extends { details?: unknown }>(
		run: () => Promise<T>,
	): Promise<T & CompletionDetails> {
		const scope: CallScope = { completion: CompletionMetadata.final() };
		return this.store.run(scope, async () => {
			const result = await run();
			return { ...result, details: CompletionMetadata.attach(result.details, scope.completion) };
		});
	}
}
