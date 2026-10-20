/**
 * Remote-work classifier: decides what one shell command does to another
 * host.
 *
 * Mirrored in pi-teams/extensions/pi-teams/remote-work-classifier.ts;
 * keep the two copies in sync. It knows only the command shapes the
 * remote-work guards act on - the ssh family, file-transfer clients, and
 * a headless pi - and never runs anything.
 *
 * Verdicts:
 *   transfer    - moving bytes to or from another host
 *   ssh-info    - version/config/key queries; no session
 *   ssh-tunnel  - a port forward with no remote command
 *   ssh-shell   - an interactive remote shell
 *   ssh-exec    - a remote command that is not transfer-only
 *   headless-pi - a pi agent run that is not a management subcommand
 *
 * This is a guard, not a sandbox: a command built at runtime (a script
 * written first, an interpreter, eval of a variable) is not seen.
 */

export type RemoteVerdict =
	| "transfer"
	| "ssh-info"
	| "ssh-tunnel"
	| "ssh-shell"
	| "ssh-exec"
	| "headless-pi";

/** Owns the verdicts for a command string; stateless. */
export class RemoteWorkClassifier {
	static readonly SSH_NAMES: ReadonlySet<string> = new Set([
		"ssh", "autossh", "mosh", "dbclient",
	]);
	static readonly SHELL_NAMES: ReadonlySet<string> = new Set([
		"bash", "sh", "zsh", "dash", "ksh",
	]);
	/** The first word of a remote command that only moves bytes. */
	static readonly TRANSFER_WORDS: ReadonlySet<string> = new Set([
		"cat", "cd", "tar", "mkdir", "test", "true",
		"sha256sum", "sha1sum", "md5sum", "stat", "cmp",
		"gzip", "gunzip", "zcat", "xz", "zstd", "base64",
	]);
	static readonly PI_MANAGEMENT: ReadonlySet<string> = new Set([
		"install", "remove", "uninstall", "update", "list", "config",
	]);
	/** Shell control words that are not the command itself. */
	static readonly KEYWORDS: ReadonlySet<string> = new Set([
		"if", "then", "else", "elif", "fi", "while", "until", "do",
		"done", "for", "in", "esac", "case", "function", "{", "}",
		"!", "time",
	]);
	static readonly LAUNCHERS: ReadonlySet<string> = new Set([
		"sudo", "env", "nohup", "exec", "command", "builtin", "time",
		"timeout", "nice", "ionice", "stdbuf", "xargs", "sshpass",
		"setsid",
	]);
	static readonly TAR_DANGEROUS: ReadonlySet<string> = new Set([
		"--to-command", "--checkpoint-action", "-I",
		"--use-compress-program", "-F", "--info-script",
		"--rsh-command",
	]);

	/** Every verdict for the command, one per command position. */
	classify(command: string): RemoteVerdict[] {
		const verdicts: RemoteVerdict[] = [];
		this.collect(command, verdicts, 0);
		return verdicts;
	}

	/** Classify a command and its substitutions and `-c` scripts. */
	private collect(
		command: string,
		out: RemoteVerdict[],
		depth: number,
	): void {
		if (depth > 4) return;
		const scan = this.scan(command);
		for (const nested of scan.nested) {
			this.collect(nested, out, depth + 1);
		}
		for (const segment of scan.segments) {
			this.classifySegment(segment, out, depth + 1);
		}
	}

	/** One command position: unwrap launchers, then dispatch on name. */
	private classifySegment(
		segment: string,
		out: RemoteVerdict[],
		depth: number,
	): void {
		const words = this.commandWords(this.words(segment));
		if (words.length === 0) return;
		const name = this.basename(words[0]);
		const args = words.slice(1);
		if (RemoteWorkClassifier.SHELL_NAMES.has(name)) {
			const script = this.scriptArg(args);
			if (script !== undefined) this.collect(script, out, depth);
			return;
		}
		if (RemoteWorkClassifier.SSH_NAMES.has(name)) {
			out.push(this.sshVerdict(args));
			return;
		}
		if (name === "scp" || name === "sftp") {
			out.push(this.transferVerdict(args));
			return;
		}
		if (name === "rsync") {
			out.push(this.rsyncVerdict(args));
			return;
		}
		if (name === "pi") {
			const verdict = this.piVerdict(args);
			if (verdict !== null) out.push(verdict);
		}
	}

	// -- ssh family --------------------------------------------------------

	/** Parse ssh options into its operands and the flags that decide the
	 *  session shape. */
	private sshOperands(args: string[]): {
		positional: string[];
		tunnel: boolean;
		info: boolean;
		remoteCommand: boolean;
	} {
		let tunnel = false;
		let info = false;
		let remoteCommand = false;
		let index = 0;
		const positional: string[] = [];
		while (index < args.length) {
			const arg = args[index];
			if (!arg.startsWith("-") || arg === "-") {
				positional.push(...args.slice(index));
				break;
			}
			if (arg.startsWith("--")) {
				index += 1;
				continue;
			}
			const letters = arg.slice(1);
			let takesNext = false;
			for (let at = 0; at < letters.length; at++) {
				const letter = letters[at];
				if (letter === "G" || letter === "V") info = true;
				if (letter === "N" || letter === "W") tunnel = true;
				if (!"BbcDEeFIiJLlmOopQRSWw".includes(letter)) {
					continue;
				}
				const attached = letters.slice(at + 1);
				const value = attached !== ""
					? attached : (args[index + 1] ?? "");
				if (letter === "O") info = true;
				if (letter === "o"
					&& /^RemoteCommand=/i.test(value.trim())) {
					remoteCommand = true;
				}
				if (attached === "") takesNext = true;
				break;
			}
			index += takesNext ? 2 : 1;
		}
		return { positional, tunnel, info, remoteCommand };
	}

	private sshVerdict(args: string[]): RemoteVerdict {
		const before = this.sshOperands(args);
		const host = before.positional[0];
		// ssh re-parses options after the host; the first non-option after
		// it starts the remote command.
		const after = this.sshOperands(before.positional.slice(1));
		if (before.info || after.info) return "ssh-info";
		if (before.remoteCommand || after.remoteCommand) return "ssh-exec";
		if (host === undefined) return "ssh-shell";
		if (before.tunnel || after.tunnel) return "ssh-tunnel";
		const remote = after.positional.join(" ").trim();
		if (remote === "") return "ssh-shell";
		return this.remoteIsTransfer(remote) ? "transfer" : "ssh-exec";
	}

	/** True when every segment of a remote command only moves bytes. */
	private remoteIsTransfer(remote: string): boolean {
		const scan = this.scan(remote);
		if (scan.nested.length > 0 || scan.segments.length === 0) {
			return false;
		}
		let sawCommand = false;
		for (const segment of scan.segments) {
			const words = this.commandWords(this.words(segment));
			if (words.length === 0) continue;
			sawCommand = true;
			const name = this.basename(words[0]);
			if (!RemoteWorkClassifier.TRANSFER_WORDS.has(name)) {
				return false;
			}
			if (name === "tar" && this.tarRunsCommands(words.slice(1))) {
				return false;
			}
		}
		return sawCommand;
	}

	/** tar options that execute a program instead of writing bytes. */
	private tarRunsCommands(args: string[]): boolean {
		return args.some((arg) => {
			const name = arg.split("=")[0];
			return RemoteWorkClassifier.TAR_DANGEROUS.has(name);
		});
	}

	// -- transfer clients --------------------------------------------------

	private transferVerdict(args: string[]): RemoteVerdict {
		for (let index = 0; index < args.length; index++) {
			const arg = args[index];
			const program = arg === "-S" ? (args[index + 1] ?? "")
				: arg.startsWith("-S") ? arg.slice(2) : "";
			if (program !== ""
				&& !RemoteWorkClassifier.SSH_NAMES
					.has(this.basename(program))) {
				return "ssh-exec";
			}
		}
		return "transfer";
	}

	private rsyncVerdict(args: string[]): RemoteVerdict {
		let shell = "";
		let remotePath = "";
		for (let index = 0; index < args.length; index++) {
			const arg = args[index];
			if (arg === "-e" || arg === "--rsh") {
				shell = args[index + 1] ?? "";
			} else if (arg.startsWith("--rsh=")) {
				shell = arg.slice("--rsh=".length);
			} else if (arg.startsWith("-e") && arg.length > 2) {
				shell = arg.slice(2);
			} else if (arg === "--rsync-path") {
				remotePath = args[index + 1] ?? "";
			} else if (arg.startsWith("--rsync-path=")) {
				remotePath = arg.slice("--rsync-path=".length);
			}
		}
		if (remotePath !== ""
			&& !/^[A-Za-z0-9_./\\-]*rsync(\.exe)?$/.test(remotePath)) {
			return "ssh-exec";
		}
		if (shell === "") return "transfer";
		const words = this.commandWords(this.words(shell));
		if (words.length === 0) return "ssh-exec";
		if (!RemoteWorkClassifier.SSH_NAMES
			.has(this.basename(words[0]))) {
			return "ssh-exec";
		}
		return this.sshOperands(words.slice(1)).positional.length >= 2
			? "ssh-exec" : "transfer";
	}

	// -- headless pi -------------------------------------------------------

	/** A pi management subcommand is not an agent run; anything else is. */
	private piVerdict(args: string[]): RemoteVerdict | null {
		// An explicit headless form is an agent run whatever follows it.
		if (args.some((arg) => arg === "-p" || arg === "--print"
			|| arg === "--mode" || arg.startsWith("--mode="))) {
			return "headless-pi";
		}
		const plain = args.filter((arg) => !arg.startsWith("-"));
		if (plain.length === 0) {
			const metaOnly = args.length > 0 && args.every((arg) =>
				["-v", "--version", "-h", "--help"].includes(arg));
			return metaOnly ? null : "headless-pi";
		}
		return RemoteWorkClassifier.PI_MANAGEMENT.has(plain[0])
			? null : "headless-pi";
	}

	// -- shell text --------------------------------------------------------

	/** Split into command positions and collect substitution bodies. */
	private scan(text: string): { segments: string[]; nested: string[] } {
		const segments: string[] = [];
		const nested: string[] = [];
		let current = "";
		let quote: string | null = null;
		let index = 0;
		const push = (): void => {
			const segment = current.trim();
			if (segment !== "") segments.push(segment);
			current = "";
		};
		while (index < text.length) {
			const char = text[index];
			if (quote === "'") {
				current += char;
				if (char === "'") quote = null;
				index += 1;
				continue;
			}
			if (quote === '"') {
				if (char === "\\") {
					current += text.slice(index, index + 2);
					index += 2;
					continue;
				}
				if (char === '"') {
					quote = null;
					current += char;
					index += 1;
					continue;
				}
				if (char === "$" && text[index + 1] === "(") {
					const inner = this.balanced(text, index + 2, "(", ")");
					nested.push(inner.body);
					current += `$(${inner.body})`;
					index = inner.end;
					continue;
				}
				current += char;
				index += 1;
				continue;
			}
			if (char === "'" || char === '"') {
				quote = char;
				current += char;
				index += 1;
				continue;
			}
			if (char === "\\") {
				current += text.slice(index, index + 2);
				index += 2;
				continue;
			}
			if (char === "$" && text[index + 1] === "(") {
				const inner = this.balanced(text, index + 2, "(", ")");
				nested.push(inner.body);
				current += `$(${inner.body})`;
				index = inner.end;
				continue;
			}
			if (char === "`") {
				const close = text.indexOf("`", index + 1);
				const end = close < 0 ? text.length : close;
				nested.push(text.slice(index + 1, end));
				current += text.slice(index, end + 1);
				index = end + 1;
				continue;
			}
			if (char === ";" || char === "\n") {
				push();
				index += 1;
				continue;
			}
			if (char === "(" || char === ")") {
				push();
				index += 1;
				continue;
			}
			if (char === "&" || char === "|") {
				push();
				index += text[index + 1] === char ? 2 : 1;
				continue;
			}
			current += char;
			index += 1;
		}
		push();
		return { segments, nested };
	}

	/** The body of a balanced `$(...)` with the index just past it. */
	private balanced(
		text: string,
		start: number,
		open: string,
		close: string,
	): { body: string; end: number } {
		let depth = 1;
		let quote: string | null = null;
		let index = start;
		while (index < text.length) {
			const char = text[index];
			if (quote !== null) {
				if (char === quote) quote = null;
				else if (char === "\\") index += 1;
				index += 1;
				continue;
			}
			if (char === "'" || char === '"') {
				quote = char;
			} else if (char === open) {
				depth += 1;
			} else if (char === close) {
				depth -= 1;
				if (depth === 0) {
					return { body: text.slice(start, index), end: index + 1 };
				}
			}
			index += 1;
		}
		return { body: text.slice(start), end: text.length };
	}

	/** Words of one segment, quotes removed and redirections dropped. */
	private words(segment: string): string[] {
		const words: string[] = [];
		let current = "";
		let quote: string | null = null;
		let index = 0;
		const flush = (): void => {
			if (current !== "") words.push(current);
			current = "";
		};
		while (index < segment.length) {
			const char = segment[index];
			if (quote === "'") {
				if (char === "'") quote = null;
				else current += char;
				index += 1;
				continue;
			}
			if (quote === '"') {
				if (char === "\\") {
					current += segment[index + 1] ?? "";
					index += 2;
					continue;
				}
				if (char === '"') quote = null;
				else current += char;
				index += 1;
				continue;
			}
			if (char === "'" || char === '"') {
				quote = char;
				index += 1;
				continue;
			}
			if (char === "\\") {
				current += segment[index + 1] ?? "";
				index += 2;
				continue;
			}
			if (/\s/.test(char)) {
				flush();
				index += 1;
				continue;
			}
			current += char;
			index += 1;
		}
		flush();
		return this.withoutRedirections(words);
	}

	private withoutRedirections(words: string[]): string[] {
		const kept: string[] = [];
		for (let index = 0; index < words.length; index++) {
			const word = words[index];
			if (/^\d*(>>?|<<?)(&\d+)?$/.test(word)) {
				if (/^\d*(>>?|<<?)$/.test(word)) index += 1;
				continue;
			}
			if (/^\d*(>>?|<<?).+/.test(word)) continue;
			kept.push(word);
		}
		return kept;
	}

	// -- launchers ---------------------------------------------------------

	/** Skip assignments, control words, and launchers so the real command
	 *  word is first. */
	private commandWords(words: string[]): string[] {
		let rest = [...words];
		for (;;) {
			if (rest.length === 0) return rest;
			const word = rest[0];
			const name = this.basename(word);
			if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(word)) {
				rest = rest.slice(1);
				continue;
			}
			if (RemoteWorkClassifier.KEYWORDS.has(word)) {
				rest = rest.slice(1);
				continue;
			}
			if (word.startsWith("-")) {
				rest = rest.slice(1);
				continue;
			}
			if (RemoteWorkClassifier.LAUNCHERS.has(name)) {
				rest = this.skipLauncher(name, rest.slice(1));
				continue;
			}
			return rest;
		}
	}

	private skipLauncher(name: string, args: string[]): string[] {
		let rest = [...args];
		while (rest.length > 0) {
			const arg = rest[0];
			if (arg === "--") return rest.slice(1);
			if (!arg.startsWith("-") || arg === "-") break;
			rest = rest.slice(1);
			if (this.launcherOptionTakesValue(name, arg)) {
				rest = rest.slice(1);
			}
		}
		// timeout's first operand is a duration, never the command.
		if (name === "timeout" && rest.length > 0) rest = rest.slice(1);
		return rest;
	}

	private launcherOptionTakesValue(name: string, arg: string): boolean {
		if (arg.startsWith("--")) {
			const [long, inline] = arg.slice(2).split("=");
			if (inline !== undefined) return false;
			return this.launcherLongValue(name, long);
		}
		const letter = arg[1];
		if (!this.launcherShortValues(name).includes(letter)) return false;
		return arg.length === 2;
	}

	private launcherShortValues(name: string): string {
		switch (name) {
			case "sudo": return "ugpCTrUt";
			case "env": return "uCS";
			case "timeout": return "sk";
			case "nice": return "n";
			case "ionice": return "cnpu";
			case "stdbuf": return "ioe";
			case "xargs": return "nIPEsLdas";
			case "sshpass": return "pfd";
			default: return "";
		}
	}

	private launcherLongValue(name: string, long: string): boolean {
		switch (name) {
			case "sudo":
				return ["user", "group", "prompt", "close-from", "chdir",
					"role", "type", "other-user", "host"].includes(long);
			case "env":
				return ["unset", "chdir", "split-string"].includes(long);
			case "timeout":
				return ["signal", "kill-after"].includes(long);
			case "nice":
				return long === "adjustment";
			case "ionice":
				return ["class", "classdata", "pid", "pgid"].includes(long);
			case "stdbuf":
				return ["input", "output", "error"].includes(long);
			case "xargs":
				return ["delimiter", "max-args", "max-procs", "max-chars",
					"arg-file", "replace", "eof"].includes(long);
			case "sshpass":
				return long === "password";
			default:
				return false;
		}
	}

	/** The argument of a `-c` script flag, if the shell has one. */
	private scriptArg(args: string[]): string | undefined {
		for (let index = 0; index < args.length; index++) {
			const arg = args[index];
			if (arg === "--") return undefined;
			if (!arg.startsWith("-")) return undefined;
			if (arg === "-c" || /^-[A-Za-z]*c[A-Za-z]*$/.test(arg)) {
				return args[index + 1] ?? "";
			}
		}
		return undefined;
	}

	private basename(word: string): string {
		const parts = word.split(/[\\/]/);
		return parts[parts.length - 1] ?? "";
	}
}
