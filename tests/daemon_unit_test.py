#!/usr/bin/env python3
"""Unit tests for the pi-daemon PTY host daemon and pi-rc client.

Imports the real `pi/daemon/pi-daemon` and `pi/bin/pi-rc` modules with
isolated XDG dirs (scratch under ~/tmp, never /tmp) and exercises the
pure ownership logic plus the in-process ticket/session control surface
with a real daemon object (never `serve()`/`shutdown()` - shutdown ends
in os._exit and would kill the test process).
"""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import shutil
SCRATCH = tempfile.mkdtemp(
    prefix="pi-daemon-unit-", dir=os.path.expanduser("~/tmp"))
os.environ["XDG_STATE_HOME"] = os.path.join(SCRATCH, "state")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(SCRATCH, "runtime")
os.makedirs(os.environ["XDG_STATE_HOME"], exist_ok=True)
os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


from importlib.machinery import SourceFileLoader

def load(name, path):
    return SourceFileLoader(name, path).load_module()


daemon = load("pi_daemon", os.path.join(REPO, "pi", "daemon", "pi-daemon"))
pi_rc = load("pi_rc", os.path.join(REPO, "pi", "bin", "pi-rc"))

PASS = 0
FAIL = []


def ok(name, fn):
    global PASS
    try:
        fn()
        PASS += 1
        print("  ok   " + name)
    except Exception as exc:
        FAIL.append(name)
        import traceback
        traceback.print_exc()
        print("  FAIL " + name + ": " + str(exc))


def assert_eq(actual, expected, msg=""):
    assert actual == expected, f"{msg} expected {expected!r}, got {actual!r}"


def assert_true(cond, msg=""):
    assert cond, msg


# --- pi-rc name helpers ---------------------------------------------------

def test_names():
    assert_eq(pi_rc.session_name("foo"), "pi-foo")
    assert_eq(pi_rc.session_name("a:b"), "pi-a_b")
    assert_eq(pi_rc.short_name("pi-foo"), "foo")
    assert_eq(pi_rc.session_base("=x"), "x")
    assert_true(pi_rc.same_dir("/tmp/x", "/tmp/./x"))
    name = pi_rc.unique_session_name("base", 123, lambda n: False)
    assert_true(name.startswith("pi-base-"), name)
    assert_true(len(name) > len("pi-base-"), "hash suffix present")


# --- registry -------------------------------------------------------------

def test_registry():
    reg = daemon.Registry(os.path.join(SCRATCH, "sessions.json"))
    reg.add("pi-a", "/x", ["pi"])
    assert_eq(reg.load()["pi-a"]["dir"], "/x")
    reg.set_argv("pi-a", ["pi", "--session", "/x/a.jsonl"])
    assert_eq(reg.load()["pi-a"]["argv"][2], "/x/a.jsonl")
    reg.drop("pi-a")
    assert_true("pi-a" not in reg.load())


# --- ticket store ---------------------------------------------------------

def test_ticket_store():
    store = daemon.TicketStore(
        os.path.join(SCRATCH, "tickets.json"),
        os.path.join(SCRATCH, "ticket-logs"))
    store.load()
    with store.lock:
        t1 = store.alloc_id_locked()
        t2 = store.alloc_id_locked()
    assert_eq(t1, "t-1")
    assert_eq(t2, "t-2")
    rec = {"id": t1, "kind": "shell", "status": "running", "command": "x",
           "cwd": "/x", "session": "s", "created": 1.0, "started": 1.0}
    store.add(rec)
    assert_eq(store.get(t1)["session"], "s")
    store.update(t1, status="done", finished=2.0)
    assert_eq(store.get(t1)["status"], "done")
    snap = store.snapshot("s", "shell")
    assert_eq(len(snap), 1)
    assert_eq(len(store.snapshot("other")), 0)
    removed = store.gc(now=1000.0, ttl=1.0, max_finished=0)
    assert_true(t1 in removed, "finished ticket expired by GC")
    assert_true(store.get(t1) is None)
    # running tickets never expire
    rec2 = {"id": t2, "kind": "shell", "status": "running", "command": "y",
            "cwd": "/x", "session": "s", "created": 1.0, "started": 1.0}
    store.add(rec2)
    assert_eq(store.gc(now=9999.0, ttl=0.0, max_finished=0), [])
    try:
        store.remove(t2)
        assert False, "running remove must raise"
    except ValueError:
        pass
    store.update(t2, status="done")
    store.remove(t2)
    assert_true(store.get(t2) is None)
    # reset wipes and restarts the counter
    store.add(rec2)
    count = store.reset()
    assert_eq(count, 1)
    with store.lock:
        assert_eq(store.alloc_id_locked(), "t-1")
    # read_range bounds
    log = store.log_path("t-1")
    os.makedirs(store.log_dir, exist_ok=True)
    with open(log, "wb") as f:
        f.write(b"0123456789")
    start, nxt, size, data = store.read_range(log, 3, 4)
    assert_eq((start, nxt, size), (3, 7, 10))
    assert_eq(data, b"3456")
    _, nxt2, _, data2 = store.read_range(log, 9, 4)
    assert_eq(data2, b"9")
    assert_eq(nxt2, 10)
    _, nxt3, _, data3 = store.read_range(log, 99, 4)
    assert_eq(data3, b"")
    assert_eq(nxt3, 99)


# --- extension fingerprint/diff -------------------------------------------

def test_ext_fingerprint():
    root = os.path.join(SCRATCH, "ext")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    for rel in ("a.ts", "sub/b.ts"):
        with open(os.path.join(root, rel), "w") as f:
            f.write("x")
    fp1 = daemon.ext_root_fingerprint([root])
    assert_eq(sorted(fp1), [os.path.join(root, "a.ts"),
                            os.path.join(root, "sub", "b.ts")])
    # change b, add c
    time.sleep(0.01)
    with open(os.path.join(root, "sub", "b.ts"), "w") as f:
        f.write("xx")
    with open(os.path.join(root, "c.ts"), "w") as f:
        f.write("y")
    fp2 = daemon.ext_root_fingerprint([root])
    diff = daemon.ext_diff_files(fp1, fp2)
    assert_eq(diff["added"], [os.path.join(root, "c.ts")])
    assert_eq(diff["removed"], [])
    assert_eq(diff["changed"], [os.path.join(root, "sub", "b.ts")])
    # missing path skipped
    assert_eq(daemon.ext_root_fingerprint([os.path.join(root, "nope")]), {})


# --- exit classification --------------------------------------------------

def test_exit_classification():
    # normal exit 0 -> not abnormal
    p = subprocess.run(["sh", "-c", "exit 0"])
    assert_true(not daemon._exit_abnormal(p.returncode if p.returncode < 256 else 0))
    # nonzero exit -> abnormal
    p = subprocess.run(["sh", "-c", "exit 7"])
    assert_true(daemon._exit_abnormal((7 << 8) & 0xFFFF))
    # signal death -> abnormal
    proc = subprocess.Popen(["sh", "-c", "kill -TERM $$"])
    _, status = os.waitpid(proc.pid, 0)
    assert_true(daemon._exit_abnormal(status))
    assert_true(os.WIFSIGNALED(status))


# --- in-process daemon control (tickets + sessions) -----------------------

# The daemon needs one instance shared across ticket/session tests; the
# shutdown path (os._exit) is never called - cleanup is explicit.
DAEMON = daemon.Daemon(daemon.Registry(daemon.REG_PATH),
                       sock_path=os.path.join(SCRATCH, "daemon.sock"))


def _drain_session(name):
    """Wait for a fake session's relay loop to drop it after stop/exit."""
    for _ in range(50):
        if DAEMON.table.get(name) is None:
            return True
        time.sleep(0.05)
    return False


def test_ticket_control():
    DAEMON.tickets.start()
    r = DAEMON.control.ticket_submit(
        {"session": "s1", "cwd": SCRATCH,
         "command": "printf 'out-marker'; printf 'err-marker' >&2; /bin/true"})
    assert_true(r.get("ok"), r)
    tid = r["id"]
    rec = DAEMON.control.ticket_wait({"id": tid, "timeout": 5})
    assert_true(rec["ok"], rec)
    assert_eq(rec["ticket"]["status"], "done")
    assert_eq(rec["ticket"]["exit"], 0)
    out = DAEMON.control.ticket_output({"id": tid})
    assert_true(out["ok"])
    assert_true("out-marker" in out["output"], out["output"])
    assert_true("err-marker" in out["output"], "stderr merged into the log")
    # incremental offset read
    raw = DAEMON.control.ticket_output({"id": tid, "offset": 0})
    assert_true(raw["ok"] and raw["data"])
    # list + kind filter
    listed = DAEMON.control.ticket_list({})
    assert_true(any(t["id"] == tid for t in listed["tickets"]))
    shell = DAEMON.control.ticket_list({"kind": "shell"})
    assert_true(any(t["id"] == tid for t in shell["tickets"]))
    bad = DAEMON.control.ticket_list({"kind": "agent"})
    assert_true(not bad.get("ok"), "agent kind rejected")
    # remove finished
    rem = DAEMON.control.ticket_remove({"id": tid})
    assert_true(rem.get("ok"))
    assert_true(DAEMON.control.ticket_remove({"id": tid}).get("error") == "no-ticket")


def test_ticket_cancel_and_reset():
    r = DAEMON.control.ticket_submit(
        {"session": "s1", "cwd": SCRATCH, "command": "sleep 30"})
    tid = r["id"]
    rec = DAEMON.control.ticket_wait({"id": tid, "timeout": 0})
    assert_eq(rec["ticket"]["status"], "running")
    cancelled = DAEMON.control.ticket_cancel({"id": tid})
    assert_true(cancelled["ok"])
    assert_eq(cancelled["ticket"]["status"], "cancelled")
    reset = DAEMON.control.tickets_reset({})
    assert_true(reset["ok"])
    assert_true(reset["removed"] >= 1)
    # counter restarted
    r2 = DAEMON.control.ticket_submit(
        {"session": "s1", "cwd": SCRATCH, "command": "printf x"})
    assert_eq(r2["id"], "t-1")
    DAEMON.control.ticket_remove({"id": r2["id"]})


def test_session_control():
    name = "pi-unittest"
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH,
         "argv": ["sh", "-c", "echo hosted-ready; sleep 30"]})
    assert_true(r.get("ok"), r)
    assert_true(r.get("created"))
    assert_true(_drain_session(name) is False or DAEMON.table.get(name) is not None)
    listed = DAEMON.control.list({})
    names = [s["name"] for s in listed["sessions"]]
    assert_true(name in names, names)
    st = DAEMON.control.state({"name": name, "state": "busy"})
    assert_true(st["ok"])
    listed = DAEMON.control.list({})
    sess = next(s for s in listed["sessions"] if s["name"] == name)
    assert_eq(sess["state"], "busy")
    assert_eq(DAEMON.control.which({"file": "/nonexistent/x.jsonl"})["name"], None)
    wr = DAEMON.control.input({"name": name, "text": "x"})
    assert_true(wr["ok"] and wr["bytes"] > 0)
    det = DAEMON.control.detach({"name": name})
    assert_true(det["ok"])
    DAEMON.control.stop({"name": name})
    assert_true(_drain_session(name), "session ended after stop")
    unknown = DAEMON.control.stop({"name": "pi-no-such"})
    assert_true(unknown["ok"])
    # bad requests are rejected
    assert_true(not DAEMON.control.start({"name": "", "dir": SCRATCH,
                                          "argv": ["sh"]}).get("ok"))
    assert_true(not DAEMON.control.state({"name": name, "state": "loud"}).get("ok"))


def main():
    try:
        return _main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def _main():
    ok("pi-rc name helpers", test_names)
    ok("registry round-trip", test_registry)
    ok("ticket store lifecycle", test_ticket_store)
    ok("extension fingerprint/diff", test_ext_fingerprint)
    ok("exit classification", test_exit_classification)
    ok("ticket control (submit/wait/output/list/remove)", test_ticket_control)
    ok("ticket cancel + reset", test_ticket_cancel_and_reset)
    ok("session control (start/list/state/input/detach/stop)", test_session_control)
    print(f"\n{PASS}/{PASS + len(FAIL)} unit tests passed")
    if FAIL:
        print("Failed: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())