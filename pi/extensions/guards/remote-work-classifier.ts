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
 * A heredoc body is data, not command positions, unless a shell on that
 * line reads its standard input as a program; an unquoted body still runs
 * the substitutions it carries.
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

/** A heredoc whose body starts after the newline of its own line. */
type PendingHeredoc = {
	delimiter: string;
	quoted: boolean;
	stripTabs: boolean;
	/** Segments already collected for the declaring command. */
	lineStart: number;
	/** How much of that command's text came before the declaration. */
	offset: number;
	/** The descriptor the body was written to; empty means standard input. */
	descriptor: string;
	/** The rest of the command's own text, where later redirections sit. */
	rest: string;
};

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
		const bounds: string[] = [];
		const nested: string[] = [];
		let current = "";
		let quote: string | null = null;
		let pending: PendingHeredoc[] = [];
		let index = 0;
		const push = (bound: string): void => {
			const segment = current.trim();
			if (segment !== "") {
				segments.push(segment);
				bounds.push(bound);
			}
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
			if (char === "<" && text[index + 1] === "<"
				&& text[index + 2] !== "<") {
				const declared = this.heredocWord(text, index + 2);
				if (declared !== undefined) {
					const descriptor = /(?:^|\s)(\d*)$/.exec(current)?.[1] ?? "";
					current = current.slice(0, current.length - descriptor.length);
					pending.push({
						delimiter: declared.delimiter,
						quoted: declared.quoted,
						stripTabs: declared.stripTabs,
						lineStart: segments.length,
						offset: current.trim().length,
						descriptor,
						rest: this.commandRest(text, declared.end),
					});
					index = declared.end;
					continue;
				}
			}
			if (char === ";" || char === "\n") {
				push(char);
				index += 1;
				if (char === "\n" && pending.length > 0) {
					index = this.consumeHeredocs(
						text, index, pending, segments, bounds, nested);
					pending = [];
				}
				continue;
			}
			if (char === "(" || char === ")") {
				push(char);
				index += 1;
				continue;
			}
			// `<&3` copies a descriptor; only a bare `&` backgrounds the command.
			if ((char === "&" && !/[<>]/.test(text[index - 1] ?? ""))
				|| char === "|") {
				const doubled = text[index + 1] === char;
				push(doubled ? char + char : char);
				index += doubled ? 2 : 1;
				continue;
			}
			current += char;
			index += 1;
		}
		push("");
		if (pending.length > 0) {
			this.consumeHeredocs(text, index, pending, segments, bounds, nested);
		}
		return { segments, nested };
	}

	/** The `<<` operand: an optional `-`, the delimiter, and its end. */
	private heredocWord(
		text: string,
		start: number,
	): {
		delimiter: string;
		quoted: boolean;
		stripTabs: boolean;
		end: number;
	} | undefined {
		let index = start;
		const stripTabs = text[index] === "-";
		if (stripTabs) index += 1;
		while (text[index] === " " || text[index] === "\t") index += 1;
		let delimiter = "";
		let quoted = false;
		let quote: string | null = null;
		while (index < text.length) {
			const char = text[index];
			if (quote === null) {
				if (char === "'" || char === '"') {
					quoted = true;
					quote = char;
					index += 1;
					continue;
				}
				if (char === "\\") {
					quoted = true;
					delimiter += text[index + 1] ?? "";
					index += 2;
					continue;
				}
				if (/[\s;|&<>()]/.test(char)) break;
				delimiter += char;
				index += 1;
				continue;
			}
			if (char === quote) {
				quote = null;
				index += 1;
				continue;
			}
			if (quote === '"' && char === "\\") {
				delimiter += text[index + 1] ?? "";
				index += 2;
				continue;
			}
			delimiter += char;
			index += 1;
		}
		return delimiter === "" && !quoted ? undefined
			: { delimiter, quoted, stripTabs, end: index };
	}

	/** Take the queued bodies, routing each by the command that reads it. */
	private consumeHeredocs(
		text: string,
		start: number,
		pending: PendingHeredoc[],
		segments: string[],
		bounds: string[],
		nested: string[],
	): number {
		let index = start;
		for (let at = 0; at < pending.length; at++) {
			const entry = pending[at];
			const body = this.heredocBody(text, index, entry);
			index = body.end;
			// The declaring command, and the commands a pipe feeds from it.
			const command = [segments[entry.lineStart] ?? ""];
			for (let later = entry.lineStart + 1; later < segments.length
				&& bounds[later - 1] === "|"; later++) {
				// A command that redirects its own stdin does not read the pipe.
				const declared = pending
					.filter((other) => other.lineStart === later)
					.map((other) => ({ at: other.offset, to: other.descriptor }));
				if (!this.readsPipeline(segments[later] ?? "", declared)) break;
				command.push(segments[later]);
			}
			const program = this.suppliesStdin(entry)
				&& this.commandRunsStdin(command);
			if (program) nested.push(body.text);
			// An unquoted body expands even when a shell reads it as a program,
			// and an escaped substitution is left for that shell to resolve.
			if (!entry.quoted) nested.push(...this.expansions(body.raw));
		}
		return index;
	}

	/** The body up to its delimiter line, as the command receives it. */
	private heredocBody(
		text: string,
		start: number,
		entry: PendingHeredoc,
	): { raw: string; text: string; end: number } {
		const lines: string[] = [];
		let index = start;
		while (index < text.length) {
			let line = this.heredocLine(text, index, entry);
			index = line.end;
			// A trailing backslash joins the next line to this one, and the
			// line the join forms may carry the delimiter at its end.
			while (!entry.quoted && this.continues(line.text)) {
				line.text = line.text.slice(0, -1);
				const next = this.heredocLine(text, index, entry);
				line = { text: line.text + next.text, end: next.end };
				index = line.end;
			}
			if (line.text === entry.delimiter) {
				return this.heredocResult(lines, entry, index);
			}
			lines.push(line.text);
		}
		return this.heredocResult(lines, entry, text.length);
	}

	/** One raw line of a body, its tabs stripped when `<<-` asked for it. */
	private heredocLine(
		text: string,
		start: number,
		entry: PendingHeredoc,
	): { text: string; end: number } {
		const stop = text.indexOf("\n", start);
		const end = stop < 0 ? text.length : stop;
		const raw = text.slice(start, end);
		return {
			text: entry.stripTabs ? raw.replace(/^\t+/, "") : raw,
			end: Math.min(end + 1, text.length),
		};
	}

	/** True when the line ends in an unescaped backslash. */
	private continues(line: string): boolean {
		const trailing = /\\+$/.exec(line)?.[0].length ?? 0;
		return trailing % 2 === 1;
	}

	/** The body as written, and as delivered: an unquoted one loses its escapes. */
	private heredocResult(
		lines: string[],
		entry: PendingHeredoc,
		end: number,
	): { raw: string; text: string; end: number } {
		const raw = lines.join("\n");
		return {
			raw,
			text: entry.quoted ? raw : raw.replace(/\\([$`\\])/g, "$1"),
			end,
		};
	}

	/** Substitution bodies an unquoted heredoc still runs. */
	private expansions(text: string): string[] {
		const bodies: string[] = [];
		let index = 0;
		while (index < text.length) {
			if (text[index] === "\\") {
				index += 2;
				continue;
			}
			if (text[index] === "$" && text[index + 1] === "(") {
				const inner = this.balanced(text, index + 2, "(", ")");
				bodies.push(inner.body);
				index = inner.end;
				continue;
			}
			if (text[index] === "`") {
				const close = text.indexOf("`", index + 1);
				const end = close < 0 ? text.length : close;
				bodies.push(text.slice(index + 1, end));
				index = end + 1;
				continue;
			}
			index += 1;
		}
		return bodies;
	}

	/** True when a command in the pipeline reads stdin as a shell program. */
	private commandRunsStdin(segments: string[]): boolean {
		return segments.some((segment) => {
			const words = this.commandWords(this.words(segment));
			return words.length > 0 && this.stdinProgram(words);
		});
	}

	/** The rest of the declaring command, up to the next `;` or pipe. */
	private commandRest(text: string, start: number): string {
		let quote: string | null = null;
		for (let index = start; index < text.length; index++) {
			const char = text[index];
			if (char === "\\") {
				index += 1;
				continue;
			}
			if (quote !== null) {
				if (char === quote) quote = null;
				continue;
			}
			if (char === "'" || char === "\"") {
				quote = char;
				continue;
			}
			if ((char === "$" || char === "<" || char === ">")
				&& text[index + 1] === "(") {
				// A separator inside a substitution belongs to that command.
				index = this.substitutionEnd(text, index + 1);
				continue;
			}
			if (char === "`") {
				const end = text.indexOf("`", index + 1);
				index = end < 0 ? text.length : end;
				continue;
			}
			if (char === "#" && (index === start || /\s/.test(text[index - 1] ?? ""))) {
				return text.slice(start, index);
			}
			if (char === ";" || char === "|" || char === "\n") {
				return text.slice(start, index);
			}
			if (char === "&" && text[index - 1] !== "<" && text[index - 1] !== ">") {
				return text.slice(start, index);
			}
		}
		return text.slice(start);
	}

	/** True when the redirections leave stdin reading the body itself. */
	private suppliesStdin(entry: PendingHeredoc): boolean {
		const read = new Map<string, string>([[this.descriptorKey(entry.descriptor), "body"]]);
		this.applyRedirections(entry.rest, read);
		return read.get("0") === "body";
	}

	/** A descriptor spelled one way, so `03` and `3` are the same stream. */
	private descriptorKey(digits: string | undefined): string {
		return String(Number(digits || "0"));
	}

	/** True when a command keeps the stdin a pipe hands it. */
	private readsPipeline(
		text: string,
		declared: Array<{ at: number; to: string }>,
	): boolean {
		const read = new Map<string, string>([["0", "pipe"]]);
		this.applyRedirections(text, read, declared);
		return read.get("0") === "pipe";
	}

	/**
	 * Follow a command's redirections, recording what each descriptor reads.
	 * A heredoc's own redirection is missing from the text, so `declared` puts
	 * it back where it stood.
	 */
	private applyRedirections(
		text: string,
		read: Map<string, string>,
		declared: Array<{ at: number; to: string }> = [],
	): void {
		const where = (digits: string | undefined, fallback: string): string =>
			this.descriptorKey(digits || fallback);
		let quote: string | null = null;
		// Where the operand of the previous redirection ended.
		let consumed = -1;
		for (let index = 0; index <= text.length; index++) {
			for (const heredoc of declared) {
				if (heredoc.at === index) read.set(where(heredoc.to, "0"), "other");
			}
			const char = text[index] ?? "";
			if (char === "\\") {
				index += 1;
				continue;
			}
			if (quote !== null) {
				if (char === quote) quote = null;
				continue;
			}
			if (char === "'" || char === "\"") {
				quote = char;
				continue;
			}
			if (char === "#" && (index === 0 || /\s/.test(text[index - 1] ?? ""))) {
				// A word that starts with `#` begins a comment, which runs to the
				// end of the line and carries no redirection of its own.
				const end = text.indexOf("\n", index);
				index = end < 0 ? text.length : end;
				continue;
			}
			if (char === "$" && text[index + 1] === "(") {
				index = this.substitutionEnd(text, index + 1);
				continue;
			}
			if (char === "`") {
				const end = text.indexOf("`", index + 1);
				index = end < 0 ? text.length : end;
				continue;
			}
			if (char !== "<" && char !== ">") continue;
			const target = where(this.descriptorNumber(text, index, consumed),
				char === "<" ? "0" : "1");
			const next = text.slice(index + 1);
			if (next.startsWith("(")) {
				index = this.substitutionEnd(text, index + 1);
				continue;
			}
			if (next.startsWith(char)) {
				consumed = index + 2 + this.redirectionOperand(next.slice(1)).end;
				index += 1;
				read.set(target, "other");
				continue;
			}
			if (next.startsWith("&")) {
				const operand = this.descriptorOperand(next.slice(1));
				const moved = operand.value;
				consumed = index + 2 + operand.end;
				const digits = moved.replace(/-$/, "");
				if (digits === "") {
					read.set(target, "other");
					continue;
				}
				const source = where(digits, "0");
				read.set(target, read.get(source) ?? "other");
				if (moved !== digits) read.set(source, "other");
				continue;
			}
			consumed = index + 1 + this.redirectionOperand(next).end;
			read.set(target, "other");
		}
	}

	/** The index of the parenthesis closing the substitution at `start`. */
	private substitutionEnd(text: string, start: number): number {
		let depth = 0;
		let quote: string | null = null;
		for (let index = start; index < text.length; index++) {
			const char = text[index] ?? "";
			if (char === "\\") {
				index += 1;
				continue;
			}
			if (quote !== null) {
				if (char === quote) quote = null;
				continue;
			}
			if (char === "'" || char === "\"") {
				quote = char;
				continue;
			}
			if (char === "(") depth += 1;
			if (char === ")" && --depth === 0) return index;
		}
		return text.length;
	}

	/** The digits a duplication's operand names, after quote removal. */
	/** The descriptor written as its own token before a redirection operator. */
	private descriptorNumber(text: string, index: number, consumed: number): string | undefined {
		let at = index;
		while (at > consumed && /\d/.test(text[at - 1] ?? "")) at -= 1;
		if (at === index) return undefined;
		// A number glued to a word is part of that word, as in `>out3`.
		const before = text[at - 1];
		if (before !== undefined && !/[\s;&|()<>]/.test(before)) return undefined;
		return text.slice(at, index);
	}

	/** The word a redirection takes, and where it ends in the source. */
	private redirectionOperand(text: string): { word: string; end: number } {
		let word = "";
		let end = text.length;
		for (let index = 0; index < text.length; index++) {
			const char = text[index] ?? "";
			if (char === "\\") {
				index += 1;
				// A backslash-newline continues the word and leaves nothing behind.
				if (text[index] !== "\n") word += text[index] ?? "";
				continue;
			}
			if (char === "$" && text[index + 1] === "'") {
				const quoted = this.ansiQuoted(text.slice(index + 2));
				word += quoted.word;
				index += 1 + quoted.consumed;
				continue;
			}
			if (char === "'" || char === "\"") {
				const close = text.indexOf(char, index + 1);
				word += this.quotedWord(
					text.slice(index + 1, close < 0 ? text.length : close), char);
				index = close < 0 ? text.length : close;
				continue;
			}
			if (word === "" && /\s/.test(char)) continue;
			if (/[\s;|&<>()]/.test(char)) {
				end = index;
				break;
			}
			word += char;
		}
		return { word, end };
	}

	/** An ANSI-C quoted word's value and the source it spans. */
	private ansiQuoted(text: string): { word: string; consumed: number } {
		let word = "";
		for (let index = 0; index < text.length; index++) {
			const char = text[index];
			if (char === "'") return { word, consumed: index + 1 };
			if (char !== "\\") {
				word += char;
				continue;
			}
			index += 1;
			const slice = text.slice(index, index + 9);
			const code = /^u([0-9a-fA-F]{1,4})/.exec(slice)?.[1]
				?? /^U([0-9a-fA-F]{1,8})/.exec(slice)?.[1];
			const point = code === undefined ? -1 : parseInt(code, 16);
			if (point >= 0 && point <= 0x10ffff) {
				index += code.length;
				word += String.fromCodePoint(point);
				continue;
			}
			const octal = /^[0-7]{1,3}/.exec(text.slice(index, index + 3))?.[0];
			if (octal !== undefined) {
				index += octal.length - 1;
				word += String.fromCharCode(parseInt(octal, 8));
				continue;
			}
			const hex = /^x([0-9a-fA-F]{1,2})/.exec(text.slice(index, index + 3))?.[1];
			if (hex !== undefined) {
				index += hex.length;
				word += String.fromCharCode(parseInt(hex, 16));
				continue;
			}
			const escaped = text[index] ?? "";
			word += /['"\\?]/.test(escaped) ? escaped : "\\" + escaped;
		}
		return { word, consumed: text.length };
	}

	/** A quoted word's value: single quotes are literal, double quotes decode. */
	private quotedWord(content: string, quote: string): string {
		if (quote === "'") return content;
		let word = "";
		for (let index = 0; index < content.length; index++) {
			const char = content[index];
			if (char !== "\\") {
				word += char;
				continue;
			}
			const escaped = content[index + 1] ?? "";
			index += 1;
			// A backslash-newline continues the word and leaves nothing behind.
			if (escaped === "\n") continue;
			// Inside double quotes a backslash is special only before these.
			word += /[$`"\\]/.test(escaped) ? escaped : "\\" + escaped;
		}
		return word;
	}

	/** The descriptor an `&` copy reads by name, if it is one. */
	private descriptorOperand(text: string): { value: string; end: number } {
		const { word, end } = this.redirectionOperand(text);
		return { value: /^\d+-?$/.test(word) ? word : "", end };
	}

	/** A shell with no script operand runs the program on its stdin. */
	private stdinProgram(words: string[]): boolean {
		if (!RemoteWorkClassifier.SHELL_NAMES.has(this.basename(words[0]))) {
			return false;
		}
		// Options are read in order; the first operand names a script unless
		// `-s` already asked for the program on stdin.
		let stdin = false;
		let rest = false;
		const args = words.slice(1);
		for (let at = 0; at < args.length; at++) {
			const arg = args[at];
			// Quotes are marked in `arg`; the value a shell passes carries none.
			const value = arg.replace(/\u0001/g, "");
			if (rest) {
				// After `--` every word is an operand, but one that may vanish
				// cannot stand for a script.
				if (!this.mayVanish(arg)) return stdin;
				continue;
			}
			if (value === "--") {
				rest = true;
				continue;
			}
			if (value === "-") continue;
			if (value.startsWith("--")) {
				if (/^--(rcfile|init-file)$/.test(value)) at += 1;
				continue;
			}
			if (!/^[-+][A-Za-z]/.test(value)) {
				// A substitution may expand to nothing, leaving no operand at all.
				if (this.mayVanish(arg)) continue;
				return stdin;
			}
			// Short-option bundles are read a letter at a time, and a leading `+`
			// turns an option off instead of on.
			const enabled = value[0] === "-";
			const flags = value.slice(1).split("");
			for (let flag = 0; flag < flags.length; flag++) {
				if (flags[flag] === "c") return false;
				if (flags[flag] === "s") stdin = enabled;
				if (flags[flag] !== "o" && flags[flag] !== "O") continue;
				// `-o` names an option, and the rest of the bundle or the next
				// word is its value.
				if (flag + 1 === flags.length) at += 1;
				break;
			}
		}
		return true;
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
				current += "\u0001";
				index += 1;
				continue;
			}
			if (char === "#" && current === "") {
				// A word that starts with `#` begins a comment, which runs to the
				// end of the line and carries no syntax of its own.
				const end = segment.indexOf("\n", index);
				index = end < 0 ? segment.length : end;
				continue;
			}
			if (char === "\\") {
				// A backslash-newline continues the word and leaves nothing behind.
				if (segment[index + 1] !== "\n") current += segment[index + 1] ?? "";
				index += 2;
				continue;
			}
			if ((char === "$" || char === "<" || char === ">")
				&& segment[index + 1] === "(") {
				const end = this.substitutionEnd(segment, index + 1) + 1;
				current += segment.slice(index, end);
				index = end;
				continue;
			}
			if (char === "`") {
				const end = segment.indexOf("`", index + 1) + 1;
				current += segment.slice(index, end === 0 ? segment.length : end);
				index = end === 0 ? segment.length : end;
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

	/** True when the whole word can expand to nothing at all. */
	private mayVanish(word: string): boolean {
		let index = 0;
		while (index < word.length) {
			if (!word.startsWith("$(", index) && word[index] !== "`") return false;
			const end = word[index] === "`"
				? word.indexOf("`", index + 1)
				: this.substitutionEnd(word, index + 1);
			if (end < 0) return false;
			index = end + 1;
		}
		return word.length > 0;
	}

	private withoutRedirections(words: string[]): string[] {
		const kept: string[] = [];
		for (let index = 0; index < words.length; index++) {
			const word = words[index];
			if (/^\d*(>>?|<<?)(&\d+)?$/.test(word)) {
				if (/^\d*(>>?|<<?)$/.test(word)) index += 1;
				continue;
			}
			if (/^\d*(>>?|<<?)&$/.test(word)) {
				// `3<& 3` names its source in the next word.
				index += 1;
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
		return (parts[parts.length - 1] ?? "").replace(/\u0001/g, "");
	}
}
