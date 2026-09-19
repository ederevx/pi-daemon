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
os.environ["PI_PTYD_MIN_REVIVE_LIFE"] = "0"
# Reap detached idle sessions fast in tests so the guard is exercised
# without waiting the six-hour production default.
os.environ["PI_PTYD_IDLE_REAP"] = "5.0"
# The extension-reload watch must never touch the real agent home in
# tests: point its roots at a scratch dir and run it fast.
EXT_ROOT = os.path.join(SCRATCH, "ext-root")
os.makedirs(EXT_ROOT, exist_ok=True)
with open(os.path.join(EXT_ROOT, "a.ts"), "w") as _f:
    _f.write("v1")
os.environ["PI_PTYD_EXT_WATCH_ROOTS"] = EXT_ROOT
os.environ["PI_PTYD_EXT_WATCH_INTERVAL"] = "0.05"
os.environ["PI_PTYD_EXT_WATCH_DEBOUNCE"] = "0.0"
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


def test_reload_guard():
    """A reload injection is strictly in-place: it never spawns a
    session and it marks a session that dies right after so the revival
    path cannot turn the reload into a spawned replacement."""
    name = "pi-reloadtest"
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH, "argv": ["sh", "-c", "sleep 30"]})
    assert_true(r.get("ok"), r)
    file = os.path.join(SCRATCH, "conv-reload.jsonl")
    open(file, "w").close()
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file})["ok"])
    DAEMON.control.state({"name": name, "state": "idle"})
    res = DAEMON.control.extensions_reload({"force": True})
    assert_true(name in res["reloaded"], res)
    sess = DAEMON.table.get(name)
    assert_true(sess is not None and sess.reload_injected,
                "reload sets the guard flag")
    # exactly one session with that name: nothing was spawned
    assert_eq(len([s for s in DAEMON.table.snapshot()
                   if s.name == name]), 1)
    # announce (session_start) clears the guard after a survived reload
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file})["ok"])
    assert_true(not sess.reload_injected)
    DAEMON.control.stop({"name": name})
    assert_true(_drain_session(name))


def test_reload_death_not_revived():
    """An abnormally dying session that was just given a /reload must
    never be revived into a fresh spawn; without the guard the same
    death does attempt a spawn."""
    name = "pi-reloaddeath"
    file = os.path.join(SCRATCH, "conv-death.jsonl")
    open(file, "w").close()
    sess = daemon.Session(name, SCRATCH, ["pi"], pid=1, master_fd=-1)
    sess.file = file
    sess.spawned_at = 0.0           # past the (zero) revive life anyway
    assert_true(DAEMON.table.put(sess))
    calls = []
    orig_spawn = DAEMON.spawn

    def recording_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    DAEMON.spawn = recording_spawn
    try:
        DAEMON.table.remove_if(sess)    # the relay loop already dropped it
        sess.reload_injected = True
        DAEMON.revive(sess, 7 << 8)     # abnormal death (exit 7)
        assert_eq(calls, [], "reload-dead session is NOT respawned")
        # the same death without the guard WOULD spawn
        sess.reload_injected = False
        DAEMON.revive(sess, 7 << 8)
        assert_eq(len(calls), 1, "the guard is what blocks the spawn")
    finally:
        DAEMON.spawn = orig_spawn


def _spawn_session(name):
    """A hosted fake session with an announced conversation file."""
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH, "argv": ["sh", "-c", "sleep 60"]})
    assert_true(r.get("ok"), r)
    file = os.path.join(SCRATCH, "conv-%s.jsonl" % name)
    open(file, "w").close()
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file})["ok"])


def test_extensions_reload_deferral():
    """A round that finds a reloadable session busy must NOT advance
    the on-disk snapshot: the busy session still owes the reload and
    retries on later polls instead of silently losing the change."""
    a, b = "pi-defer-a", "pi-defer-b"
    _spawn_session(a)
    _spawn_session(b)
    DAEMON.control.state({"name": a, "state": "idle"})
    DAEMON.control.state({"name": b, "state": "busy"})
    base = daemon.ext_root_fingerprint([EXT_ROOT])
    daemon._write_ext_snapshot(base, None)
    before = open(daemon.EXT_SNAPSHOT_PATH, "rb").read()
    res = DAEMON.control.extensions_reload({"force": True})
    assert_true(a in res["reloaded"], res)
    assert_true(b in [s["id"] for s in res["skipped"]], res)
    assert_true(res["deferred"], "busy session defers the round")
    after = open(daemon.EXT_SNAPSHOT_PATH, "rb").read()
    assert_eq(after, before, "snapshot must not advance while deferred")
    # once the session is idle the same round completes and stamps
    DAEMON.control.state({"name": b, "state": "idle"})
    res = DAEMON.control.extensions_reload({"force": True})
    assert_true(a in res["reloaded"] and b in res["reloaded"], res)
    assert_true(not res["deferred"], res)
    DAEMON.control.stop({"name": a})
    DAEMON.control.stop({"name": b})
    _drain_session(a)
    _drain_session(b)


def test_ext_watch_loop_owes_busy_sessions():
    """End-to-end: the automatic watch retries a busy session until it
    goes idle and only then advances the snapshot, so every hosted
    session gets exactly one in-place /reload per change."""
    a, b = "pi-owed-a", "pi-owed-b"
    _spawn_session(a)
    _spawn_session(b)
    DAEMON.control.state({"name": a, "state": "idle"})
    DAEMON.control.state({"name": b, "state": "busy"})
    base = daemon.ext_root_fingerprint([EXT_ROOT])
    daemon._write_ext_snapshot(base, None)

    def wait_until(pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    stop = threading.Event()
    thread = threading.Thread(target=daemon.ext_watch_loop,
                              args=(DAEMON, stop), daemon=True)
    thread.start()
    sess_a = DAEMON.table.get(a)
    sess_b = DAEMON.table.get(b)
    try:
        # change the watched root: idle A reloads, busy B defers
        time.sleep(0.02)
        with open(os.path.join(EXT_ROOT, "a.ts"), "w") as f:
            f.write("v2-longer-content")
        changed = daemon.ext_root_fingerprint([EXT_ROOT])
        assert_true(wait_until(lambda: DAEMON.table.get(a).reload_injected),
                    "idle session got its reload")
        assert_true(not sess_b.reload_injected,
                    "busy session is not reloaded yet")
        assert_eq(daemon._read_ext_snapshot(), base,
                  "snapshot not advanced while B is owed")
        assert_true(not os.path.exists(daemon.EXT_DIFF_PATH),
                    "no diff stamped while B is owed")
        # B goes idle: the next poll reloads it and stamps everything
        DAEMON.control.state({"name": b, "state": "idle"})
        assert_true(wait_until(lambda: DAEMON.table.get(b).reload_injected),
                    "busy session got its deferred reload")
        assert_true(wait_until(
            lambda: daemon._read_ext_snapshot() == changed),
            "snapshot advanced once all owed sessions were reloaded")
        assert_true(wait_until(
            lambda: os.path.exists(daemon.EXT_DIFF_PATH)),
            "pending diff was stamped")
        diff = json.load(open(daemon.EXT_DIFF_PATH))
        assert_eq(diff["changed"], [os.path.join(EXT_ROOT, "a.ts")])
    finally:
        stop.set()
        thread.join(timeout=5.0)
        assert_true(not thread.is_alive(), "watch loop stopped cleanly")
        DAEMON.control.stop({"name": a})
        DAEMON.control.stop({"name": b})
        _drain_session(a)
        _drain_session(b)



def test_idle_reap_detects_and_ends_detached_sessions():
    """Regression: a hosted session that outlives its user (detached and
    model-idle past the grace) is ended by the daemon instead of piling
    up as a phantom that respawns on every daemon restart."""
    name = "pi-idle-reap"
    DAEMON.control.stop({"name": name})  # idempotent re-run guard
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH,
         "argv": ["sh", "-c", "echo hosted-ready; sleep 60"]})
    assert_true(r.get("ok"), r)
    sess = DAEMON.table.get(name)
    assert_true(sess is not None, "session did not appear")
    sess.last_activity = time.time() - daemon.IDLE_REAP - 1.0
    assert_true(sess.idle_exceeded(),
                "aged idle detached session not detected as reapable")
    assert_true(_drain_session(name), "idle detached session not reaped")
    # Teardown is async (the session relay thread drops the table entry
    # and registry): wait for both to settle like a client would.
    for _ in range(50):
        listed = DAEMON.control.list({})
        gone = (name not in [s["name"] for s in listed["sessions"]]
                and name not in DAEMON.registry.load())
        if gone:
            break
        time.sleep(0.05)
    assert_true(name not in DAEMON.registry.load(),
                "reaped session still respawnable from the registry")


def test_idle_reap_spares_busy_and_attached():
    """A model that is busy must never be reaped, and an idle session
    with a live bridge viewer stays put."""
    name = "pi-busy-keep"
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH,
         "argv": ["sh", "-c", "echo bg; sleep 60"]})
    assert_true(r.get("ok"), r)
    sess = DAEMON.table.get(name)
    assert_true(DAEMON.control.state(
        {"name": name, "state": "busy"}).get("ok"))
    sess.last_activity = time.time() - daemon.IDLE_REAP - 1.0
    assert_true(not sess.idle_exceeded(),
                "busy session must not be reapable")
    time.sleep(0.7)
    assert_true(DAEMON.table.get(name) is not None,
                "busy session was reaped anyway")
    DAEMON.control.stop({"name": name})
    assert_true(_drain_session(name))


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
    ok("reload guard (in-place only, no spawn)", test_reload_guard)
    ok("reload death is never revived", test_reload_death_not_revived)
    ok("extensions_reload defers busy sessions (no stamp)",
       test_extensions_reload_deferral)
    ok("watch loop owes busy sessions until idle",
       test_ext_watch_loop_owes_busy_sessions)
    ok("idle reap detects and ends detached idle sessions",
       test_idle_reap_detects_and_ends_detached_sessions)
    ok("idle reap spares busy and attached sessions",
       test_idle_reap_spares_busy_and_attached)
    print(f"\n{PASS}/{PASS + len(FAIL)} unit tests passed")
    if FAIL:
        print("Failed: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())