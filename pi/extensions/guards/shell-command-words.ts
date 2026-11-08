/**
 * Shell command words: which word of a shell segment names the program.
 *
 * Mirrored in pi-daemon/pi/extensions/guards/shell-command-words.ts; keep
 * the two copies in sync. It strips the leading assignments and control
 * words, then consumes a launcher's own options, so the caller is handed
 * the real command word first. It knows shell syntax only, never runs
 * anything, and holds no state between commands.
 */

export class ShellCommandWords {
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

	/** Skip assignments, control words, and launchers so the real command
	 *  word is first. */
	resolve(words: string[]): string[] {
		let rest = [...words];
		for (;;) {
			if (rest.length === 0) return rest;
			const word = rest[0];
			const name = this.basename(word);
			if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(word)) {
				rest = rest.slice(1);
				continue;
			}
			if (ShellCommandWords.KEYWORDS.has(word)) {
				rest = rest.slice(1);
				continue;
			}
			if (word.startsWith("-")) {
				rest = rest.slice(1);
				continue;
			}
			if (ShellCommandWords.LAUNCHERS.has(name)) {
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

	/** The word's own name, with no directory and no escaped-word marker. */
	basename(word: string): string {
		const parts = word.split(/[\\/]/);
		return (parts[parts.length - 1] ?? "").replace(/\u0001/g, "");
	}
}
