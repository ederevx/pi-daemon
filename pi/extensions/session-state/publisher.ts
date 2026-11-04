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
 * - publishing stops at session_shutdown, which pi always emits before
 *   it disposes the runtime. A turn killed mid-flight therefore keeps its
 *   `busy` record and is reported as a death, never as a settle;
 * - `aborted` rides with an idle publish, so an interrupted turn is
 *   reported as run-aborted instead of settled.
 *
 * The shared instance is the process's one runtime, and it owns the
 * counter and the stop flag. Executing `pi-rc state` is injected, so this
 * module owns the protocol and nothing about how a host launches the
 * client.
 */

export type StateExec = (args: string[]) => Promise<unknown>;

export type RunState = "busy" | "idle" | "waiting";

export class RunStatePublisher {
	/** Every publisher in this process stamps from this one counter. */
	static readonly shared = new RunStatePublisher();

	private seq = 0;
	private stopped = false;

	/** Stop every publish for the rest of the process (session_shutdown):
	 *  the turn that is still running keeps its `busy` record. */
	stop(): void {
		this.stopped = true;
	}

	/** Publish one state change for `session`. Errors never surface: the
	 *  state is display and evidence, never a control signal. */
	publish(
		exec: StateExec,
		session: string,
		state: RunState,
		aborted = false,
	): Promise<void> {
		if (this.stopped) return Promise.resolve();
		const args = ["state", session, state, "--seq", String(this.seq++)];
		if (aborted) args.push("--aborted");
		return Promise.resolve(exec(args)).then(
			() => undefined,
			() => undefined,
		);
	}
}
