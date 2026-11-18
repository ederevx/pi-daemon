/**
 * pi-daemon — composition test for the four prerequisite gates.
 *
 * Four owners gate their own prerequisite over the same `tool_call` event:
 * the agent-comms acknowledgement (pi-daemon), the daemon onboarding call
 * (pi-daemon), the root-memory convention acknowledgement (agent-mem-struct)
 * and the consultation acknowledgement (pi-consult). Each one admits the
 * tools every other owner declares as its prerequisite's escape, so the
 * declared escapes have to hold whatever order pi registers the handlers in.
 * The reported deadlock was exactly this: whichever gate ran first blocked
 * the other extension's own way in.
 *
 * The suite registers the four real owners — no reimplemented gates, no
 * stubbed decisions — in every one of the 24 handler orders and dispatches
 * through the chain, so a fix that only works because one extension happens
 * to register first cannot pass. The memory owner is the real bridge, so the
 * real Python hook runs and records its receipts.
 *
 * The daemon sources come from this test's own checkout.
 * AGENT_MEM_STRUCT_ROOT and PI_CONSULT_ROOT configure the other checkouts.
 * With neither configured the composition cases report themselves unsupported;
 * partial configuration or an invalid root fails.
 *
 * The last case is the registration smoke: the production entry files are
 * loaded through the host's own resource loader, without a session, and their
 * gate escape declarations are read back as metadata. It proves that the
 * shipped entries still register their tools with the declarations the
 * composition cases consume - nothing about host dispatch, which the cases
 * above cover for the consumed side and the installed-session proof covers for
 * the produced side.
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { assert, assertEq, scratchDir, skip, test, withEnv } from "./harness.ts";
import {
	CommsAcknowledgements,
	COMMS_ACK_STATE,
} from "../../pi/extensions/comms/acknowledgements.ts";
import { CommsMessageLog } from "../../pi/extensions/comms/message-log.ts";
import { CommsTools } from "../../pi/extensions/comms/tools.ts";
import { PreDaemonTool } from "../../pi/extensions/pre-daemon.ts";
import { ToolGateEscapes } from "../../pi/extensions/guards/tool-gate-escapes.ts";

/** The four owners, in the names the assertions and failure labels use. */
type OwnerName = "comms" | "daemon" | "memory" | "consult";

interface Roots {
	daemon: string;
	memory: string;
	consult: string;
}

/** The configured checkouts, or null when none are configured. A partial
 *  configuration is a mistake, not a reason to skip. */
function configuredRoots(): Roots | null {
	const env = process.env;
	// The daemon root is this file's own checkout: the modules under test are
	// the ones beside this test, never a checkout an environment variable names.
	const daemon = resolve(fileURLToPath(new URL("../..", import.meta.url)));
	const given = [env.AGENT_MEM_STRUCT_ROOT, env.PI_CONSULT_ROOT];
	if (given.every((value) => !value)) return null;
	if (given.some((value) => !value)) {
		throw new Error(
			"gate composition: set AGENT_MEM_STRUCT_ROOT and PI_CONSULT_ROOT " +
				"together, or neither of them",
		);
	}
	const roots: Roots = {
		daemon,
		memory: resolve(given[0]!),
		consult: resolve(given[1]!),
	};
	const entries: Array<[string, string]> = [
		["pi-daemon", join(roots.daemon, "pi", "extensions", "pre-daemon.ts")],
		["agent-mem-struct", join(roots.memory, "hooks", "pi", "root-memory-extension.ts")],
		["pi-consult", join(roots.consult, "src", "implementation-gate.ts")],
	];
	for (const [name, path] of entries) {
		if (!exists(path)) {
			throw new Error(`gate composition: ${name} root has no ${path}`);
		}
	}
	return roots;
}

function exists(path: string): boolean {
	try {
		statSync(path);
		return true;
	} catch {
		return false;
	}
}

/** One owner's live registration, kept so a case can drive its own way in. */
interface Owners {
	acks: CommsAcknowledgements;
	daemon: PreDaemonTool;
	contract: any;
}

const PERMUTATIONS: OwnerName[][] = (() => {
	const items: OwnerName[] = ["comms", "daemon", "memory", "consult"];
	const result: OwnerName[][] = [];
	const walk = (prefix: OwnerName[], rest: OwnerName[]): void => {
		if (rest.length === 0) {
			result.push(prefix);
			return;
		}
		for (let index = 0; index < rest.length; index += 1) {
			walk(
				[...prefix, rest[index]!],
				rest.filter((_, at) => at !== index),
			);
		}
	};
	walk([], items);
	return result;
})();

/** The host pi hands an extension: registration points, the live tool
 *  registry the gate escapes are read from, and one session branch. */
class FakeHost {
	readonly handlers: Array<(event: any, ctx: any) => unknown> = [];
	readonly tools: any[] = [];
	readonly entries: Array<Record<string, unknown>> = [];
	constructor(readonly cwd: string) {}

	on(name: string, handler: (event: any, ctx: any) => unknown): void {
		if (name === "tool_call") this.handlers.push(handler);
	}

	registerTool(def: any): void {
		this.tools.push(def);
	}

	registerEntryRenderer(): void {}
	registerCommand(): void {}
	sendMessage(): void {}
	getSettings(): Record<string, unknown> {
		return {};
	}

	appendEntry(customType: string, data: unknown): void {
		this.entries.push({ type: "custom", customType, data });
	}

	getAllTools(): Array<{ name: string; annotations?: Record<string, unknown> }> {
		return this.tools.map((tool) => ({
			name: String(tool.name),
			...(tool.annotations ? { annotations: tool.annotations } : {}),
		}));
	}

	tool(name: string): any {
		const found = this.tools.find((tool) => tool.name === name);
		if (!found) throw new Error(`gate composition: no ${name} was registered`);
		return found;
	}

	context(): any {
		return {
			cwd: this.cwd,
			sessionManager: {
				getEntries: () => this.entries,
				getBranch: () => this.entries,
				getSessionId: () => "session-composition",
			},
		};
	}

	/** Dispatch one tool call through every registered handler, in order. */
	async dispatch(
		toolName: string,
		input: Record<string, unknown> = {},
	): Promise<{ blocked: boolean; reason: string }> {
		const event = { toolName, input };
		const ctx = this.context();
		for (const handler of this.handlers) {
			const result = (await handler(event, ctx)) as
				| { block?: boolean; reason?: unknown }
				| undefined;
			if (result?.block) {
				return { blocked: true, reason: String(result.reason ?? "") };
			}
		}
		return { blocked: false, reason: "" };
	}
}

/** The comms owner's CLI, stubbed per its own interface: every call reports
 *  that the service accepted it, which is what an acknowledgement needs. */
function stubCli(armed: () => string[] = () => []): any {
	return {
		run: async (verb: string) => {
			if (verb === "ack") {
				return { exitCode: 0, answer: { acknowledged: armed(), unknown: [] } };
			}
			return { exitCode: 0, answer: { status: "accepted" } };
		},
	};
}

/** The tools the daemon's onboarding gate owns; every other gate covers
 *  ordinary work as well. */
const DAEMON_OWNED = [
	"bash",
	"daemon_tasks",
	"daemon_gc_reap",
	"transcript_read",
	"comms_send",
	"comms_wait",
	"comms_replay",
	"comms_ack",
	"comms_spawn",
	"peers",
];

function denies(owner: OwnerName, tool: string): boolean {
	if (owner === "daemon") return DAEMON_OWNED.includes(tool);
	return true;
}

/** The gate the registration order puts first among those still armed. */
function firstDenier(
	order: OwnerName[],
	armed: Set<OwnerName>,
	tool: string,
): OwnerName {
	const owner = order.find((name) => armed.has(name) && denies(name, tool));
	if (!owner) throw new Error(`no armed gate denies ${tool} — the case is broken`);
	return owner;
}

let memoryModule: any;
let consultModules: any;

async function loadMemory(roots: Roots): Promise<any> {
	if (!memoryModule) {
		memoryModule = await import(
			pathToFileURL(join(roots.memory, "hooks", "pi", "root-memory-extension.ts")).href
		);
	}
	return memoryModule;
}

async function loadConsult(roots: Roots): Promise<any> {
	if (!consultModules) {
		const base = pathToFileURL(join(roots.consult, "src")).href;
		consultModules = {
			ImplementationGate: (await import(`${base}/implementation-gate.ts`))
				.ImplementationGate,
			ReceiptLedger: (await import(`${base}/receipt-ledger.ts`)).ReceiptLedger,
			ToolClassifier: (await import(`${base}/tool-classifier.ts`)).ToolClassifier,
			ConsultContract: (await import(`${base}/consult-contract.ts`)).ConsultContract,
		};
	}
	return consultModules;
}

/** A root memory with a shared worktree, in the shape the installer and the
 *  Python hook both expect: the root control, its conventions, the local
 *  scope, and the git-backed shared structure. */
let fixtureCounter = 0;

/** A root memory with a shared worktree, in the shape the installer and the
 *  Python hook both expect: the root control, its conventions, the local
 *  scope, and the git-backed shared structure. */
function memoryFixture(
	label: string,
	roots: Roots,
): { home: string; shared: string; configHome: string } {
	fixtureCounter += 1;
	const home = join(scratchDir(), `fixture-${fixtureCounter}-${label.replace(/[^a-z]+/gi, "-")}`);
	mkdirSync(home, { recursive: true });
	const shared = join(home, "shared memory");
	// The hook only runs for the agent home it was installed for, so the
	// fixture's config home is this run's own pi home, never the host's.
	const configHome = process.env.PI_CODING_AGENT_DIR
		? resolve(process.env.PI_CODING_AGENT_DIR)
		: "";
	assert(
		configHome !== "",
		`${label}: isolateHost() must set PI_CODING_AGENT_DIR; the fixture never ` +
			"falls back to the host's own agent home",
	);
	mkdirSync(join(home, "memory", "local"), { recursive: true });
	mkdirSync(configHome, { recursive: true });
	// The root documents are the canonical ones, exactly as the installer lays
	// them down: the hook refuses a root that differs from its checkout.
	const structure = readFileSync(join(roots.memory, "STRUCTURE.md"), "utf-8");
	const version = structure.split("\n")[0] ?? "Structure-Version: test-v1";
	writeFileSync(
		join(home, "memory", "MEMORY.md"),
		`${version}\nStructure: ../STRUCTURE.md\nShared: ${shared}\n\n# Root\n`,
		"utf-8",
	);
	writeFileSync(
		join(home, "RULES.md"),
		readFileSync(join(roots.memory, "RULES.md"), "utf-8"),
		"utf-8",
	);
	writeFileSync(join(home, "STRUCTURE.md"), structure, "utf-8");
	writeFileSync(
		join(home, "memory", "local", "MEMORY.md"),
		"# Local\n\n**Scope:** this agent\n\n## Mandatory conventions\n\n(none)\n",
		"utf-8",
	);
	mkdirSync(shared, { recursive: true });
	const git = (...args: string[]): void => {
		try {
			execFileSync("git", args, { cwd: shared, encoding: "utf8" });
		} catch (error: any) {
			throw new Error(
				`gate composition (${label}): git ${args[0]} failed in ${shared} — ` +
					String(error?.stderr || error?.stdout || error?.message || error),
			);
		}
	};
	git("init", "-b", "main");
	git("config", "user.email", "composition@test");
	git("config", "user.name", "composition");
	writeFileSync(
		join(shared, "MEMORY.md"),
		"# Shared\n\n**Scope:** *\n\n## Mandatory conventions\n\n- Verify first.\n",
		"utf-8",
	);
	git("add", "-A");
	git("commit", "-m", "shared structure");
	return { home, shared, configHome };
}

/** Register all four real owners, in the order the case names. */
async function registerOwners(
	order: OwnerName[],
	host: FakeHost,
	roots: Roots,
	fixture: { home: string; shared: string; configHome: string },
	armed: () => string[] = () => [],
): Promise<Owners> {
	const commsLog = new CommsMessageLog(host as any);
	const cli = stubCli(armed);
	const acks = new CommsAcknowledgements(cli, commsLog, host as any);
	const daemon = new PreDaemonTool({ enabled: () => true } as any);
	const memory = await loadMemory(roots);
	const consult = await loadConsult(roots);
	const contract = new consult.ConsultContract();
	const gate = new consult.ImplementationGate(
		new consult.ReceiptLedger(consult.ConsultContract.VERSION),
		new consult.ToolClassifier({ allow: [], block: [], trustReadOnlyHint: false }),
		contract,
	);
	for (const owner of order) {
		switch (owner) {
			case "comms": {
				// The acknowledgement gate and the tools whose registrations carry
				// its declaration: the gate admits what the live registry declares.
				acks.register(host as any);
				new CommsTools(cli, commsLog, { acks } as any).register(host as any);
				break;
			}
			case "daemon": {
				daemon.register(host as any);
				break;
			}
			case "memory": {
				memory.createRootMemoryBridge(host, {
					python: "python3",
					hook: join(roots.memory, "hooks", "root-memory-context.py"),
					memoryHome: fixture.home,
					configHome: fixture.configHome,
					canonicalRoot: roots.memory,
				});
				break;
			}
			case "consult": {
				gate.register(host as any);
				const PreConsultTool = (await import(
					`${pathToFileURL(join(roots.consult, "src")).href}/pre-consult-tool.ts`
				)).PreConsultTool;
				new PreConsultTool(contract, {
					probeAll: async () => [],
					targets: () => [],
				}).register(host as any);
				break;
			}
		}
	}
	return { acks, daemon, contract };
}

/** Arm the comms obligation: one delivered envelope, durably recorded. */
function armComms(owners: Owners, host: FakeHost, id: string): void {
	const delivered = {
		messageId: id,
		kind: "message",
		sequence: 1,
		from: { agentId: "sender" },
		to: { agentId: "receiver" },
		payload: "hello",
	} as any;
	assert(
		owners.acks.markPending(delivered, `reply-${id}`, true),
		"the comms obligation must be recorded durably",
	);
	owners.acks.attach(host.context());
	assert(owners.acks.owing(), "the comms gate must owe an acknowledgement");
}

/** Which owner refused the call, read from its own denial text. */
function gateOf(reason: string, contract: any, tool: string): OwnerName | null {
	if (/agent-comms acknowledgement/i.test(reason)) return "comms";
	if (/pre_daemon/i.test(reason)) return "daemon";
	if (reason === contract.gateDenial(tool) || /pi-consult contract/i.test(reason)) {
		return "consult";
	}
	if (/pre_memory|agent-mem-struct|convention/i.test(reason)) return "memory";
	return null;
}

async function expectBlocked(
	host: FakeHost,
	owners: Owners,
	tool: string,
	expected: OwnerName,
	input: Record<string, unknown>,
	label: string,
): Promise<void> {
	const result = await host.dispatch(tool, input);
	assert(
		result.blocked,
		`${label}: ${tool} must stay blocked while ${expected} owes`,
	);
	assertEq(
		gateOf(result.reason, owners.contract, tool),
		expected,
		`${label}: ${tool} must be refused by ${expected} — got ${JSON.stringify(result.reason)}`,
	);
}

test("every handler order admits each owner's declared escape", async () => {
	const roots = configuredRoots();
	if (!roots) {
		skip("no AGENT_MEM_STRUCT_ROOT / PI_CONSULT_ROOT configured");
	}
	for (const order of PERMUTATIONS) {
		const label = `order ${order.join(">")}`;
		const fixture = memoryFixture(label, roots);
		const host = new FakeHost(fixture.shared);
		const owners = await registerOwners(order, host, roots, fixture);
		armComms(owners, host, `message-${order.join("-")}`);
		// Every prerequisite is armed, whatever the order: this is the state
		// the reported session deadlocked in. The first gate the order puts
		// in front is the one that refuses ordinary work.
		const armed = new Set<OwnerName>(["comms", "daemon", "memory", "consult"]);
		await expectBlocked(
			host,
			owners,
			"edit",
			firstDenier(order, armed, "edit"),
			{ path: join(fixture.home, "memory", "local", "MEMORY.md") },
			label,
		);
		for (const escapeTool of ["pre_memory", "comms_ack"]) {
			const result = await host.dispatch(escapeTool);
			assert(
				!result.blocked,
				`${label}: ${escapeTool} is a declared escape and must stay reachable — ${result.reason}`,
			);
		}
	}
});

test("a session walks acknowledgement first to ordinary work", async () => {
	await walkToOrdinaryWork(
		["comms", "daemon", "memory", "consult"],
		["comms", "daemon", "memory", "consult"],
	);
});

test("a session walks acquisition first to ordinary work", async () => {
	await walkToOrdinaryWork(
		["consult", "memory", "daemon", "comms"],
		["memory", "comms", "consult", "daemon"],
	);
});

/** One complete session: every gate armed, each owner's own way in executed
 *  for real, and ordinary work only once all four have completed. The walk
 *  order is independent of the handler order, so the two cases settle the
 *  acknowledgement first in one and the convention acquisition first in the
 *  other. */
async function walkToOrdinaryWork(
	order: OwnerName[],
	settle: OwnerName[],
): Promise<void> {
	const roots = configuredRoots();
	if (!roots) {
		skip("no AGENT_MEM_STRUCT_ROOT / PI_CONSULT_ROOT configured");
	}
	const label = `walk ${order.join(">")} settling ${settle.join(">")}`;
	const fixture = memoryFixture(label, roots);
	const host = new FakeHost(fixture.shared);
	const id = `message-walk-${order.join("-")}`;
	const armedIds: string[] = [];
	const owners = await registerOwners(order, host, roots, fixture, () => armedIds);
	armedIds.push(id);
	armComms(owners, host, id);
	const armed = new Set<OwnerName>(["comms", "daemon", "memory", "consult"]);
	const listerCommand =
		`python3 "${join(roots.memory, "hooks", "rm_tree.py")}" --list "${fixture.shared}"`;

	// Nothing has been settled: ordinary work is refused by the gate the
	// registration order puts first.
	await expectBlocked(
		host,
		owners,
		"edit",
		firstDenier(order, armed, "edit"),
		{ path: join(fixture.home, "memory", "local", "MEMORY.md") },
		label,
	);

	for (const owner of settle) {
		assert(armed.has(owner), `${label}: ${owner} is settled exactly once`);
		switch (owner) {
			case "comms": {
				const gate = await host.dispatch("comms_ack");
				assert(!gate.blocked, `${label}: comms_ack must reach execution — ${gate.reason}`);
				const ack = (await host
					.tool("comms_ack")
					.execute("composition-ack", { messageIds: [id] }, undefined, undefined, host.context())) as any;
				assert(
					!ack?.isError,
					`${label}: comms_ack must acknowledge the delivered message — ` +
						JSON.stringify(ack?.content ?? ack),
				);
				assert(
					!owners.acks.owing(),
					`${label}: the acknowledgement must clear the obligation`,
				);
				assertEq(
					host.entries.filter((entry) => entry.customType === COMMS_ACK_STATE).length > 0,
					true,
					`${label}: the acknowledgement must be recorded on the branch`,
				);
				break;
			}
			case "memory": {
				const gate = await host.dispatch("pre_memory");
				assert(!gate.blocked, `${label}: pre_memory must reach execution — ${gate.reason}`);
				const catalog = (await host
					.tool("pre_memory")
					.execute("composition-memory", {}, undefined, undefined, host.context())) as any;
				assert(
					!catalog?.isError,
					`${label}: pre_memory must return the convention catalog — ` +
						JSON.stringify(catalog?.content ?? []),
				);
				for (const source of [
					join(fixture.home, "memory", "MEMORY.md"),
					join(fixture.home, "RULES.md"),
					join(fixture.home, "STRUCTURE.md"),
					join(fixture.home, "memory", "local", "MEMORY.md"),
					join(fixture.shared, "MEMORY.md"),
				]) {
					const read = await host.dispatch("read", { path: source });
					assert(!read.blocked, `${label}: read ${source} must stay reachable — ${read.reason}`);
					// The dispatch is the PreToolUse record; the read itself must
					// still fetch the whole document the gate asks for.
					assert(
						readFileSync(source, "utf-8").length > 0,
						`${label}: ${source} must be readable in full`,
					);
				}
				const list = await host.dispatch("rm_tree_exec", { command: listerCommand });
				assert(!list.blocked, `${label}: rm_tree_exec must stay reachable — ${list.reason}`);
				const listed = (await host
					.tool("rm_tree_exec")
					.execute("composition-tree", { command: listerCommand }, undefined, undefined, host.context())) as any;
				assert(
					!listed?.isError,
					`${label}: rm_tree_exec must list the shared structure — ` +
						JSON.stringify(listed?.content ?? listed),
				);
				break;
			}
			case "daemon": {
				const gate = await host.dispatch("pre_daemon");
				assert(!gate.blocked, `${label}: pre_daemon must reach execution — ${gate.reason}`);
				const catalog = (await host
					.tool("pre_daemon")
					.execute("composition-daemon", {}, undefined, undefined, host.context())) as any;
				assert(
					JSON.stringify(catalog ?? {}).includes("pre_daemon"),
					`${label}: pre_daemon must return its catalog`,
				);
				break;
			}
			case "consult": {
				const gate = await host.dispatch("pre_consult");
				assert(!gate.blocked, `${label}: pre_consult must reach execution — ${gate.reason}`);
				const result = (await host
					.tool("pre_consult")
					.execute("composition-consult", { acknowledge: true }, undefined, undefined, host.context())) as any;
				assert(
					!result?.isError,
					`${label}: pre_consult must acknowledge — ` + JSON.stringify(result?.content ?? result),
				);
				// The receipt is the tool's own result: the gate reads the
				// transcript, so the session records what the tool returned.
				host.entries.push({
					type: "message",
					message: {
						...result,
						role: "toolResult",
						toolCallId: "composition-consult",
						toolName: "pre_consult",
						isError: false,
					},
				});
				break;
			}
		}
		armed.delete(owner);

		// Its own admission settles this gate and no other: the remaining
		// gates still refuse ordinary work, and the memory gate still refuses
		// a mutation until its own receipts exist.
		if (order.some((name) => armed.has(name) && denies(name, "edit"))) {
			await expectBlocked(
				host,
				owners,
				"edit",
				firstDenier(order, armed, "edit"),
				{ path: join(fixture.home, "memory", "local", "MEMORY.md") },
				`${label} after ${owner}`,
			);
		}
		const mutation = await host.dispatch("edit", {
			path: join(fixture.home, "memory", "local", "MEMORY.md"),
		});
		if (armed.has("memory")) {
			assert(
				mutation.blocked,
				`${label}: a memory edit must stay blocked until the conventions are acknowledged`,
			);
			assertEq(gateOf(mutation.reason, owners.contract, "edit"), "memory", label);
		} else {
			// Another gate may still refuse ordinary work; the memory gate
			// itself must no longer be the one refusing it.
			assert(
				gateOf(mutation.reason, owners.contract, "edit") !== "memory",
				`${label}: the memory gate must be settled by its own receipts — ${mutation.reason}`,
			);
		}
	}

	assertEq(armed.size, 0, `${label}: every prerequisite must be settled`);
	const work = await host.dispatch("edit", { path: join(fixture.home, "RULES.md") });
	assert(
		!work.blocked,
		`${label}: ordinary work must run once every prerequisite completed — ${work.reason}`,
	);
	const memoryMutation = await host.dispatch("edit", {
		path: join(fixture.home, "memory", "local", "MEMORY.md"),
	});
	assert(
		!memoryMutation.blocked,
		`${label}: a memory edit must run once the conventions are acknowledged — ${memoryMutation.reason}`,
	);
}

test("production entries preserve gate escape declarations", async () => {
	const roots = configuredRoots();
	if (!roots) {
		skip("no AGENT_MEM_STRUCT_ROOT / PI_CONSULT_ROOT configured");
	}
	const fixture = memoryFixture("registration smoke", roots);
	// The entries exactly as a package install loads them: the daemon
	// extension, the onboarding tool, the memory package entry and the
	// pi-consult registration, with the tools and declarations each must carry.
	const entries = [
		join(roots.daemon, "pi", "extensions", "daemon.ts"),
		join(roots.daemon, "pi", "extensions", "pre-daemon.ts"),
		join(roots.memory, "extensions", "agent-mem-struct.ts"),
		join(roots.consult, "src", "index.ts"),
	];
	const expected: Array<{ tool: string; admits: string[]; readOnly: boolean; entry: string }> = [
		{
			tool: "pre_daemon",
			admits: ["pre_daemon"],
			readOnly: true,
			entry: join(roots.daemon, "pi", "extensions", "pre-daemon.ts"),
		},
		{
			tool: "comms_ack",
			admits: ["comms_ack"],
			readOnly: false,
			entry: join(roots.daemon, "pi", "extensions", "daemon.ts"),
		},
		{
			tool: "pre_memory",
			admits: ["pre_memory", "read", "rm_tree_exec"],
			readOnly: false,
			entry: join(roots.memory, "extensions", "agent-mem-struct.ts"),
		},
		{
			tool: "pre_consult",
			admits: ["pre_consult"],
			readOnly: true,
			entry: join(roots.consult, "src", "index.ts"),
		},
	];
	// Every path the entries resolve, and every subprocess the memory entry
	// runs while registering, stays inside this fixture.
	await withEnv(
		{
			PI_CODING_AGENT_DIR: fixture.configHome,
			AMS_CONFIG_HOME: fixture.configHome,
			AMS_MEMORY_HOME: fixture.home,
			AMS_CANONICAL_ROOT: roots.memory,
			AMS_HOOK: join(roots.memory, "hooks", "root-memory-context.py"),
			AMS_PYTHON: "python3",
			PI_CONSULT_CONFIG: undefined,
		},
		async () => {
			const { DefaultResourceLoader } = await import("@earendil-works/pi-coding-agent");
			const loader = new DefaultResourceLoader({
				cwd: fixture.shared,
				agentDir: fixture.configHome,
				additionalExtensionPaths: entries,
				noExtensions: true,
				noSkills: true,
				noPromptTemplates: true,
				noThemes: true,
				noContextFiles: true,
			});
			// Load only: no lifecycle event, no tool call, no agent session.
			await loader.reload();
			const loaded = loader.getExtensions();
			const failures = loaded.errors.map((one) => `${one.path}: ${one.error}`);
			assertEq(
				failures.length,
				0,
				`registration smoke: every entry must load — ${failures.join(" | ")}`,
			);
			assertEq(
				(loaded.warnings ?? []).length,
				0,
				"registration smoke: no entry may warn — " +
					(loaded.warnings ?? []).map((one) => `${one.path}: ${one.warning}`).join(" | "),
			);
			const loadedPaths = loaded.extensions.map((extension) => realpathSync(extension.resolvedPath));
			assertEq(loadedPaths.length, entries.length, "registration smoke: four entries load");
			assertEq(
				new Set(loadedPaths).size,
				entries.length,
				"registration smoke: each entry loads exactly once",
			);
			for (const entry of entries) {
				assert(
					loadedPaths.includes(realpathSync(entry)),
					`registration smoke: ${entry} must load`,
				);
			}

			for (const { tool, admits, readOnly, entry } of expected) {
				const owners = loaded.extensions.filter((extension) => extension.tools.has(tool));
				assertEq(owners.length, 1, `registration smoke: ${tool} is registered exactly once`);
				const owner = owners[0]!;
				assertEq(
					realpathSync(owner.resolvedPath),
					realpathSync(entry),
					`registration smoke: ${tool} comes from ${entry}`,
				);
				const definition = owner.tools.get(tool)!.definition as any;
				const declaration = definition.annotations?.[ToolGateEscapes.KEY];
				assert(
					declaration,
					`registration smoke: ${tool} must declare ${ToolGateEscapes.KEY}`,
				);
				assertEq(
					declaration.version,
					ToolGateEscapes.VERSION,
					`registration smoke: ${tool} declares the current version`,
				);
				assertEq(
					JSON.stringify(declaration.admits),
					JSON.stringify(admits),
					`registration smoke: ${tool} declares its own escape`,
				);
				assertEq(
					definition.annotations?.readOnlyHint,
					readOnly,
					`registration smoke: ${tool} keeps its read-only hint`,
				);
				assertEq(
					(owner.handlers.get("tool_call") ?? []).length > 0,
					true,
					`registration smoke: ${entry} still guards its tool call`,
				);
			}

			const declared = loaded.extensions.flatMap((extension) =>
				[...extension.tools.entries()]
					.filter(([, registered]) => {
						const annotations = (registered.definition as any).annotations;
						return annotations && Object.hasOwn(annotations, ToolGateEscapes.KEY);
					})
					.map(([name]) => name),
			);
			assertEq(
				declared.sort().join(","),
				expected.map((one) => one.tool).sort().join(","),
				"registration smoke: no other loaded tool owns the declaration",
			);
		},
	);
});
