/**
 * pi-daemon — transport for the agent-comms tools.
 *
 * `pi/bin/pi-comms` is the only client of the service, so the extension
 * runs it instead of speaking `pi-rpc/1` a second time. One invocation
 * is one outcome: the parsed JSON answer, or the CLI's stderr and exit
 * code. Failures are values here, never throws, so a missing service
 * degrades to a tool error instead of breaking the turn.
 */

import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExecOptions } from "@earendil-works/pi-coding-agent";

import type { ProcessRunner } from "../daemon.ts";

/** One CLI invocation, successful or not. */
export interface CommsOutcome {
	command: string;
	argv: string[];
	exitCode: number | null;
	stdout: string;
	stderr: string;
	/** The service's answer; an array when `replay --all` printed pages. */
	answer: unknown;
	killed: boolean;
	/** Set when nothing was run: the binary is missing or unresolvable. */
	missingBinary?: string;
}

/** Runs `pi-comms --format json <command>` and reports what came back. */
export class CommsCli {
	/** The environment is read per call so a test can redirect the binary. */
	constructor(
		private readonly runner: Pick<ProcessRunner, "run">,
		private readonly env: NodeJS.ProcessEnv = process.env,
	) {}

	/** One command; `timeoutMs` bounds the whole child, not the hold. */
	async run(
		command: string,
		argv: string[],
		options: ExecOptions = {},
	): Promise<CommsOutcome> {
		const argvWithAgent = this.withAgent(argv, this.identity());
		const binary = this.binary();
		if (!existsSync(binary)) {
			return {
				command,
				argv: argvWithAgent,
				exitCode: null,
				stdout: "",
				stderr: "",
				answer: undefined,
				killed: false,
				missingBinary: binary,
			};
		}
		let result: { stdout?: string; stderr?: string; code?: number; killed?: boolean };
		try {
			result = await this.runner.run(
				binary,
				["--format", "json", ...argvWithAgent],
				options,
			);
		} catch (error) {
			return {
				command,
				argv: argvWithAgent,
				exitCode: null,
				stdout: "",
				stderr: error instanceof Error ? error.message : String(error),
				answer: undefined,
				killed: false,
			};
		}
		const stdout = result.stdout ?? "";
		const stderr = result.stderr ?? "";
		return {
			command,
			argv: argvWithAgent,
			exitCode: result.killed ? null : (result.code ?? 0),
			stdout,
			stderr,
			answer: CommsCli.parse(stdout),
			killed: Boolean(result.killed),
		};
	}

	/** The identity every command acts as: the one a launch bound this
	 *  session to, else the daemon name it is hosted under. Without it the
	 *  CLI refuses every command, so a session that cannot name itself can
	 *  neither send nor read mail. */
	identity(): string {
		return this.env.PI_COMMS_AGENT_ID
			|| this.env.PI_HOSTED_SESSION
			|| this.registered();
	}

	/** The daemon's own name for this session, read from its registry when the
	 *  launch environment names nothing. A session started by `pi-rc` carries
	 *  its session file but not the mailbox name the spawn ledger writes, so
	 *  without this the tools cannot name the agent they act as and the CLI
	 *  refuses every command. */
	private registered(): string {
		const state = this.env.PI_DAEMON_STATE_DIR;
		const mine = this.env.PI_SESSION_FILE;
		if (!state || !mine) return "";
		try {
			const registry = JSON.parse(readFileSync(
				join(state, "sessions", "sessions.json"), "utf8")) as {
					files?: Record<string, string>;
				};
			for (const [name, file] of Object.entries(registry.files ?? {})) {
				if (file === mine) return name;
			}
		} catch {
			// No registry (or no readable one): there is nothing to name with.
		}
		return "";
	}

	/** One command's argv with the identity in front of it, if there is one. */
	private withAgent(argv: string[], identity: string): string[] {
		return identity ? ["--agent", identity, ...argv] : argv;
	}

	/** `PI_COMMS_BIN`, else the bundled script, else the installed one. */
	binary(): string {
		const override = this.env.PI_COMMS_BIN;
		if (override) return override;
		const bundled = join(
			CommsCli.moduleDir(), "..", "..", "bin", "pi-comms");
		if (existsSync(bundled)) return bundled;
		return join(this.env.HOME || homedir(), ".local", "bin", "pi-comms");
	}

	/** One JSON object, or one per line for `replay --all`; undefined
	 *  when the CLI printed nothing. */
	private static parse(stdout: string): unknown {
		const text = stdout.trim();
		if (!text) return undefined;
		const lines = text.split(/\r?\n/).filter((line) => line.trim());
		if (lines.length === 1) {
			try {
				return JSON.parse(lines[0]);
			} catch {
				return undefined;
			}
		}
		const pages: unknown[] = [];
		for (const line of lines) {
			try {
				pages.push(JSON.parse(line));
			} catch {
				return undefined;
			}
		}
		return pages;
	}

	private static moduleDir(): string {
		try {
			return dirname(fileURLToPath(import.meta.url));
		} catch {
			return process.cwd();
		}
	}
}
