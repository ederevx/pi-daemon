/**
 * pi-daemon — remote-work guard tests.
 * ssh from a hosted session is transport for files only: the guard must
 * refuse a remote shell or remote work in bash, powershell, and
 * daemon_tasks submit, while transfers, ssh queries, and tunnels stay
 * allowed. It must also be disabled by PI_REMOTE_GUARD=off or
 * `piDaemon.remoteGuard.enabled=false`.
 */

import { test, assert, assertEq, withEnv } from "./harness.ts";
import {
	RemoteWorkClassifier,
} from "../../pi/extensions/guards/remote-work-classifier.ts";
import {
	RemoteWorkGuard,
} from "../../pi/extensions/guards/remote-work-guard.ts";
import { RemotePolicy } from "../../pi/extensions/settings/remote-policy.ts";

type Handler = (event: any, ctx?: any) => any;

class MockPi {
	readonly handlers = new Map<string, Handler[]>();

	on(name: string, handler: Handler): void {
		const list = this.handlers.get(name) ?? [];
		list.push(handler);
		this.handlers.set(name, list);
	}

	gate(): Handler {
		return this.handlers.get("tool_call")![0];
	}
}

function guard(
	enabled: boolean | (() => boolean) = true,
): { pi: MockPi; gate: Handler } {
	const pi = new MockPi();
	const policy = {
		enabled: typeof enabled === "function" ? enabled : () => enabled,
	};
	new RemoteWorkGuard(policy).register(pi as never);
	return { pi, gate: pi.gate() };
}

function bash(command: string, extra: Record<string, unknown> = {}): any {
	return { type: "tool_call", toolName: "bash", input: { command }, ...extra };
}

test("remote guard: refuses a remote shell and remote work", () => {
	const { gate } = guard();
	for (const command of [
		"ssh host",
		"ssh -T host",
		"ssh host make",
		"ssh -p 2222 -i k host 'git pull'",
		"ssh host pi -p x",
		"cd x && ssh host ls",
		"bash -c \"ssh host ls\"",
		"sudo env A=1 timeout 5 ssh host ls",
		"echo $(ssh host hostname)",
		"ssh -o RemoteCommand=ls host",
	]) {
		const result = gate(bash(command));
		assertEq(result?.block, true, `must block: ${command}`);
		assert(typeof result?.reason === "string", "reason must be text");
	}
});

test("remote guard: allows transfers, ssh queries, and tunnels", () => {
	const { gate } = guard();
	for (const command of [
		"scp a host:b",
		"scp -P 2222 -i k host:a .",
		"sftp -b batch host",
		"rsync -az -e 'ssh -p 2222' d host:d",
		"ssh host 'cat > f' < f",
		"tar c d | ssh host 'mkdir -p d && tar x -C d'",
		"ssh host sha256sum f",
		"ssh -V",
		"ssh -G host",
		"ssh -N -L 1:h:2 host",
		"pi -p x",
		"grep -rn ssh README.md",
	]) {
		assertEq(gate(bash(command))?.block, undefined,
			`must stay allowed: ${command}`);
	}
});

test("remote guard: covers powershell and daemon_tasks submit", () => {
	const { gate } = guard();
	assertEq(gate({
		type: "tool_call", toolName: "powershell",
		input: { command: "ssh host ls" },
	})?.block, true, "powershell is a shell tool");
	assertEq(gate({
		type: "tool_call", toolName: "daemon_tasks",
		input: { action: "submit", command: "ssh host make" },
	})?.block, true, "submit runs the same command later");
	assertEq(gate({
		type: "tool_call", toolName: "daemon_tasks",
		input: { action: "status", id: "t-1" },
	}), undefined, "ticket management is untouched");
	assertEq(gate({
		type: "tool_call", toolName: "read", input: { path: "x" },
	}), undefined, "foreign tools are untouched");
});

test("remote guard: a nested call is visible through parentToolCallId", () => {
	const { gate } = guard();
	const result = gate(bash("ssh host ls", { parentToolCallId: "call-1/0" }));
	assertEq(result?.block, true, "nested bash is blocked too");
});

test("remote guard: disabled by the setting or the env switch", async () => {
	assertEq(guard(false).gate(bash("ssh host ls")), undefined,
		"setting disables");
	await withEnv({ PI_REMOTE_GUARD: "off" }, () => {
		const policy = new RemotePolicy({ section: () => ({}) });
		assertEq(policy.enabled(), false, "PI_REMOTE_GUARD=off");
		const pi = new MockPi();
		new RemoteWorkGuard(policy).register(pi as never);
		assertEq(pi.gate()(bash("ssh host ls")), undefined, "off means off");
	});
	await withEnv({ PI_REMOTE_GUARD: "on" }, () => {
		const policy = new RemotePolicy({
			section: () => ({ remoteGuard: { enabled: false } }),
		});
		assertEq(policy.enabled(), true, "env wins over the setting");
	});
	assertEq(
		new RemotePolicy({ section: () => ({ remoteGuard: { enabled: false } }) })
			.enabled(),
		false, "the setting disables",
	);
	assertEq(new RemotePolicy({ section: () => ({}) }).enabled(), true,
		"on by default");
});

test("remote guard: the refusal names the sanctioned paths", () => {
	const { gate } = guard();
	const reason = String(gate(bash("ssh host make"))?.reason ?? "");
	assert(/scp|sftp|rsync/.test(reason), "names the file-transfer clients");
	assert(/pi-teams|team_spawn/.test(reason), "names the coordination path");
	assert(/PI_REMOTE_GUARD=off/.test(reason), "names the switch");
});

test("remote classifier: verdicts for the command shapes", () => {
	const classifier = new RemoteWorkClassifier();
	assertEq(classifier.classify("ssh host")[0], "ssh-shell");
	assertEq(classifier.classify("ssh host make")[0], "ssh-exec");
	assertEq(classifier.classify("ssh -N -L 1:h:2 host")[0], "ssh-tunnel");
	assertEq(classifier.classify("ssh -V")[0], "ssh-info");
	assertEq(classifier.classify("scp a host:b")[0], "transfer");
	assertEq(classifier.classify("pi -p x")[0], "headless-pi");
	assertEq(classifier.classify("pi update").length, 0);
});
