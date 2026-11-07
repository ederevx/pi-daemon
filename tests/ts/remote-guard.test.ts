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
		"ssh host -T",
		"ssh host -o RemoteCommand=make",
		"(cd x && ssh host make)",
		"cat <<<\"$(ssh host id)\"",
		"cat <<<\"\\\\$(ssh host id)\"",
		"cat <<<\"'$(ssh host id)'\"",
		"cat <<<'a\\'\"$(ssh host id)\"",
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
		"ssh host -- cat f",
		"pi -p x",
		"grep -rn ssh README.md",
		"cat <<<'$(ssh host id)'",
		"cat <<<\"\\$(ssh host id)\"",
		"bash <<<safe",
	]) {
		assertEq(gate(bash(command))?.block, undefined,
			`must stay allowed: ${command}`);
	}
});

test("remote guard: a heredoc body is data unless a shell runs it", () => {
	const { gate } = guard();
	for (const command of [
		"python - <<'PY'\npi = e.get('pi') if e else None\nPY",
		"cat > notes.txt <<'EOF'\nssh host rm -rf /tmp/x\nEOF",
		"cat <<'EOF'\n$(ssh host id)\nEOF",
		"bash script.sh <<'EOF'\nssh host id\nEOF",
		"bash <<'EOF' 1>&2 </dev/null\nssh host id\nEOF",
		"bash <<'EOF' >3</dev/null\nssh host id\nEOF",
		"bash <<'EOF' > 3</dev/null\nssh host id\nEOF",
		"bash <<'A' 3<&0 <<'B' <&0\nssh host id\nA\nplain\nB",
		"cat <<'A' | bash <<'B'\nssh host id\nA\nprintf safe\nB",
		"cat <<'A' | bash < /dev/null\nssh host id\nA",
		"cat <<'A' | bash 3</dev/null <&3\nssh host id\nA",
		"cat <<'A' | bash 3<&0 <<'B'\nssh host id\nA\nprintf safe\nB",
		"bash 3<<'EOF' <& 3</dev/null\nssh host id\nEOF",
		"bash<<<safe",
		"bash <<'EOF' <<<safe\nssh host id\nEOF",
		"bash <<<EOF",
		"bash <<E\\\nOF\nsafe\nEOF",
		"bash <<\"E\\\nOF\"\nsafe\nEOF",
		"bash <<\"EOF\"\nsafe\nEOF",
		"bash <<$'EOF'\nsafe\nEOF",
		"bash 3<<'EOF' <&$'\\0'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\UFFFFFFFF'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\u0034'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'4'\nssh host id\nEOF",
		"bash <<'EOF' >>3<file\nssh host id\nEOF",
		"bash <<'EOF' >out3</dev/null\nssh host id\nEOF",
		"bash <<'EOF' > >(cat; :) </dev/null\nssh host id\nEOF",
		"bash <<'EOF' >$(printf /dev/null) </dev/null\nssh host id\nEOF",
		"bash <<'EOF' >$(printf /dev/null; :) </dev/null\nssh host id\nEOF",
		"bash <<'EOF' x#y </dev/null\nssh host id\nEOF",
		"bash +s script.sh <<'EOF'\nssh host id\nEOF",
		"bash -s +s script.sh <<'EOF'\nssh host id\nEOF",
		"bash -eo posix script.sh <<'EOF'\nssh host id\nEOF",
		"bash '-c' 'x' <<'EOF'\nssh host id\nEOF",
		"bash <<'EOF' $(printf '')script.sh\nssh host id\nEOF",
		"bash <<'EOF' \"\"$(printf '')\nssh host id\nEOF",
		"bash <<'EOF' \"$(printf '')\"\nssh host id\nEOF",
		"bash <<'EOF' $(x) script.sh\nssh host id\nEOF",
		"bash 3<<'EOF' <&3 < <(cat)\nssh host id\nEOF",
		"bash 3<<'EOF' 4<&03\nssh host id\nEOF",
		"bash 3<<'EOF' 4>& 3\nssh host id\nEOF",
		"bash 3<<'EOF' 4>&'3'\nssh host id\nEOF",
		"bash 3<<'EOF' 3>out <&3\nssh host id\nEOF",
		"bash <<'EOF' 0>out\nssh host id\nEOF",
		"bash 3<<'EOF' 4>&3\nssh host id\nEOF",
		"bash 3<<'EOF' & cat <&3\nssh host id\nEOF",
		"bash 3<<'EOF' 4<&3\nssh host id\nEOF",
		"bash <<'EOF' <&3\nssh host id\nEOF",
		"bash - script.sh <<'EOF'\nssh host id\nEOF",
		"bash -- script.sh <<'EOF'\nssh host id\nEOF",
		"bash -- -s <<'EOF'\nssh host id\nEOF",
		"bash -c 'printf safe' -s <<'EOF'\nssh host id\nEOF",
		"bash script.sh -s <<'EOF'\nssh host id\nEOF",
		"bash <<'EOF' 'x<y'\nssh host id\nEOF",
		"bash 3<<'EOF'\nssh host id\nEOF",
		"bash 3<<EOF\nssh host id\nEOF",
		"bash <<EOF\nprintf '%s' '\\$(ssh host id)'\nEOF",
		"cat <<'E'; bash </dev/null\nssh host id\nE",
		"cat <<'E'; bash\nssh host id\nE",
		"bash -c 'make' <<'EOF'\nssh host id\nEOF",
		"bash <<'EOF' 0</dev/null\nssh host id\nEOF",
	]) {
		assertEq(gate(bash(command))?.block, undefined,
			`the body is data: ${command}`);
	}
	for (const command of [
		"bash - <<'SH'\nssh host id\nSH",
		"bash <<'A' 3<&0 <<'B' <&3\nssh host id\nA\nplain\nB",
		"cat <<'A' | bash <<'B'\nprintf safe\nA\nssh host id\nB",
		"cat <<'A' | bash\nssh host id\nA",
		"cat <<'A' | bash 3</dev/null\nssh host id\nA",
		"cat <<'A' | bash 3<&0 <<'B' <&3\nssh host id\nA\nprintf safe\nB",
		"bash <<'EOF' 1>&2 <&0\nssh host id\nEOF",
		"sudo bash <<'SH'\nssh host id\nSH",
		"sh <<-EOF\n\tssh host id\n\tEOF",
		"cat <<EOF\n$(ssh host id)\nEOF",
		"cat <<'EOF' | bash\nssh host id\nEOF",
		"  bash <<'EOF'\nssh host id\nEOF",
		"bash <<'EOF' 3</dev/null\nssh host id\nEOF",
		"bash </dev/null <<'EOF'\nssh host id\nEOF",
		"bash <<''\nssh host id\n\n",
		"bash <<EOF\nprintf '%s' '$(ssh host id)'\nEOF",
		"bash <<EOF\nprintf '%s' \"\\$(ssh host id)\"\nEOF",
		"cat <<EOF\nlit \\\n$(ssh host id)\nEOF",
		"cat <<EOF\nE\\\nOF\nssh host id",
		"bash 0<<'EOF'\nssh host id\nEOF",
		"bash <<'A' 3<<'B'\nssh host id\nA\nplain\nB",
		"bash 3<<'A' <<'B'\nplain\nA\nssh host id\nB",
		"bash <<'EOF' >\" <ignored\"\nssh host id\nEOF",
		"bash <<'EOF' >\"a<b\"\nssh host id\nEOF",
		"bash -s arg <<'EOF'\nssh host id\nEOF",
		"bash -- <<'EOF'\nssh host id\nEOF",
		"bash +e <<'EOF'\nssh host id\nEOF",
		"bash -s -- arg <<'EOF'\nssh host id\nEOF",
		"bash --noprofile <<'EOF'\nssh host id\nEOF",
		"bash --rcfile f <<'EOF'\nssh host id\nEOF",
		"bash 3<<'EOF' <&3\nssh host id\nEOF",
		"bash 3<<'EOF' 4<&3 <&4\nssh host id\nEOF",
		"bash <<'EOF' & cat </dev/null\nssh host id\nEOF",
		"bash 3<<'EOF' 0>&3\nssh host id\nEOF",
		"bash <<'EOF' 3>&0 <&3\nssh host id\nEOF",
		"bash 3<<'EOF' <&'3'\nssh host id\nEOF",
		"bash 3<<'EOF' <&\\3\nssh host id\nEOF",
		"bash 3<<'EOF' <& 3\nssh host id\nEOF",
		"bash <&0 -s arg <<'EOF'\nssh host id\nEOF",
		"bash 2>&1 <<'EOF'\nssh host id\nEOF",
		"bash 3<<'EOF' <&\\\n3\nssh host id\nEOF",
		"bash 3<<'EOF' <&\\\n 3\nssh host id\nEOF",
		"bash 3<<'EOF' <&\"\\\n3\"\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'3'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\063'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\x33'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\u0033'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\U00000033'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'\\u33'\nssh host id\nEOF",
		"bash 3<<'EOF' <&$'3\\0ignored'\nssh host id\nEOF",
		"cat <<$'EOF'\nsafe\nEOF\nssh host id",
		"bash <<$'\\u0045OF'\nssh host id\nEOF",
		"cat <<\"E\\OF\"\nsafe\nE\\OF\nssh host id",
		"cat <<\"E\\\nOF\"\nsafe\nEOF\nssh host id",
		"bash<<<safe <<'EOF'\nssh host id\nEOF",
		"bash <<<safe <<'EOF'\nssh host id\nEOF",
		"bash <<< safe <<'EOF'\nssh host id\nEOF",
		"cat <<<EOF\nssh host id",
		"cat <<<'EOF'\nssh host id",
		"cat <<E\\\nOF\nsafe\nEOF\nssh host id",
		"bash <<E\\\nOF\n$(ssh host id)\nEOF",
		"bash 3<<'EOF' <& '3'\nssh host id\nEOF",
		"bash 3<<'EOF' <&3-\nssh host id\nEOF",
		"bash 3<<'EOF' <&03\nssh host id\nEOF",
		"bash <<'EOF' > >(cat </dev/null)\nssh host id\nEOF",
		"bash <<'EOF' >$(printf /dev/null </dev/null)\nssh host id\nEOF",
		"bash <<'EOF' >`cat </dev/null`\nssh host id\nEOF",
		"bash <<'EOF' $(printf '')\nssh host id\nEOF",
		"bash -- $(printf '') <<'EOF'\nssh host id\nEOF",
		"bash '-s' arg <<'EOF'\nssh host id\nEOF",
		"bash '--' <<'EOF'\nssh host id\nEOF",
		"bash -eo posix <<'EOF'\nssh host id\nEOF",
		"bash -euo posix <<'EOF'\nssh host id\nEOF",
		"bash +s -s script.sh <<'EOF'\nssh host id\nEOF",
		"bash <<'EOF' # </dev/null\nssh host id\nEOF",
		"bash <<'EOF' #<stdin\nssh host id\nEOF",
		"bash <<'EOF' # x\nssh host id\nEOF",
		"bash <<'EOF' >$(printf /dev/null; :)\nssh host id\nEOF",
		"bash <<'EOF' > >(cat; :)\nssh host id\nEOF",
		"bash <<'EOF' >out3\nssh host id\nEOF",
		"bash <<'EOF' >o3 <&0\nssh host id\nEOF",
		"bash <<'A'; cat </dev/null\nssh host id\nA",
		"bash <<'A'; cat <<'B'\nssh host id\nA\nsafe\nB",
		"bash <<'OUT'\nsh <<'IN'\nssh host id\nIN\nOUT",
	]) {
		assertEq(gate(bash(command))?.block, true,
			`the shell runs the body: ${command}`);
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
