/**
 * pi-daemon — the tool-completion contract.
 *
 * A tool whose result arrives later than its call must say so. The
 * offloading bash backend submits a daemon ticket and returns a claim at
 * once, so its tool result is an acknowledgment, not the command's output;
 * a caller that forwards it as the result would hand an acknowledgment to
 * whoever asked for the output. This module owns the vocabulary that
 * makes the distinction explicit and portable:
 *
 * - `toolCompletion` is an additive field a producer attaches to its tool
 *   result `details`. `final` means this result is the tool's own
 *   completed output. `pending` means the producer already started the
 *   work named by an opaque payload, which the caller resolves through
 *   the continuation tool the capability advertises - it must never re-run
 *   the original operation.
 * - The capability itself is `pi-rpc/1`'s `tool-completion` service (see
 *   docs/pi-services.md), reached in-process over `pi.events`. A consumer
 *   probes it at the point of use and negotiates `completion.describe`,
 *   which names the producer tools and their resolver tools as registered
 *   in this session. No consumer needs to know this package, the bash
 *   tool, or the ticket syntax.
 *
 * The version is the schema's version, not a release version; a consumer
 * that meets an unknown version fails explicitly instead of guessing.
 */

/** The capability name the service registers under (never a package name). */
export const COMPLETION_SERVICE = "tool-completion";

/** The completion schema this producer emits and this resolver accepts. */
export const COMPLETION_VERSION = 1;

/** The method a consumer negotiates to learn the bindings. */
export const COMPLETION_DESCRIBE = "completion.describe";

/** One producer tool and the registered continuation tool that resolves it. */
export interface CompletionBinding {
	/** The registered tool whose results carry `toolCompletion`. */
	tool: string;
	/** Completion schema versions that tool emits. */
	versions: readonly number[];
	/** The registered tool a consumer calls to obtain a pending result. */
	resolver: { name: string };
}

/** The `completion.describe` result. */
export interface CompletionDescription {
	bindings: readonly CompletionBinding[];
}

/** A tool result that is final, or one that names a pending operation. */
export type ToolCompletion =
	| { version: number; state: "final" }
	| { version: number; state: "pending"; service: string; payload: unknown };

/** The additive `details` shape a producer attaches. */
export interface CompletionDetails {
	toolCompletion?: ToolCompletion;
}

/** One JSON-RPC 2.0 message of the in-process profile. */
export interface CompletionMessage {
	jsonrpc?: unknown;
	id?: unknown;
	method?: unknown;
	params?: unknown;
	result?: unknown;
	error?: { code: number; message: string };
}

/** The metadata a producer attaches to a tool result. */
export class CompletionMetadata {
	/** The result is the tool's own completed output. */
	static final(version: number = COMPLETION_VERSION): ToolCompletion {
		return { version, state: "final" };
	}

	/** The result is an acknowledgment; `payload` names the started work. */
	static pending(payload: unknown, version: number = COMPLETION_VERSION): ToolCompletion {
		return { version, state: "pending", service: COMPLETION_SERVICE, payload };
	}

	/** Merge one completion into a tool result's existing details, which
	 *  are preserved untouched: the field is additive. */
	static attach(details: unknown, completion: ToolCompletion): CompletionDetails & Record<string, unknown> {
		const base = details !== null && typeof details === "object" && !Array.isArray(details)
			? details as Record<string, unknown>
			: {};
		return { ...base, toolCompletion: completion };
	}

	/** Read a completion back out of `details`; `undefined` when the field
	 *  is absent or malformed, which a consumer treats as "not advertised". */
	static of(details: unknown): ToolCompletion | undefined {
		if (details === null || typeof details !== "object") return undefined;
		const raw = (details as CompletionDetails).toolCompletion;
		if (raw === null || typeof raw !== "object") return undefined;
		if (typeof raw.version !== "number") return undefined;
		if (raw.state === "final") return { version: raw.version, state: "final" };
		if (raw.state !== "pending") return undefined;
		if (typeof raw.service !== "string" || raw.service === "") return undefined;
		if (raw.payload === undefined || raw.payload === null) return undefined;
		return { version: raw.version, state: "pending", service: raw.service, payload: raw.payload };
	}
}
