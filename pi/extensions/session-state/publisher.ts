/**
 * The one publisher of a hosted session's model run state.
 *
 * The daemon keeps each publish as a durable run-outcome record, so the
 * record is only trustworthy when every publisher in the process shares
 * three rules. They live here, not in the extensions, because two writers
 * exist (the backgrounding extension's before_agent_start/agent_settled
 * hooks and the offload extension's ticket waits) and a rule kept twice
 * drifts:
 *
 * - one monotonic seq for the whole process, so the daemon can drop a
 *   publish that arrived out of order and a late `busy` cannot overwrite
 *   the `idle` that came after it;
 * - publishing stops at a quit, which pi always emits before it disposes
 *   the runtime. A turn killed mid-flight therefore keeps its `busy`
 *   record and is reported as a death, never as a settle - while a reload
 *   or a session switch, which replaces the runtime inside the same
 *   process, goes on publishing;
 * - `aborted` rides with an idle publish, so an interrupted turn is
 *   reported as run-aborted instead of settled.
 *
 * The counter and the stop flag are process-wide, not merely module-wide:
 * pi loads each extension with `moduleCache: false`, so daemon.ts and
 * offload.ts get a separate instance of this module and a plain
 * module-level singleton would hand each of them its own counter - after
 * a few turns one publisher's stamps would sit permanently under the
 * other's and its publishes would be dropped for good. The shared state
 * therefore hangs off `globalThis` under a registered symbol, which is
 * the one thing a duplicated module instance cannot duplicate.
 *
 * Executing `pi-rc state` is injected, so this module owns the protocol
 * and nothing about how a host launches the client.
 */

export type StateExec = (args: string[]) => Promise<unknown>;

export type RunState = "busy" | "idle" | "waiting";

/** The process-wide stamp counter and stop flag. */
export class SharedState {
	seq = 0;
	stopped = false;
}

const SHARED_KEY = Symbol.for("pi-daemon.session-state.publisher");

/** The one SharedState in this process, whichever module instance asks. */
function sharedState(): SharedState {
	const holder = globalThis as unknown as Record<symbol, SharedState>;
	holder[SHARED_KEY] ??= new SharedState();
	return holder[SHARED_KEY];
}

export class RunStatePublisher {
	/** The instance the extensions publish through. */
	static readonly shared = new RunStatePublisher();

	constructor(private readonly state: SharedState = sharedState()) {}

	/** session_shutdown: only a quit ends the process. A reload, a new
	 *  session, a resume or a fork replaces the session runtime inside the
	 *  same process, and that runtime's publishes must land - a stop that
	 *  outlived its session would silence every later turn for good, and an
	 *  idle publish after a death would report it as a settle. */
	stop(reason?: string): void {
		if (reason === undefined || reason === "quit") {
			this.state.stopped = true;
		}
	}

	/** session_start: a runtime that starts after a quit in the same
	 *  process may publish again - a stop guards the session that ended, not
	 *  the process it happened to live in. */
	resume(): void {
		this.state.stopped = false;
	}

	/** Publish one state change for `session`. Errors never surface: the
	 *  state is display and evidence, never a control signal. */
	publish(
		exec: StateExec,
		session: string,
		state: RunState,
		aborted = false,
	): Promise<void> {
		if (this.state.stopped) return Promise.resolve();
		const args = [
			"state", session, state, "--seq", String(this.state.seq++),
		];
		if (aborted) args.push("--aborted");
		// A host that throws before it launches the client must not abort the
		// publish's caller, either: the caller is a turn that is about to work.
		try {
			return Promise.resolve(exec(args)).then(
				() => undefined,
				() => undefined,
			);
		} catch {
			return Promise.resolve();
		}
	}
}
