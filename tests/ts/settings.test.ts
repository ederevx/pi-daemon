/**
 * pi-daemon — settings dock tests.
 *
 * SettingsStore must write the piDaemon namespace atomically while
 * preserving every other key and the file mode, and must never overwrite
 * an unreadable file. DaemonSettingsPresenter must own the full row set
 * (with env pinning), the non-TUI listing, and the TUI view; the
 * daemon-settings command must persist a change and report it.
 */

import {
	chmodSync,
	existsSync,
	mkdirSync,
	readFileSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { delimiter, join } from "node:path";
import {
	scratchDir,
	test,
	assert,
	assertEq,
	assertMatches,
	withEnv,
} from "./harness.ts";
import { SettingsStore } from "../../pi/extensions/settings/store.ts";
import { DaemonSettingsPresenter } from "../../pi/extensions/settings/presenter.ts";
import type { DaemonSettingsView } from "../../pi/extensions/settings/view.ts";
import { default as factory } from "../../pi/extensions/daemon.ts";

const ROW_IDS = [
	"idleReapHours",
	"idleWarnGraceHours",
	"daemonIdleTimeoutHours",
	"minReviveLifeSeconds",
	"reloadGuardGraceSeconds",
	"reloadSignalGraceSeconds",
	"ticketTtlHours",
	"ticketGcSeconds",
	"extWatch",
	"extWatchIntervalSeconds",
	"extWatchDebounceSeconds",
	"extWatchRoots",
	"offload.enabled",
	"offload.waitSeconds",
];

/** The daemon-owned and extension-owned env vars, cleared for hermetic
 *  default-value assertions. */
const ALL_ENV = {
	PI_PTYD_IDLE_REAP_HOURS: undefined,
	PI_PTYD_IDLE_WARN_HOURS: undefined,
	PI_DAEMON_IDLE_TIMEOUT_HOURS: undefined,
	PI_PTYD_MIN_REVIVE_LIFE: undefined,
	PI_PTYD_RELOAD_GUARD_GRACE: undefined,
	PI_PTYD_RELOAD_SIGNAL_GRACE: undefined,
	PI_PTYD_TICKET_TTL_HOURS: undefined,
	PI_PTYD_TICKET_GC: undefined,
	PI_PTYD_EXT_WATCH: undefined,
	PI_PTYD_EXT_WATCH_INTERVAL: undefined,
	PI_PTYD_EXT_WATCH_DEBOUNCE: undefined,
	PI_PTYD_EXT_WATCH_ROOTS: undefined,
	PI_OFFLOAD: undefined,
	PI_OFFLOAD_WAIT: undefined,
};

function theme(): { fg: (color: string, text: string) => string } {
	return { fg: (_color, text) => text };
}

/** Minimal pi surface for the command-registration and e2e tests. */
class FakePi {
	readonly commands = new Map<string, unknown>();
	on(): void {}
	registerCommand(name: string, def: unknown): void {
		this.commands.set(name, def);
	}
	async exec(): Promise<{ code: number; stdout: string; stderr: string }> {
		return { code: 0, stdout: "", stderr: "" };
	}
	async sendUserMessage(): Promise<void> {}
	async sendMessage(): Promise<void> {}
}

test("settings: rows cover every piDaemon setting in order", async () => {
	const dir = join(scratchDir(), "rows-agent");
	await withEnv({ ...ALL_ENV, PI_CODING_AGENT_DIR: dir }, () => {
		const rows = new DaemonSettingsPresenter().rows();
		assertEq(rows.map((row) => row.id).join(","), ROW_IDS.join(","),
			"row ids and order");
		const watch = rows.find((row) => row.id === "extWatch")!;
		assertEq((watch.values ?? []).join(","), "off,on", "flag cycles off/on");
		assertEq(watch.value, "on", "extWatch default is on");
		const reap = rows.find((row) => row.id === "idleReapHours")!;
		assert(reap.submenu !== undefined, "number row opens an editor");
		assertEq(reap.value, "12", "idle reap default");
		const wait = rows.find((row) => row.id === "offload.waitSeconds")!;
		assertEq(wait.value, "120", "offload wait default from offload.ts");
		const roots = rows.find((row) => row.id === "extWatchRoots")!;
		assert(roots.value.includes(delimiter), "roots render path-joined");
	});
});

test("settings: env vars pin rows and win the effective value", async () => {
	const dir = join(scratchDir(), "pin-agent");
	await withEnv({
		...ALL_ENV,
		PI_CODING_AGENT_DIR: dir,
		PI_PTYD_IDLE_REAP_HOURS: "99",
		PI_PTYD_EXT_WATCH: "off",
	}, () => {
		const rows = new DaemonSettingsPresenter().rows();
		const reap = rows.find((row) => row.id === "idleReapHours")!;
		assertEq(reap.label, "Idle reap (h) (env-pinned)");
		assertEq(reap.value, "99");
		const watch = rows.find((row) => row.id === "extWatch")!;
		assertEq(watch.label, "Extension watch (env-pinned)");
		assertEq(watch.value, "off");
	});
});

test("settings: non-TUI present lists the rows on stderr", async () => {
	const dir = join(scratchDir(), "list-fallback-agent");
	const lines: string[] = [];
	const real = console.error;
	console.error = (message?: unknown) => {
		lines.push(String(message));
	};
	try {
		await withEnv({ ...ALL_ENV, PI_CODING_AGENT_DIR: dir }, async () => {
			await new DaemonSettingsPresenter().present(undefined, "cli", () => {});
		});
	} finally {
		console.error = real;
	}
	const text = lines.join("\n");
	assertMatches(text, /pi-daemon-settings:/);
	assertMatches(text, /Idle reap \(h\): .*current: 12/);
	assertMatches(text, /Offload wait: .*current: 120/);
});

test("settings: TUI present renders the rows through ui.custom", async () => {
	const dir = join(scratchDir(), "view-agent");
	await withEnv({ ...ALL_ENV, PI_CODING_AGENT_DIR: dir }, async () => {
		let customCalls = 0;
		let rendered: string[] = [];
		const ui = {
			custom: async (render: any) => {
				customCalls++;
				const component = await render(
					{ requestRender() {} }, theme(), {}, () => {});
				rendered = component.render(100);
				return undefined;
			},
			notify: () => {},
		};
		await new DaemonSettingsPresenter().present(ui as never, "tui", () => {});
		assertEq(customCalls, 1, "ui.custom used once");
		const text = rendered.join("\n");
		assertMatches(text, /Idle reap/);
		assertMatches(text, /Extension watch/);
		assertMatches(text, /\bon\b/);
	});
});

test("settings: a failing custom UI falls back to the listing", async () => {
	const dir = join(scratchDir(), "view-fail-agent");
	const lines: string[] = [];
	const real = console.error;
	console.error = (message?: unknown) => {
		lines.push(String(message));
	};
	try {
		await withEnv({ ...ALL_ENV, PI_CODING_AGENT_DIR: dir }, async () => {
			const ui = {
				custom: async () => {
					throw new Error("boom");
				},
				notify: () => {},
			};
			await new DaemonSettingsPresenter().present(ui as never, "tui", () => {});
		});
	} finally {
		console.error = real;
	}
	assertMatches(lines.join("\n"), /pi-daemon-settings:/);
});

test("settings: store writes atomically and preserves keys and mode", async () => {
	const dir = join(scratchDir(), "store-agent");
	mkdirSync(dir, { recursive: true });
	const file = join(dir, "settings.json");
	writeFileSync(file, JSON.stringify({
		theme: "dark",
		packages: ["git:x"],
		piDaemon: { offload: { enabled: false } },
	}, null, 2) + "\n");
	chmodSync(file, 0o600);
	await withEnv({ PI_CODING_AGENT_DIR: dir }, () => {
		const store = new SettingsStore();
		store.set("offload.enabled", true);
		store.set("idleReapHours", 42);
	});
	const written = JSON.parse(readFileSync(file, "utf8"));
	assertEq(written.theme, "dark", "other top-level key preserved");
	assertEq(written.packages.join(","), "git:x", "packages preserved");
	assertEq(written.piDaemon.offload.enabled, true, "nested key written");
	assertEq(written.piDaemon.idleReapHours, 42, "new key written");
	assertEq(statSync(file).mode & 0o777, 0o600, "file mode preserved");
});

test("settings: a corrupt file is reported, never overwritten", async () => {
	const dir = join(scratchDir(), "corrupt-agent");
	mkdirSync(dir, { recursive: true });
	const file = join(dir, "settings.json");
	writeFileSync(file, "{ not json");
	await withEnv({ PI_CODING_AGENT_DIR: dir }, () => {
		const store = new SettingsStore();
		let threw = false;
		try {
			store.set("idleReapHours", 1);
		} catch {
			threw = true;
		}
		assert(threw, "corrupt file must report failure");
	});
	assertEq(readFileSync(file, "utf8"), "{ not json", "file untouched");
});

test("settings: the agent dir is resolved at call time", async () => {
	const first = join(scratchDir(), "late-a");
	const second = join(scratchDir(), "late-b");
	await withEnv({ PI_CODING_AGENT_DIR: first }, () => {
		new SettingsStore().set("idleReapHours", 1);
	});
	await withEnv({ PI_CODING_AGENT_DIR: second }, () => {
		const store = new SettingsStore();
		assertEq(store.path(), join(second, "settings.json"));
		assertEq(store.section().idleReapHours, undefined,
			"no bleed from the first directory");
	});
	assert(existsSync(join(first, "settings.json")), "first write landed");
});

test("settings: invalid input is rejected without persisting", async () => {
	const dir = join(scratchDir(), "invalid-agent");
	await withEnv({ PI_CODING_AGENT_DIR: dir }, () => {
		const presenter = new DaemonSettingsPresenter();
		const outcome = presenter.apply("idleReapHours", "-5");
		assert("error" in outcome, "negative number rejected");
		const bad = presenter.apply("idleReapHours", "nan");
		assert("error" in bad, "non-finite number rejected");
		assert(!existsSync(join(dir, "settings.json")), "nothing persisted");
	});
});

test("settings: the roots list row stores a platform-split array", async () => {
	const dir = join(scratchDir(), "list-row-agent");
	await withEnv({ ...ALL_ENV, PI_CODING_AGENT_DIR: dir }, () => {
		const outcome = new DaemonSettingsPresenter()
			.apply("extWatchRoots", ["/one", "/two"].join(delimiter));
		assert("label" in outcome, "list accepted");
	});
	const written = JSON.parse(readFileSync(join(dir, "settings.json"), "utf8"));
	assertEq(written.piDaemon.extWatchRoots.join(","), "/one,/two");
});

test("settings: the command persists a change and reports the save", async () => {
	const pi = new FakePi();
	factory(pi as never);
	const command = pi.commands.get("daemon-settings") as {
		description: string;
		handler: (args: unknown[], ctx: unknown) => Promise<void>;
	};
	assert(command !== undefined, "daemon-settings registered");
	assertEq(command.description, "Edit pi-daemon settings");
	const dir = join(scratchDir(), "command-agent");
	const notices: Array<{ message: string; kind?: string }> = [];
	await withEnv({ ...ALL_ENV, PI_CODING_AGENT_DIR: dir }, async () => {
		let view: DaemonSettingsView | undefined;
		const ctx = {
			mode: "tui",
			ui: {
				custom: async (render: any) => {
					view = render({ requestRender() {} }, theme(), {}, () => {});
					return undefined;
				},
				notify: (message: string, kind?: string) => {
					notices.push({ message, kind });
				},
			},
		};
		await command.handler([], ctx);
		view!.selectItem("extWatch");
		view!.handleInput("\r");
	});
	assertEq(notices.length, 1, "exactly one notice");
	assertEq(notices[0].kind, "info");
	assertEq(notices[0].message,
		"Saved Extension watch. pi reloads extensions when settings.json " +
		"changes; daemon-owned values apply on the next daemon restart.");
	const written = JSON.parse(readFileSync(join(dir, "settings.json"), "utf8"));
	assertEq(written.piDaemon.extWatch, false, "change persisted");
});