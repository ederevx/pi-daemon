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
import select
import signal
import socket
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
# Reload injection falls back to typing instantly in tests; the signal
# path tests below re-enable the wait around their own calls.
os.environ["PI_PTYD_RELOAD_SIGNAL_GRACE"] = "0"
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


def ok(name, fn, posix_only=False, windows_only=False):
    global PASS
    if posix_only and os.name == "nt":
        print("  skip " + name + " (POSIX only)")
        return
    if windows_only and os.name != "nt":
        print("  skip " + name + " (Windows only)")
        return
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


def test_owned_selection():
    base = os.path.join(SCRATCH, "owned")
    other = os.path.join(SCRATCH, "owned-other")
    os.makedirs(base, exist_ok=True)
    os.makedirs(other, exist_ok=True)

    def touch(name, mtime):
        path = os.path.join(base, name)
        with open(path, "w") as f:
            f.write("x")
        os.utime(path, (mtime, mtime))
        return path

    old = touch("old.jsonl", 1000)
    new = touch("new.jsonl", 2000)
    sessions = [
        {"name": "pi-a", "dir": base, "file": old, "state": "busy"},
        {"name": "pi-b", "dir": base, "file": new, "state": "idle"},
        {"name": "pi-c", "dir": other, "file": new, "state": "busy"},
        {"name": "pi-d", "dir": base, "file": "-", "state": "busy"},
        {"name": "pi-e", "dir": base,
         "file": os.path.join(base, "missing.jsonl"), "state": "busy"},
    ]
    picked = pi_rc.pick_owned_session(sessions, base)
    assert_true(picked is not None, "a candidate was picked")
    assert_eq(picked["name"], "pi-b", "newest same-dir real file wins")
    tie = touch("tie.jsonl", 3000)
    tied = [
        {"name": "pi-x", "dir": base, "file": tie, "state": "idle"},
        {"name": "pi-y", "dir": base, "file": tie, "state": "busy"},
    ]
    assert_eq(pi_rc.pick_owned_session(tied, base)["name"], "pi-y",
              "busy wins an mtime tie")
    assert_true(pi_rc.pick_owned_session(
        [{"name": "pi-c", "dir": other, "file": new, "state": "busy"}],
        base) is None, "no same-dir candidate")


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
        t3 = store.alloc_id_locked("host-5e9a")
    assert_eq(t1, "t-1")
    assert_eq(t2, "t-2")
    # A session segment rides the id; the counter still makes it unique.
    assert_eq(t3, "t-5e9a-3")
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
    # Snapshot is oldest-first even though ids now carry a session segment
    # and are not numerically sortable across sessions.
    store.add({"id": "t-zzz-9", "kind": "shell", "status": "done",
               "command": "z", "cwd": "/x", "session": "order",
               "created": 20.0})
    store.add({"id": "t-aaa-10", "kind": "shell", "status": "done",
               "command": "a", "cwd": "/x", "session": "order",
               "created": 10.0})
    assert_eq([r["id"] for r in store.snapshot("order", "shell")],
              ["t-aaa-10", "t-zzz-9"])
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
    # The child owns the status encoding: the POSIX seam refines a
    # waitpid status (signal or nonzero exit); a clean quit is final.
    child = daemon.pi_platform.PosixPtyChild(123, -1)
    assert_true(not child.exit_abnormal(0))
    assert_true(child.exit_abnormal((7 << 8) & 0xFFFF))
    proc = subprocess.Popen(["sh", "-c", "kill -TERM $$"])
    _, status = os.waitpid(proc.pid, 0)
    assert_true(os.WIFSIGNALED(status))
    assert_true(child.exit_abnormal(status))


def test_tty_size_fallback():
    # Windows fd 0 is the console input handle, which cannot report a
    # size; tty_size must fall through to stdout/stderr before the default.
    real = pi_rc.os.get_terminal_size
    seen = []

    def fake(fd):
        seen.append(fd)
        if fd == 0:
            raise OSError("fd 0 is not a screen buffer")
        return os.terminal_size((123, 45))

    pi_rc.os.get_terminal_size = fake
    try:
        assert_eq(pi_rc.tty_size(0), (123, 45))
        assert_eq(seen[0], 0)
        assert_true(1 in seen, "did not fall through to stdout")
    finally:
        pi_rc.os.get_terminal_size = real


def test_exit_seam():
    # Every backend exposes exit_abnormal; the base default (nonzero) is
    # what the Windows backend uses, and the daemon must not classify
    # status itself.
    base = daemon.pi_platform.PtyChild()
    assert_true(not base.exit_abnormal(0))
    assert_true(base.exit_abnormal(1))
    assert_true(not hasattr(daemon, "_exit_abnormal"),
                "exit classification belongs to the child seam")


class _RecordingChild(daemon.pi_platform.PtyChild):
    """Stand-in PTY child: records the resize/repaint calls it receives."""

    def __init__(self):
        self.sizes = []
        self.winches = 0

    def set_winsize(self, cols, rows):
        self.sizes.append((cols, rows))

    def signal_winch(self):
        self.winches += 1


def test_resize_delegates():
    # Session.resize applies the size and asks the child to repaint; the
    # platform child owns how that repaint is delivered.
    child = _RecordingChild()
    sess = daemon.Session("pi-resize", SCRATCH, ["pi"], 123, -1, child=child)
    sess.resize(100, 30)
    assert_eq(child.sizes, [(100, 30)])
    assert_eq(child.winches, 1)


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
    assert_eq(r2["id"], "t-s1-1")
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
    # the reload's own session_start re-announces the SAME file: that
    # must NOT clear the no-spawn guard (pi reloads the extension module
    # and re-announces mid-reload; clearing there would let a broken
    # reload crash revive into a fresh spawn).
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file})["ok"])
    assert_true(sess.reload_injected,
                "same-file re-announce must not clear the reload guard")
    assert_true(sess.reload_injected_at > 0,
                "guard carries its arm timestamp")
    # a genuinely different conversation file clears the guard
    other = os.path.join(SCRATCH, "conv-reload-other.jsonl")
    open(other, "w").close()
    assert_true(DAEMON.control.announce(
        {"name": name, "file": other})["ok"])
    assert_true(not sess.reload_injected,
                "changed conversation file clears the reload guard")
    DAEMON.control.stop({"name": name})
    assert_true(_drain_session(name))


def test_reload_signal_consumed_direct():
    """The daemon signals the in-session extension directly: it writes
    a one-shot token-stamped signal file and, once the extension
    consumes it (deletes it), counts the reload as done without ever
    typing into the PTY."""
    name = "pi-reloadsig"
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH, "argv": ["sh", "-c", "sleep 30"]})
    assert_true(r.get("ok"), r)
    file = os.path.join(SCRATCH, "conv-reloadsig.jsonl")
    open(file, "w").close()
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file})["ok"])
    DAEMON.control.state({"name": name, "state": "idle"})
    sig = os.path.join(daemon.EXT_RELOAD_SIGNAL_DIR, name + ".json")
    seen = {}

    def consume():
        for _ in range(400):
            if os.path.exists(sig):
                with open(sig) as f:
                    seen["rec"] = json.load(f)
                os.unlink(sig)
                return
            time.sleep(0.01)

    orig = daemon.RELOAD_SIGNAL_GRACE
    daemon.RELOAD_SIGNAL_GRACE = 5.0
    t = threading.Thread(target=consume)
    t.start()
    try:
        res = DAEMON.control.extensions_reload({"force": True})
    finally:
        daemon.RELOAD_SIGNAL_GRACE = orig
        t.join(3)
    assert_true(name in res["reloaded"], res)
    assert_true("token" in seen.get("rec", {}),
                "signal carries a fresh round token")
    assert_true(not os.path.exists(sig), "signal consumed exactly once")
    sess = DAEMON.table.get(name)
    assert_true(sess is not None and sess.reload_injected,
                "signal path arms the no-spawn guard")
    DAEMON.control.stop({"name": name})
    assert_true(_drain_session(name))


def test_reload_signal_fallback_types():
    """An extension without the watcher never consumes the signal: the
    daemon deletes the stale file and falls back to typing /reload
    into the PTY, arming the no-spawn guard as before."""
    name = "pi-reloadfall"
    r = DAEMON.control.start(
        {"name": name, "dir": SCRATCH, "argv": ["sh", "-c", "sleep 30"]})
    assert_true(r.get("ok"), r)
    file = os.path.join(SCRATCH, "conv-reloadfall.jsonl")
    open(file, "w").close()
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file})["ok"])
    DAEMON.control.state({"name": name, "state": "idle"})
    sig = os.path.join(daemon.EXT_RELOAD_SIGNAL_DIR, name + ".json")
    res = DAEMON.control.extensions_reload({"force": True})
    assert_true(name in res["reloaded"], res)
    assert_true(not os.path.exists(sig), "stale signal cleared before typing")
    sess = DAEMON.table.get(name)
    assert_true(sess is not None and sess.reload_injected,
                "fallback keeps the no-spawn guard armed")
    DAEMON.control.stop({"name": name})
    assert_true(_drain_session(name))


def test_reload_signal_unsafe_name_falls_back():
    """A session name that cannot be a single file component never
    becomes a signal path: the daemon falls back to typing, and no
    signal file ever escapes the signal directory."""
    name = "pi-../traverse"
    sess = daemon.Session(name, SCRATCH, ["pi"], pid=1, master_fd=-1)
    sess.file = os.path.join(SCRATCH, "conv-traverse.jsonl")
    assert_true(DAEMON.table.put(sess))
    DAEMON.control.state({"name": name, "state": "idle"})
    try:
        res = DAEMON.control.extensions_reload({"sessions": [name],
                                                "force": True})
        assert_true(any(e.get("id") == name
                        and e.get("reason") == "write-failed"
                        for e in res["skipped"]), res)
        assert_true(name not in res["reloaded"], res)
        assert_eq(os.listdir(daemon.EXT_RELOAD_SIGNAL_DIR), [],
                  "no signal file outside the directory")
    finally:
        DAEMON.table.remove_if(sess)


def test_reload_signal_startup_cleanup():
    """Signals left behind by a previous daemon run are dropped at
    startup so a fresh session with the same name never consumes a
    stale round token."""
    sigdir = daemon.EXT_RELOAD_SIGNAL_DIR
    os.makedirs(sigdir, exist_ok=True)
    stale = os.path.join(sigdir, "pi-gone.json")
    open(stale, "w").close()
    daemon._clear_stale_reload_signals()
    assert_true(not os.path.exists(stale))


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
        # armed within the reload survive grace: death is NOT revived
        sess.reload_injected = True
        sess.reload_injected_at = time.time()
        DAEMON.revive(sess, 7 << 8)     # abnormal death (exit 7)
        assert_eq(calls, [], "reload-dead session is NOT respawned")
        # the same death without the guard WOULD spawn
        sess.reload_injected = False
        DAEMON.revive(sess, 7 << 8)
        assert_eq(len(calls), 1, "the guard is what blocks the spawn")
        # a session that survived past the reload guard grace is
        # revivable again: an armed flag without a live arm window must
        # not suppress a genuinely post-reload crash.
        sess.reload_injected = True
        sess.reload_injected_at = time.time() - \
            daemon.RELOAD_GUARD_GRACE - 1
        DAEMON.revive(sess, 7 << 8)
        assert_eq(len(calls), 2, "survivor past the grace is revived")
    finally:
        DAEMON.spawn = orig_spawn


def test_reload_same_file_announce_no_spawn():
    """Regression: the reload's own session_start re-announces the SAME
    conversation file (pi reloads the extension module mid-reload). That
    re-announce must not clear the reload guard, or a reloaded session
    that then dies abnormally would be revived into a fresh spawn - the
    reload must stay strictly in-place and never spawn anything."""
    name = "pi-samefile"
    file_ = os.path.join(SCRATCH, "conv-samefile.jsonl")
    open(file_, "w").close()
    sess = daemon.Session(name, SCRATCH, ["pi"], pid=1, master_fd=-1)
    sess.file = file_
    sess.spawned_at = 0.0
    assert_true(DAEMON.table.put(sess))
    DAEMON.control.state({"name": name, "state": "idle"})
    sess.reload_injected = True
    sess.reload_injected_at = time.time()
    # the reload's own session_start -> extension re-announces same file
    assert_true(DAEMON.control.announce(
        {"name": name, "file": file_})["ok"])
    assert_true(sess.reload_injected,
                "same-file re-announce keeps the reload guard armed")
    calls = []
    orig_spawn = DAEMON.spawn

    def recording_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    DAEMON.spawn = recording_spawn
    try:
        DAEMON.table.remove_if(sess)
        DAEMON.revive(sess, 7 << 8)
        assert_eq(calls, [], "reloaded session dying after a same-file "
                             "re-announce is NOT respawned")
    finally:
        DAEMON.spawn = orig_spawn
        DAEMON.table.remove_if(sess)


def test_stop_and_reap_discard_conversation_file():
    """A session the daemon ends itself (stop, or the idle-reap of a
    stale detached phantom) drops its conversation file too, so it stops
    showing up in pi's /resume picker. A clean user quit (stopping is
    False) keeps the file, and an absorbed duplicate whose conversation
    still has a live holder keeps it as well (the file passes to its
    survivor)."""

    def mk(name, file_, stopping):
        sess = daemon.Session(name, SCRATCH, ["pi"], pid=1, master_fd=-1)
        sess.file = file_
        sess.stopping = stopping
        assert_true(DAEMON.table.put(sess))
        return sess

    # stop/reap of the sole holder discards the file (/resume cleaned)
    sole_file = os.path.join(SCRATCH, "discard-sole.jsonl")
    open(sole_file, "w").close()
    sole = mk("pi-discard-sole", sole_file, True)
    DAEMON.drop(sole, 0)
    assert_true(not os.path.exists(sole_file),
                "daemon-ended session file removed from /resume")
    assert_true(DAEMON.table.get("pi-discard-sole") is None)

    # a clean user quit keeps the conversation resumable
    quit_file = os.path.join(SCRATCH, "quit.jsonl")
    open(quit_file, "w").close()
    quit_sess = mk("pi-quit", quit_file, False)
    DAEMON.drop(quit_sess, 0)
    assert_true(os.path.exists(quit_file),
                "clean quit keeps the conversation file")

    # an absorbed duplicate shares its file with a live holder: dropping
    # it must NOT unlink the conversation the survivor still hosts
    shared = os.path.join(SCRATCH, "shared.jsonl")
    open(shared, "w").close()
    live = mk("pi-live", shared, False)
    dup = mk("pi-dup", shared, True)   # absorbed holder is marked stopping
    DAEMON.drop(dup, 0)
    assert_true(os.path.exists(shared),
                "absorbed duplicate's shared conversation is not deleted")
    assert_true(DAEMON.table.get("pi-live") is not None)
    # when the surviving holder is itself later ended by the daemon, the
    # now-orphaned conversation is finally discarded
    live.stopping = True
    DAEMON.drop(live, 0)
    assert_true(not os.path.exists(shared),
                "last holder drop discards the conversation")


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


# --- restart handover (registry dedupe, roster, idle watch) -----------------

def test_registry_dedupe():
    """One registry entry per conversation file: duplicates of the same
    --session conversation are dropped (never respawned side by side),
    the attached duplicate wins when a prefer callback says so, and
    blank-start argv entries are never treated as duplicates."""
    assert_eq(daemon.argv_session_file(["pi", "--session", "x"]), "x")
    assert_eq(daemon.argv_session_file(["pi"]), None)

    def write(path, a_extra=None):
        entries = {
            "pi-a": {"dir": SCRATCH,
                     "argv": ["pi", "--session", "conv-a.jsonl"]},
            "pi-b": {"dir": SCRATCH,
                     "argv": ["pi", "--session", "conv-a.jsonl"]},
            "pi-c": {"dir": SCRATCH,
                     "argv": ["pi", "--session", "conv-c.jsonl"]},
            "pi-d": {"dir": SCRATCH, "argv": ["pi"]},
        }
        reg = daemon.Registry(path)
        reg.write(entries)
        return reg

    reg = write(os.path.join(SCRATCH, "dedupe1.json"))
    keep, drop = reg.dedupe(daemon.argv_session_file)
    assert_eq(drop, ["pi-b"], drop)
    assert_eq(keep, {"conv-a.jsonl": "pi-a", "conv-c.jsonl": "pi-c"})
    data = reg.load()
    assert_true("pi-b" not in data and "pi-a" in data)
    assert_true("pi-d" in data, "blank-start entries stay untouched")

    reg = write(os.path.join(SCRATCH, "dedupe2.json"))
    keep, drop = reg.dedupe(daemon.argv_session_file,
                            prefer=lambda n: n == "pi-b")
    assert_eq(drop, ["pi-a"], drop)
    assert_eq(keep["conv-a.jsonl"], "pi-b")


def test_restart_planner_prefers_attached():
    """RestartPlanner.dedupe_registry keeps the duplicate whose live
    session has a bridge viewer (the one a user is attached to)."""
    path = os.path.join(SCRATCH, "dedupe3.json")
    reg = daemon.Registry(path)
    reg.write({
        "pi-attach": {"dir": SCRATCH,
                      "argv": ["pi", "--session", "conv-att.jsonl"]},
        "pi-detach": {"dir": SCRATCH,
                      "argv": ["pi", "--session", "conv-att.jsonl"]},
    })
    a = daemon.Session("pi-attach", SCRATCH, ["pi"],
                       pid=os.getpid(), master_fd=0)
    d = daemon.Session("pi-detach", SCRATCH, ["pi"],
                       pid=os.getpid(), master_fd=0)
    assert_true(DAEMON.table.put(a, refuse_if_exists=True))
    assert_true(DAEMON.table.put(d, refuse_if_exists=True))
    pair = socket.socketpair()
    a.install_client(pair[0])
    try:
        planner = daemon.RestartPlanner(reg, DAEMON.table)
        keep, drop = planner.dedupe_registry()
        assert_eq(drop, ["pi-detach"], drop)
        assert_eq(keep["conv-att.jsonl"], "pi-attach")
    finally:
        pair[0].close()
        pair[1].close()
        DAEMON.table.remove_if(a)
        DAEMON.table.remove_if(d)


def test_daemon_roster():
    """The roster drives the single-daemon guarantee: older entries are
    shut down gracefully through their own coordinates, dead entries
    are pruned, and younger entries are never touched."""
    path = os.path.join(SCRATCH, "roster.json")
    roster = daemon.DaemonRoster(path)
    roster.register(202, "127.0.0.1", 1, "tok")
    roster.register(303, "127.0.0.1", 1, "tok")
    roster.register(404, "127.0.0.1", 1, "tok")
    # rewrite stamps so ordering is deterministic: 202 (fake live peer)
    # and 303 (dead port) are older than the caller, 404 is younger.
    with open(path) as f:
        data = json.load(f)
    data["202"]["started"] = 100.0
    data["303"]["started"] = 150.0
    data["404"]["started"] = 600.0
    with open(path, "w") as f:
        json.dump(data, f)

    got = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(4)

    def fake_peer():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                f = conn.makefile("rwb")
                got.append(json.loads(f.readline()).get("cmd"))
                f.write(json.dumps({"ok": True}).encode() + b"\n")
                f.flush()
                got.append(json.loads(f.readline()).get("cmd"))
                f.write(json.dumps({"ok": True}).encode() + b"\n")
                f.flush()
            except Exception:
                return
            finally:
                conn.close()

    t = threading.Thread(target=fake_peer, daemon=True)
    t.start()
    data["202"]["port"] = port
    with open(path, "w") as f:
        json.dump(data, f)
    try:
        stopped, pruned = roster.shutdown_others(999, 200.0)
        assert_eq(stopped, 1, got)
        assert_eq(pruned, 1, "refused port is a dead entry")
        assert_true(got[:2] == ["hello", "shutdown"], got)
        data = roster._load()
        assert_true("404" in data, "younger entry is never touched")
        assert_true("202" not in data and "303" not in data)
    finally:
        srv.close()
    roster.remove(404)
    assert_eq(roster._load(), {})


def _spawn_fake_daemon(linger):
    """Spawn a subprocess that speaks the daemon control handshake
    (hello, shutdown - both acked) on an ephemeral port, then lingers
    for `linger` seconds before exiting (unless killed first)."""
    code = (
        "import json, socket, sys, threading, time\n"
        "srv = socket.socket()\n"
        "srv.bind(('127.0.0.1', 0))\n"
        "srv.listen(4)\n"
        "print(srv.getsockname()[1], flush=True)\n"
        "def serve():\n"
        "    while True:\n"
        "        try:\n"
        "            conn, _ = srv.accept()\n"
        "        except OSError:\n"
        "            return\n"
        "        try:\n"
        "            f = conn.makefile('rwb')\n"
        "            json.loads(f.readline())\n"
        "            f.write(b'{\"ok\": true}\\n'); f.flush()\n"
        "            json.loads(f.readline())\n"
        "            f.write(b'{\"ok\": true}\\n'); f.flush()\n"
        "        except Exception:\n"
        "            pass\n"
        "        finally:\n"
        "            conn.close()\n"
        "threading.Thread(target=serve, daemon=True).start()\n"
        "time.sleep(%f)\n" % linger
    )
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline().strip())


def _roster_with_peer(pid, port, tag):
    path = os.path.join(SCRATCH, "roster-%s.json" % tag)
    roster = daemon.DaemonRoster(path)
    roster.register(pid, "127.0.0.1", port, "tok")
    with open(path) as f:
        data = json.load(f)
    data[str(pid)]["started"] = 100.0
    with open(path, "w") as f:
        json.dump(data, f)
    return roster


def test_roster_waits_for_peer_death():
    """A peer that acks the graceful shutdown is awaited until its
    process is actually gone: its control port can die before its exit
    teardown runs, and that teardown removes the shared endpoint file
    the boot is about to republish."""
    proc, port = _spawn_fake_daemon(linger=0.4)
    roster = _roster_with_peer(proc.pid, port, "wait")
    try:
        t0 = time.time()
        stopped, pruned = roster.shutdown_others(999, 200.0, 5.0)
        elapsed = time.time() - t0
        assert_eq(stopped, 1)
        assert_eq(pruned, 0)
        assert_true(elapsed >= 0.4,
                    "shutdown_others returned before the peer died "
                    "(%.2fs)" % elapsed)
        assert_true(roster._load() == {}, "stopped entry is pruned")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_roster_force_kills_lingering_peer():
    """A peer that acks the shutdown but never exits is killed by its
    roster pid, so a new daemon always proceeds to a single-daemon
    state instead of sharing the host with a zombie."""
    proc, port = _spawn_fake_daemon(linger=30.0)
    roster = _roster_with_peer(proc.pid, port, "kill")
    try:
        # A wait budget shorter than the linger guarantees the
        # force-kill branch runs.
        stopped, pruned = roster.shutdown_others(999, 200.0, 0.2)
        assert_eq(stopped, 1)
        assert_eq(pruned, 0)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("lingering peer survived the force kill")
        assert_true(roster._load() == {}, "killed entry is pruned")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_endpoint_remove_if_owner_scoped():
    """EndpointFile.remove_if unlinks only a file that still publishes
    the caller's own coordinates: a dying daemon must never delete the
    endpoint a successor published over it."""
    ep = daemon.pi_platform.EndpointFile(
        os.path.join(SCRATCH, "endpoint.json"))
    ep.write("127.0.0.1", 40000, "mine")
    ep.remove_if("127.0.0.1", 40000, "theirs")
    assert_true(ep.read().get("token") == "mine",
                "foreign coordinates never unlink the file")
    ep.remove_if("127.0.0.1", 40000, "mine")
    assert_eq(ep.read(), {}, "own coordinates unlink the file")
    ep.remove_if("127.0.0.1", 40000, "mine")
    assert_eq(ep.read(), {}, "removing a missing file is quiet")


def test_idle_shutdown_watch():
    """The sweep stays quiet while any activity signal is live: a busy
    or waiting session, an attached viewer, a running ticket, or a
    parked handover connection; a fully detached idle daemon is
    quiescent."""
    assert_eq(daemon.DAEMON_IDLE_TIMEOUT, 300.0)
    watch = daemon.IdleShutdownWatch(DAEMON, DAEMON.shutdown_event)
    assert_true(watch.quiescent())
    sess = daemon.Session("pi-idlewatch", SCRATCH, ["pi"],
                          pid=os.getpid(), master_fd=0)
    assert_true(DAEMON.table.put(sess, refuse_if_exists=True))
    pair = socket.socketpair()
    try:
        assert_true(watch.quiescent())
        sess.state = "busy"
        assert_true(not watch.quiescent())
        sess.state = "waiting"
        assert_true(not watch.quiescent(),
                    "parked on a daemon wait counts as active")
        sess.state = "idle"
        sess.install_client(pair[0])
        assert_true(not watch.quiescent(), "attached viewer is activity")
        sess.take_clients()
        assert_true(watch.quiescent())
        with DAEMON.handover_lock:
            DAEMON.handover_wait[object()] = ("t", "d", "f", None)
        assert_true(not watch.quiescent(), "parked handover is activity")
        with DAEMON.handover_lock:
            DAEMON.handover_wait.clear()
        r = DAEMON.control.ticket_submit(
            {"session": "s1", "cwd": SCRATCH, "command": "sleep 30"})
        assert_true(not watch.quiescent(), "running ticket is activity")
        DAEMON.control.ticket_cancel({"id": r["id"]})
        assert_true(watch.quiescent())
    finally:
        pair[0].close()
        pair[1].close()
        DAEMON.table.remove_if(sess)
        with DAEMON.handover_lock:
            DAEMON.handover_wait.clear()


# --- platform seams -------------------------------------------------------

def test_platform_seams():
    plat = daemon.pi_platform
    # POSIX layout honors XDG; Windows layout uses LOCALAPPDATA/TEMP.
    posix = plat.RuntimeLayout(environ={"XDG_RUNTIME_DIR": "/run/x",
                                        "XDG_STATE_HOME": "/state/x"},
                               platform="linux")
    assert_eq(posix.endpoint_path(), "/run/x/pi-pty-host.sock")
    assert_eq(posix.registry_path(), "/state/x/pi-pty-host/sessions.json")
    win = plat.RuntimeLayout(
        environ={"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local",
                 "TEMP": "C:\\Temp"}, platform="win32")
    assert_true(win.endpoint_path().endswith("pi-pty-host.sock"))
    assert_true("pi-daemon" in win.endpoint_path())
    assert_true("pi-pty-host" in win.registry_path())
    # The agent home honors PI_CODING_AGENT_DIR, else ~/.pi/agent.
    agent = plat.RuntimeLayout(environ={"PI_CODING_AGENT_DIR": "/agent/x"},
                               platform="linux")
    assert_eq(agent.agent_home(), "/agent/x")
    # Shell launchers live in the seam, not in callers, and resolve bash
    # the same way on every platform, so an offloaded command runs in the
    # shell pi would have used locally.
    lin = plat.ProcessControl(platform="linux")
    assert_eq(lin.resume_command()[-2:], ["-c", "pi -c || exec pi"])
    assert_eq(lin.shell_command("echo hi")[-2:], ["-c", "echo hi"])
    assert_eq(lin.group_kwargs(), {"start_new_session": True})
    winp = plat.ProcessControl(platform="win32")
    assert_eq(winp.resume_command()[-2:], ["-c", "pi -c || exec pi"])
    assert_eq(winp.shell_command("echo hi")[-2:], ["-c", "echo hi"])
    assert_true("creationflags" in winp.group_kwargs())
    pinned = plat.ProcessControl(platform="win32",
                                 environ={"PI_SHELL": sys.executable})
    assert_eq(pinned.shell_command("echo hi"),
              [sys.executable, "-c", "echo hi"])
    # A launched `pi` is wrapped so the shell resolves the shim (Windows
    # cannot CreateProcess a bare `pi`) and `"$@"` keeps paths verbatim.
    assert_eq(pinned.pi_argv(["--session", "C:\\x"])[-2:],
              ["--session", "C:\\x"])
    assert_eq(pinned.pi_argv()[2], 'exec pi "$@"')
    assert_eq(pinned.pi_argv()[3], "pi")
    # The token handshake is the transport's only authentication.
    hs = plat.ControlHandshake("tok")
    assert_true(hs.validate({"cmd": "hello", "token": "tok"}))
    assert_true(not hs.validate({"cmd": "hello", "token": "bad"}))
    assert_true(not hs.validate({"cmd": "list"}))
    assert_eq(hs.ack().get("ok"), True)
    assert_eq(hs.reject().get("error"), "bad-handshake")


def test_shell_path_skips_wsl_stub():
    plat = daemon.pi_platform
    root = os.environ.get("SystemRoot", "C:\\WINDOWS")
    stub = os.path.join(root, "System32", "bash.exe")
    # Windows ships bash.exe in the system directory as the WSL launcher;
    # it execs into the default Linux distro, so a session started
    # through it dies with 127 (the Windows pi chain is not reachable
    # there). It is the only bash a native-Windows PATH resolves, so the
    # seam rejects it everywhere, explicit override included, and the
    # next candidate (Git bash) wins.
    win = plat.ProcessControl(platform="win32",
                              environ={"SystemRoot": root,
                                       "PI_SHELL": stub})
    resolved = win.shell_path()
    assert_true(resolved)
    assert_true(os.path.normcase(os.path.abspath(resolved))
                != os.path.normcase(os.path.abspath(stub)))
    assert_eq(win.pi_argv()[0], resolved)
    assert_eq(win.resume_command()[0], resolved)
    # The predicate is a pure path judgement.
    assert_eq(win._is_wsl_stub(stub), True)
    assert_eq(win._is_wsl_stub(
        os.path.join(root, "Sysnative", "bash.exe")), True)
    assert_eq(win._is_wsl_stub(r"C:\Program Files\Git\bin\bash.exe"),
              False)
    # Without the system dir there is nothing to reject, so an explicit
    # path is honored verbatim.
    bare = plat.ProcessControl(platform="win32",
                               environ={"PI_SHELL": stub})
    assert_eq(bare._is_wsl_stub(stub), False)
    assert_eq(bare.shell_path(), stub)


class _RecordingControl:
    """Stand-in ProcessControl: records SIGWINCH deliveries."""

    def __init__(self):
        self.winches = []

    def signal_winch(self, pid):
        self.winches.append(pid)


def test_posix_pty_child():
    # POSIX-only: PosixPtyChild reaps through os.waitpid/os.WNOHANG.
    plat = daemon.pi_platform
    child = plat.PosixPtyChild(123, -1)
    assert_eq(child.pid, 123)
    assert_eq(child.output_handle(), -1)
    # 123 is not our child: ChildProcessError means "nothing to reap".
    assert_eq(child.wait_nohang(), (True, 0))

    # Repaint nudge: pi redraws only on a real winsize change, so a
    # resize at the current size must toggle one row and restore it,
    # signaling at both ends, exactly like the Windows child.
    ctrl = _RecordingControl()
    child = plat.PosixPtyChild(123, -1, control=ctrl)
    sizes = []
    child.get_winsize = lambda: (24, 80)
    child.set_winsize = lambda cols, rows: sizes.append((cols, rows))
    child.signal_winch()
    assert_eq(sizes, [(80, 23), (80, 24)])
    assert_eq(ctrl.winches, [123, 123])

    # An unreadable winsize cannot be toggled: signal once and stop.
    ctrl.winches = []
    child.get_winsize = lambda: (0, 0)
    child.signal_winch()
    assert_eq(sizes, [(80, 23), (80, 24)])
    assert_eq(ctrl.winches, [123])


class _RecordingClient:
    """Stand-in control client: records resize requests, never dials."""

    def __init__(self):
        self.requests = []

    def control(self, req, timeout=None):
        self.requests.append(req)
        return {"ok": True}


def _relay_until(fd_read, want, deadline):
    got = b""
    while want not in got and time.time() < deadline:
        ready, _w, _e = select.select([fd_read], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(fd_read, 65536)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    assert_true(want in got, "relay missing %r (got %r)" % (want, got))


def test_terminal_seam():
    plat = daemon.pi_platform
    assert_true(isinstance(plat.select_terminal_mode("linux"),
                           plat.PosixTerminal))
    master, slave = os.openpty()
    term = plat.PosixTerminal(fd=slave, out_fd=slave)
    try:
        term.enter()
        os.write(master, b"seam-in")
        _relay_until(term.input_fd(), b"seam-in", time.time() + 3)
        term.write_output(b"seam-out")
        _relay_until(master, b"seam-out", time.time() + 3)
        os.kill(os.getpid(), signal.SIGWINCH)
        resized = False
        deadline = time.time() + 3
        while time.time() < deadline:
            if term.take_resize():
                resized = True
                break
            time.sleep(0.02)
        assert_true(resized, "SIGWINCH did not set the resize flag")
    finally:
        term.restore()
        os.close(master)
        os.close(slave)


def test_bridge_relay():
    # The bridge installs signal handlers, so it must run on the main
    # thread; a driver thread feeds both directions and then detaches.
    import fcntl
    import struct
    import termios
    plat = daemon.pi_platform
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0))
    daemon_side, bridge_side = socket.socketpair()
    client = _RecordingClient()
    bridge = pi_rc.Bridge(client, bridge_side, "pi-relay",
                          initial=b"",
                          terminal=plat.PosixTerminal(fd=slave, out_fd=slave))
    errors = []

    def drive():
        try:
            daemon_side.sendall(b"daemon-hello")
            _relay_until(master, b"daemon-hello", time.time() + 3)
            os.write(master, b"typed-hello")
            _relay_until(daemon_side.fileno(), b"typed-hello",
                         time.time() + 3)
            os.kill(os.getpid(), signal.SIGWINCH)
            deadline = time.time() + 2
            while time.time() < deadline and not any(
                    req.get("cmd") == "resize" for req in client.requests):
                time.sleep(0.02)
            os.write(master, pi_rc.DETACH_KEY)
        except Exception as exc:  # surfaced after the bridge returns
            errors.append(exc)

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    try:
        status = bridge.run()
        driver.join(timeout=3)
        assert_eq(errors, [])
        assert_eq(status, "detached")
        assert_true(any(req.get("cmd") == "resize"
                        for req in client.requests), "resize not relayed")
    finally:
        driver.join(timeout=1)
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass
        for sock in (daemon_side, bridge_side):
            try:
                sock.close()
            except OSError:
                pass


def _conpty():
    sys.path.insert(0, os.path.join(REPO, "pi", "lib"))
    import importlib
    return importlib.import_module("pi_conpty")


def test_terminal_contract():
    # Both terminal implementations must satisfy every method the bridge
    # drives. WindowsConsole cannot inherit TerminalMode across the
    # pi_conpty boundary, so duck-typing is the only guard; a missing
    # wake_fd/writable_fd crashed Windows attach once already.
    conpty = _conpty()
    plat = daemon.pi_platform
    required = ("enter", "restore", "size", "input_fd", "wake_fd",
                "writable_fd", "read_input", "write_output", "take_resize")
    for mode in (plat.PosixTerminal(), conpty.WindowsConsole()):
        for name in required:
            assert_true(callable(getattr(mode, name, None)),
                        "%s missing %s" % (type(mode).__name__, name))


def test_utf8_chunker():
    # A multi-byte character split across relay chunks must be held back
    # rather than decoded twice by WriteFile on the UTF-8 console.
    chunker = _conpty()._Utf8Chunker()
    assert_eq(chunker.feed(b"ab"), b"ab")
    assert_eq(chunker.feed(b"\xc3"), b"")
    assert_eq(chunker.feed(b"\xa9cd"), b"\xc3\xa9cd")
    assert_eq(chunker.feed(b"x\xe2\x82"), b"x")
    assert_eq(chunker.feed(b"\xac!"), b"\xe2\x82\xac!")
    assert_eq(chunker.feed(b"\xff"), b"\xff")


class _BoomTerminal:
    """Terminal whose enter() fails after the seam is opened."""

    def __init__(self):
        self.restored = False

    def enter(self):
        raise OSError("boom")

    def restore(self):
        self.restored = True


def test_bridge_enter_failure_restores():
    term = _BoomTerminal()
    daemon_side, bridge_side = socket.socketpair()
    bridge = pi_rc.Bridge(None, bridge_side, "pi-x", terminal=term)
    raised = False
    try:
        bridge.run()
    except OSError:
        raised = True
    finally:
        daemon_side.close()
    assert_true(raised, "enter failure did not propagate")
    assert_true(term.restored, "restore not called after enter failed")


def main():
    try:
        return _main()
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def _main():
    ok("pi-rc name helpers", test_names)
    ok("owned-session selection (native dir, newest, busy tie)",
       test_owned_selection)
    ok("registry round-trip", test_registry)
    ok("ticket store lifecycle", test_ticket_store)
    ok("extension fingerprint/diff", test_ext_fingerprint)
    ok("exit classification", test_exit_classification, posix_only=True)
    ok("exit seam (base + daemon delegation)", test_exit_seam)
    ok("tty size falls through to stdout", test_tty_size_fallback)
    ok("session resize delegates to the child", test_resize_delegates)
    ok("ticket control (submit/wait/output/list/remove)", test_ticket_control)
    ok("ticket cancel + reset", test_ticket_cancel_and_reset)
    ok("session control (start/list/state/input/detach/stop)", test_session_control)
    ok("reload guard (in-place only, no spawn)", test_reload_guard)
    ok("reload signal consumed directly (no PTY typing)",
       test_reload_signal_consumed_direct)
    ok("reload signal falls back to PTY typing",
       test_reload_signal_fallback_types)
    ok("reload signal refuses unsafe names",
       test_reload_signal_unsafe_name_falls_back)
    ok("reload signal startup cleanup",
       test_reload_signal_startup_cleanup)
    ok("reload death is never revived", test_reload_death_not_revived,
       posix_only=True)
    ok("reload same-file re-announce keeps no-spawn guard",
       test_reload_same_file_announce_no_spawn)
    ok("stop/reap discards the conversation file (/resume)",
       test_stop_and_reap_discard_conversation_file)
    ok("extensions_reload defers busy sessions (no stamp)",
       test_extensions_reload_deferral)
    ok("watch loop owes busy sessions until idle",
       test_ext_watch_loop_owes_busy_sessions)
    ok("idle reap detects and ends detached idle sessions",
       test_idle_reap_detects_and_ends_detached_sessions)
    ok("idle reap spares busy and attached sessions",
       test_idle_reap_spares_busy_and_attached)
    ok("registry dedupe (one entry per conversation)", test_registry_dedupe)
    ok("restart planner prefers the attached duplicate",
       test_restart_planner_prefers_attached)
    ok("daemon roster (graceful shutdown, prune, younger spared)",
       test_daemon_roster)
    ok("idle shutdown watch (quiescence signals)",
       test_idle_shutdown_watch)
    ok("platform seams (layout, handshake, shell)",
       test_platform_seams)
    ok("roster waits for peer death before publishing",
       test_roster_waits_for_peer_death)
    ok("roster force-kills a lingering peer",
       test_roster_force_kills_lingering_peer, posix_only=True)
    ok("endpoint remove_if is owner-scoped",
       test_endpoint_remove_if_owner_scoped)
    ok("posix pty child seam", test_posix_pty_child, posix_only=True)
    ok("terminal seam (raw mode, io, SIGWINCH resize)",
       test_terminal_seam, posix_only=True)
    ok("bridge relay (daemon<->tty, detach key)",
       test_bridge_relay, posix_only=True)
    ok("terminal contract (both implementations)",
       test_terminal_contract)
    ok("utf-8 chunker (split sequences held back)", test_utf8_chunker)
    ok("bridge restores terminal when enter fails",
       test_bridge_enter_failure_restores)
    print(f"\n{PASS}/{PASS + len(FAIL)} unit tests passed")
    if FAIL:
        print("Failed: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())