#!/usr/bin/env python3
"""pi-daemon validation runner: OOP/removal lint, unit tests, and the
real daemon+client integration test. Zero dependencies (stdlib only).

Every step runs in one suite-owned environment, so no test can read,
write or signal the host's own daemon roots.

The runner reports what it measured, not what it hoped: the exact
invocation, each step's outcome, how many tests ran, and every case this
host could not exercise - as unsupported when the platform does not
implement the property, as blocked when a prerequisite is unavailable.
A suite that skips its way to exit zero is never called a pass.

Exit codes: 0 every step passed and nothing was blocked; 1 a step failed
or the host's daemon did not survive; 2 validation was incomplete
because a case reported its prerequisite blocked.

Usage:
    python3 tests/run.py
"""

import os
import re
import socket
import subprocess
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

import isolation
import pi_platform
import pi_services

# A skip that names an unavailable prerequisite is blocked, not
# unsupported: the property may well hold here, the host just cannot show
# it. `skipTest("blocked: long paths are disabled")` is the form.
BLOCKED = "blocked"

RAN = re.compile(r"^Ran (?P<count>\d+) tests? in ")
SKIPPED = re.compile(
    r"^(?P<name>\S+) \((?P<where>[^)]*)\) \.\.\. skipped (?P<reason>.*)$")
PROBLEM = re.compile(
    r"^(?P<kind>FAIL|ERROR): (?P<name>\S+) \((?P<where>[^)]*)\)")


class Step:
    """One command the gate runs, and what its own output says it did.

    The output is captured and parsed because a `unittest` module exits
    zero while skipping tests, and a gate that trusts the exit code alone
    reports coverage it never had.
    """

    def __init__(self, name, command, suite=False):
        self.name = name
        self.command = command
        self.suite = suite
        self.returncode = None
        self.ran = 0
        self.failed = []
        self.unsupported = []
        self.blocked = []

    def execute(self, environment, scratch, index):
        print(f"== {self.name} ==")
        print("command: " + " ".join(self.command))
        log = os.path.join(scratch.root, "step-%d.log" % index)
        with open(log, "wb") as handle:
            self.returncode = subprocess.run(
                self.command, cwd=REPO, env=environment,
                stdout=handle, stderr=subprocess.STDOUT).returncode
        with open(log, "r", encoding="utf-8", errors="replace") as handle:
            output = handle.read()
        sys.stdout.write(output)
        if self.suite:
            self._read(output)
        print()
        ok = self.returncode == 0 and not self.failed
        print(f"{self.name}: ok" if ok
              else f"{self.name}: FAILED ({self.returncode})")
        return ok

    def _read(self, output):
        for line in output.splitlines():
            found = RAN.match(line)
            if found:
                self.ran = int(found.group("count"))
                continue
            found = SKIPPED.match(line)
            if found:
                identifier = "%s (%s)" % (found.group("name"),
                                          found.group("where"))
                reason = found.group("reason").strip("'\"")
                if reason.startswith(BLOCKED):
                    self.blocked.append(
                        (identifier, reason[len(BLOCKED):].lstrip(" :")))
                else:
                    self.unsupported.append((identifier, reason))
                continue
            found = PROBLEM.match(line)
            if found:
                self.failed.append("%s (%s)" % (found.group("name"),
                                                found.group("where")))

    @property
    def passed(self):
        if not self.suite:
            return 1 if self.returncode == 0 else 0
        return max(0, self.ran - len(self.failed)
                   - len(self.unsupported) - len(self.blocked))


class Sentinel:
    """A service outside every suite root that no test may reach.

    The environment names the suite's own services directory, so a test
    that dials a service dials a scratch fixture. This descriptor lives in
    a sibling directory nothing points at, behind a listener that counts
    its connections: whatever the count is at the end is proof about
    where the suite looked.
    """

    TOKEN = "sentinel-not-a-real-token"

    def __init__(self, root):
        self.root = root
        self.connections = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.5)
        host, port = self._listener.getsockname()
        endpoint = os.path.join(root, "sentinel-endpoint.json")
        pi_platform.AtomicStateFile(endpoint).write(
            {"host": host, "port": port, "token": self.TOKEN})
        pi_services.ServiceDirectory(root=root).publish(
            endpoint, ["sentinel-not-a-real-service"], pid=os.getpid())
        self._running = True
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while self._running:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                continue
            self.connections.append(connection.getpeername())
            connection.close()

    def close(self):
        self._running = False
        self._thread.join(2.0)
        self._listener.close()


def host_daemons(process):
    """The host's installed descriptors and the identity of each one.

    Read-only: discovery lists and reads files, and the start token is a
    query on a live handle, never a signal. None means the host's own
    services root could not be read at all.
    """
    try:
        entries = pi_services.ServiceDirectory().find()
    except OSError:
        return None
    snapshot = []
    for entry in entries:
        pid = entry.get("pid")
        token = (process.process_start_token(pid)
                 if isinstance(pid, int) else None)
        snapshot.append((entry.get("endpoint_file"), pid, token))
    return snapshot


def preservation(before, after):
    """Whether the host's daemon survived the run unchanged."""
    if before is None or after is None:
        return ("not verified", "the host's services root was unreadable")
    if not before:
        return ("not verified",
                "no installed daemon was running when the suite started, "
                "so continuity cannot be established")
    if before == after:
        return ("verified",
                "%d installed descriptor(s) kept their pid and start token"
                % len(before))
    return ("VIOLATED",
            "installed descriptors changed: %r -> %r" % (before, after))


def steps():
    """The gate, in order: lint, unit suites, then the real integrations."""
    python = sys.executable
    return [
        Step("OOP + removal lint", [python, "tests/oop_lint.py"]),
        Step("unified service tests", [python, "tests/service_test.py", "-v"],
             suite=True),
        Step("sessions domain tests", [python, "tests/session_test.py", "-v"],
             suite=True),
        Step("transcripts domain tests",
             [python, "tests/transcripts_test.py", "-v"], suite=True),
        Step("tasks domain tests", [python, "tests/tasks_test.py", "-v"],
             suite=True),
        Step("migration tests", [python, "tests/migration_test.py", "-v"],
             suite=True),
        Step("comms domain tests", [python, "tests/comms_test.py", "-v"],
             suite=True),
        Step("comms socket tests",
             [python, "tests/comms_rpc_test.py", "-v"], suite=True),
        Step("service discovery tests",
             [python, "tests/comms_discovery_test.py", "-v"], suite=True),
        Step("pi-comms CLI tests", [python, "tests/pi_comms_test.py", "-v"],
             suite=True),
        Step("pi-rc discovery tests",
             [python, "tests/pi_rc_discovery_test.py", "-v"], suite=True),
        Step("pi-rc activation tests",
             [python, "tests/pi_rc_activation_test.py", "-v"], suite=True),
        Step("extension tests (TS)",
             ["node", "--experimental-strip-types",
              "--experimental-transform-types", "tests/ts/run.ts"]),
        Step("conversation index tests",
             [python, "tests/transcript_index_test.py", "-v"], suite=True),
        Step("transcript wire test",
             [python, "tests/transcript_wire_test.py", "-v"], suite=True),
        # Its own harness: it reports integration outcomes, not test counts.
        Step("daemon integration test",
             [python, "tests/daemon_integration_test.py"]),
        Step("install script tests", [python, "tests/install_test.py", "-v"],
             suite=True),
        Step("ConPTY backend (skips off Windows)",
             [python, "tests/conpty_test.py"], suite=True),
    ]


def report(ran_steps, invocation, before, after, sentinel):
    """Print the gate's own measurement, and say what it does not cover."""
    failed_steps = [step for step in ran_steps
                    if step.returncode != 0 or step.failed]
    unsupported = [(step, case) for step in ran_steps
                   for case in step.unsupported]
    blocked = [(step, case) for step in ran_steps for case in step.blocked]
    tests = sum(step.ran for step in ran_steps if step.suite)
    checks = [step for step in ran_steps if not step.suite]
    failures = (sum(len(step.failed) for step in ran_steps)
                + len([step for step in checks if step.returncode != 0]))
    passed = sum(step.passed for step in ran_steps)
    print("== validation report ==")
    print("command: " + " ".join(invocation))
    print("passed: %d" % passed)
    print("failed: %d%s" % (failures, " - " if failures else ""))
    for step in failed_steps:
        print("  - %s: exited %s" % (step.name, step.returncode))
    print("unsupported: %d%s" % (len(unsupported), " - " if unsupported else ""))
    for step, (identifier, reason) in unsupported:
        print("  - %s: %s - %s" % (step.name, identifier, reason))
    print("blocked: %d%s" % (len(blocked), " - " if blocked else ""))
    for step, (identifier, reason) in blocked:
        print("  - %s: %s - %s" % (step.name, identifier, reason))
    print("steps: %d passed, %d failed of %d"
          % (len(ran_steps) - len(failed_steps), len(failed_steps),
             len(ran_steps)))
    print("checks (steps whose output no test counts): %d of %d"
          % (sum(step.passed for step in checks), len(checks)))
    print("tests: %d ran, %d passed, %d failed"
          % (tests, sum(step.passed for step in ran_steps if step.suite),
             sum(len(step.failed) for step in ran_steps)))
    state, detail = preservation(before, after)
    print("host preservation: %s - %s" % (state, detail))
    if sentinel.connections:
        print("sentinel protection: VIOLATED - %d connection(s) reached a "
              "service outside the suite's roots: %r"
              % (len(sentinel.connections), sentinel.connections))
    else:
        print("sentinel protection: verified - no test reached the service "
              "outside its own roots")
    if unsupported:
        print()
        print("This host does not implement every property the suite "
              "asserts; the cases above are named, and the POSIX-only ones "
              "are expected on Windows: owner/group/other mode equality, "
              "POSIX terminal signal semantics and real systemd user units.")
    return failed_steps, blocked


def main():
    scratch = isolation.ScratchEnvironment()
    environment = scratch.environment()
    process = pi_platform.ProcessControl()
    invocation = [sys.executable] + sys.argv
    before = host_daemons(process)
    # Not a root this gate names anywhere: a service here answers only a
    # test that left its own tree.
    sentinel = Sentinel(os.path.join(scratch.root, "not-a-suite-root"))
    try:
        ran_steps = []
        for index, step in enumerate(steps()):
            ran_steps.append(step)
            step.execute(environment, scratch, index)
        after = host_daemons(process)
        failed_steps, blocked = report(ran_steps, invocation, before, after,
                                       sentinel)
        if failed_steps or sentinel.connections or \
                preservation(before, after)[0] != "verified":
            print()
            print("validation failed")
            return 1
        if blocked:
            print()
            print("validation incomplete: %d case(s) reported a blocked "
                  "prerequisite" % len(blocked))
            return 2
        print()
        print("every step passed on this host")
        return 0
    finally:
        sentinel.close()
        scratch.cleanup()


if __name__ == "__main__":
    sys.exit(main())
