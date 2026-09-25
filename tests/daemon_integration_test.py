#!/usr/bin/env python3
"""End-to-end test: run the REAL pi-daemon on isolated XDG dirs and drive
it through the REAL pi-rc client over its unix socket, plus raw wire
requests where pi-rc does not expose an argv override (session start).

Covers: daemon boot, ticket submit/wait/output/list/cancel/reset, session
start/list/state/input/detach/stop, owned-session and --here dir matching,
protocol failures, and clean shutdown.
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
AGENT = os.path.join(SCRATCH, "agent")
os.makedirs(AGENT, exist_ok=True)
ENV.update({"XDG_RUNTIME_DIR": RUNTIME, "XDG_STATE_HOME": STATE,
            "PI_CODING_AGENT_DIR": AGENT,
            "PI_PTYD_TICKET_TTL_HOURS": "1", "PI_PTYD_GC": "3600"})

FAIL = []


def fail(name, message):
    FAIL.append(name)
    print(f"  FAIL {name}: {message}")


def ok(name):
    print("  ok   " + name)


def pi_rc(*args, timeout=30, cwd=None):
    return subprocess.run([sys.executable, PI_RC, *args], env=ENV, cwd=cwd,
                          capture_output=True, text=True, timeout=timeout)


def read_endpoint():
    # The runtime endpoint file carries {host, port, token}; the control
    # transport is loopback TCP with a mandatory hello handshake.
    with open(SOCK) as fh:
        return json.load(fh)


def control_socket(endpoint):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((endpoint["host"], endpoint["port"]))
    s.sendall(json.dumps({"cmd": "hello",
                          "token": endpoint["token"]}).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    reply = json.loads(buf.split(b"\n", 1)[0])
    if not reply.get("ok"):
        raise OSError("handshake failed")
    return s


def socket_alive():
    try:
        s = control_socket(read_endpoint())
        s.close()
        return True
    except (OSError, ValueError):
        return False


def wire(req):
    s = control_socket(read_endpoint())
    s.sendall(json.dumps(req).encode() + b"\n")
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
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

    # Offset mode must be byte-faithful through the base64 wire format:
    # non-ASCII output must survive the round trip intact, and a read
    # from the end offset must report no new bytes.
    r = pi_rc("ticket-submit", "--cwd", SCRATCH, "--",
              "printf 'caf\\xc3\\xa9-\\xe4\\xb8\\xad' && sleep 0")
    if r.returncode != 0:
        fail("utf8 ticket submit", f"rc={r.returncode}")
        return 1
    utf8_tid = r.stdout.strip().split()[1]
    r = pi_rc("ticket-wait", utf8_tid, "10")
    rec = json.loads(r.stdout.strip().splitlines()[0])
    if rec["status"] != "done" or rec["exit"] != 0:
        fail("utf8 ticket wait", r.stdout)
        return 1
    r = pi_rc("ticket-output", utf8_tid, "0")
    payload = base64.b64decode(r.stdout.strip())
    if payload != "café-中".encode("utf-8"):
        fail("utf8 ticket output offset", repr(payload))
        return 1
    ok("ticket output offset round-trips non-ASCII bytes")
    r = pi_rc("ticket-output", utf8_tid,
              str(len("café-中".encode("utf-8"))))
    if base64.b64decode(r.stdout.strip()) != b"":
        fail("utf8 ticket output drained", r.stdout)
        return 1
    ok("ticket output at end offset reports no new bytes")

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
    r = pi_rc("owned", cwd=SCRATCH)
    if r.stdout.strip() != "itest":
        fail("owned session for cwd", f"out={r.stdout!r} err={r.stderr!r}")
    else:
        ok("owned session for cwd")
    r = pi_rc("ls", "--here", cwd=SCRATCH)
    if "itest" not in r.stdout:
        fail("ls --here filters by cwd", r.stdout)
    else:
        ok("ls --here filters by cwd")
    elsewhere = os.path.join(SCRATCH, "elsewhere")
    os.makedirs(elsewhere, exist_ok=True)
    r = pi_rc("owned", cwd=elsewhere)
    if r.stdout.strip():
        fail("owned empty in another dir", r.stdout)
    else:
        ok("owned empty in another dir")
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

    # -- GC reap: the tool acks through gc-reap-ack, never a force-kill --
    r = pi_rc("finish", "itest2", "--working")
    if r.returncode == 0 or "unknown command" not in r.stderr:
        fail("finish command removed", r.stdout + r.stderr)
    else:
        ok("finish command removed (use daemon_gc_reap instead)")
    r = pi_rc("gc-reap-ack", "itest2")
    if r.returncode != 0:
        fail("gc-reap-ack", r.stderr)
    else:
        ok("gc-reap-ack")
    r = pi_rc("daemon-purge")
    if r.returncode != 0:
        fail("daemon-purge", r.stderr)
    else:
        ok("daemon-purge (no idle sessions past the window)")
    r = pi_rc("stop", "itest2")
    if r.returncode != 0:
        fail("stop the gc-reap session", r.stderr)
    else:
        ok("stop the gc-reap session")

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