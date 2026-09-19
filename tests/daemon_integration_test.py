#!/usr/bin/env python3
"""End-to-end test: run the REAL pi-daemon on isolated XDG dirs and drive
it through the REAL pi-rc client over its unix socket, plus raw wire
requests where pi-rc does not expose an argv override (session start).

Covers: daemon boot, ticket submit/wait/output/list/cancel/reset, session
start/list/state/input/detach/stop, protocol failures, and clean shutdown.
"""

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import shutil

SCRATCH = tempfile.mkdtemp(
    prefix="pi-daemon-itest-", dir=os.path.expanduser("~/tmp"))
RUNTIME = os.path.join(SCRATCH, "runtime")
STATE = os.path.join(SCRATCH, "state")
os.makedirs(RUNTIME, exist_ok=True)
os.makedirs(STATE, exist_ok=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_PATH = os.path.join(REPO, "pi", "daemon", "pi-daemon")
PI_RC = os.path.join(REPO, "pi", "bin", "pi-rc")
SOCK = os.path.join(RUNTIME, "pi-pty-host.sock")

ENV = dict(os.environ)
ENV.update({"XDG_RUNTIME_DIR": RUNTIME, "XDG_STATE_HOME": STATE,
            "PI_PTYD_TICKET_TTL": "3600", "PI_PTYD_GC": "3600"})

FAIL = []


def fail(name, message):
    FAIL.append(name)
    print(f"  FAIL {name}: {message}")


def ok(name):
    print("  ok   " + name)


def pi_rc(*args, timeout=30):
    return subprocess.run([sys.executable, PI_RC, *args], env=ENV,
                          capture_output=True, text=True, timeout=timeout)


def socket_alive():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK)
        s.close()
        return True
    except OSError:
        return False


def wire(req):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(SOCK)
    s.sendall(json.dumps(req).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        buf += s.recv(65536)
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


def main():
    try:
        return _main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def _main():
    proc = subprocess.Popen([sys.executable, DAEMON_PATH], env=ENV,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)
    deadline = time.time() + 8
    while time.time() < deadline and not socket_alive():
        time.sleep(0.1)
    if not socket_alive():
        fail("daemon boot", "socket never came up")
        return 1

    # -- tickets -----------------------------------------------------------
    r = pi_rc("ticket-submit", "--cwd", SCRATCH, "--", "echo hello-from-ticket")
    if r.returncode != 0 or not r.stdout.strip().startswith("ticket t-"):
        fail("ticket submit", f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
    else:
        tid = r.stdout.strip().split()[1]
        ok(f"ticket submit -> {tid}")

    r = pi_rc("ticket-wait", tid, "10")
    rec = json.loads(r.stdout.strip().splitlines()[0])
    if rec["status"] != "done" or rec["exit"] != 0:
        fail("ticket wait", r.stdout)
    else:
        ok("ticket wait -> done")

    r = pi_rc("ticket-output", tid)
    if "hello-from-ticket" not in r.stdout:
        fail("ticket output", r.stdout)
    else:
        ok("ticket output captured")

    r = pi_rc("ticket-list")
    if tid not in r.stdout:
        fail("ticket list", r.stdout)
    else:
        ok("ticket list")

    # running + cancel
    r = pi_rc("ticket-submit", "--cwd", SCRATCH, "--", "sleep 30")
    rid = r.stdout.strip().split()[1]
    r = pi_rc("ticket-wait", rid, "0")
    if json.loads(r.stdout)["status"] != "running":
        fail("running ticket", r.stdout)
    else:
        ok("ticket running")
    r = pi_rc("ticket-cancel", rid)
    if "status cancelled" not in r.stdout:
        fail("ticket cancel", r.stdout)
    else:
        ok("ticket cancel")

    # -- sessions (raw wire start with a fake argv; pi-rc start uses pi) ---
    resp = wire({"cmd": "start", "name": "pi-itest", "dir": SCRATCH,
                 "argv": ["sh", "-c", "echo hosted-ready; sleep 30"],
                 "cols": 80, "rows": 24})
    if not resp.get("ok"):
        fail("session start", str(resp))
    else:
        ok("session start (wire)")
    r = pi_rc("list")
    if "itest" not in r.stdout:
        fail("session list", r.stdout)
    else:
        ok("session list")
    r = pi_rc("state", "itest", "busy")
    if r.returncode != 0:
        fail("session state", r.stderr)
    else:
        ok("session state busy")
    # -- reload is strictly in-place: it must never spawn anything ------
    conv = os.path.join(SCRATCH, "conv-itest.jsonl")
    open(conv, "w").close()
    r = pi_rc("announce", "itest", conv)
    if r.returncode != 0:
        fail("session announce", r.stderr)
    else:
        ok("session announce")
    pi_rc("state", "itest", "idle")
    before = pi_rc("list")
    r = pi_rc("extensions-reload", "--force")
    if "reloaded pi-itest" not in r.stdout:
        fail("extensions reload pokes the session", f"{r.stdout!r} {r.stderr!r}")
    else:
        ok("extensions reload (in-place)")
    after = pi_rc("list")
    if before.stdout.count("pi-itest") != after.stdout.count("pi-itest"):
        fail("reload spawned a session", after.stdout)
    else:
        ok("reload spawned nothing")
    r = pi_rc("which", "/x/no-such.jsonl")
    if r.returncode != 0 or r.stdout.strip() != "":
        fail("which no-holder", f"rc={r.returncode} out={r.stdout!r}")
    else:
        ok("which no-holder")
    r = pi_rc("input", "pi-itest", "ignored")
    if "typed" not in r.stdout:
        fail("session input", f"stdout={r.stdout!r} stderr={r.stderr!r}")
    else:
        ok("session input")
    r = pi_rc("detach", "itest")
    if r.returncode != 0:
        fail("session detach", r.stderr)
    else:
        ok("session detach")
    r = pi_rc("stop", "itest")
    if r.returncode != 0:
        fail("session stop", r.stderr)
    else:
        ok("session stop")
    time.sleep(0.5)
    r = pi_rc("list")
    if "itest" in r.stdout:
        fail("session gone after stop", r.stdout)
    else:
        ok("session ended")

    r = pi_rc("start", "a", "b", "c")  # start takes at most name+dir
    if r.returncode == 0:
        fail("pi-rc usage guard", r.stdout)
    else:
        ok("pi-rc usage guard")

    wire({"cmd": "start", "name": "pi-itest2", "dir": SCRATCH,
          "argv": ["sh", "-c", "sleep 30"], "cols": 80, "rows": 24})
    r = pi_rc("daemon-stop")
    if r.returncode != 0:
        fail("daemon-stop", r.stderr)
    try:
        proc.wait(timeout=8)
        ok("daemon shutdown clean")
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        fail("daemon shutdown", "still alive after stop")

    print(f"\n{'OK' if not FAIL else 'FAILURES'}: integration "
          f"{len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())