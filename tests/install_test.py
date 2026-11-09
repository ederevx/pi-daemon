#!/usr/bin/env python3
"""Tests for scripts/install.sh, in particular its --package mode.

Each case runs the installer against a scratch HOME with a fake `pi` and
fake systemd tools on PATH, then inspects what it published. The package
mode is exercised from a scratch clone, so a case can edit a source and
watch a new generation appear. Scratch lives under ~/tmp, never the system
/tmp. The suite skips without bash.
"""

import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
BASH = shutil.which("bash")
SCRATCH = os.path.expanduser("~/tmp")

sys.path.insert(0, str(ROOT / "pi" / "lib"))

from pi_daemon.lifecycle.instance_coordinator import InstanceCoordinator


class ScratchHome:
    """A disposable HOME with fake external tools for one installer run."""

    def __init__(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.root = pathlib.Path(tempfile.mkdtemp(
            prefix="pi-daemon-install-", dir=SCRATCH))
        (self.root / ".pi" / "agent").mkdir(parents=True)
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        self._write_exe("pi")
        # The unit question is answered the way systemd answers it: `show
        # -p FragmentPath` names the unit file actually loaded, and
        # `is-active` answers what a stopped unit answers. A case overrides
        # either through the environment.
        self._write_exe("systemctl", (
            'printf \'%s\\n\' "$*" >> "${FAKE_SYSTEMCTL_LOG:-/dev/null}"\n'
            'case "$*" in\n'
            '  *show*) printf \'%s\\n\' "${FAKE_SYSTEMCTL_FRAGMENT:-}" ;;\n'
            '  *is-active*) exit "${FAKE_SYSTEMCTL_ACTIVE:-3}" ;;\n'
            'esac\n'))
        self._write_exe("loginctl")
        self.runtime_dir = self.root / "xdg-runtime"
        self.runtime_dir.mkdir()
        self.state_home = self.root / "xdg-state"
        self.state_home.mkdir()
        self.env_extra = {}

    def _write_exe(self, name, extra=""):
        path = self.fakebin / name
        path.write_text("#!/bin/sh\n" + extra + "exit 0\n")
        path.chmod(0o755)

    def clone(self):
        """A copy of this package to install from, so a case can edit it.

        One clone per case: a second install of the same case reuses it, so
        an edit a case made is still there.
        """
        target = self.root / "pi-daemon"
        if not target.is_dir():
            shutil.copytree(ROOT / "pi", target / "pi")
            shutil.copytree(ROOT / "scripts", target / "scripts")
        return target

    def env(self):
        env = dict(os.environ)
        env["HOME"] = str(self.root)
        env["PATH"] = f"{self.fakebin}:{env.get('PATH', '')}"
        env.pop("PI_CODING_AGENT_DIR", None)
        # The release's own commands are what must keep bytecode out of a
        # published generation; an inherited setting would decide it instead.
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        # Every root the installer probes belongs to this scratch tree: the
        # live daemon on this host must never decide a test's outcome.
        env["XDG_RUNTIME_DIR"] = str(self.runtime_dir)
        env["XDG_STATE_HOME"] = str(self.state_home)
        env["PI_DAEMON_STATE_DIR"] = str(self.root / "daemon-state")
        env["PI_DAEMON_RUNTIME_DIR"] = str(self.root / "daemon-runtime")
        env.pop("PI_DAEMON_BASH", None)
        env.update({key: str(value) for key, value in self.env_extra.items()})
        return env

    def run(self, *args, install=None, cwd=None):
        return subprocess.run(
            [BASH, str(install or INSTALL), *args],
            cwd=str(cwd or ROOT), env=self.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def package(self, clone=None):
        """Installs from the clone and returns the completed process."""
        return self.run("--package", install=(clone or self.clone()) / "scripts" / "install.sh")

    def path(self, rel):
        return self.root / rel

    def manifest(self):
        return json.loads(
            self.path(".pi/agent/.pi-daemon/manifest.json").read_text())

    def hold_instance_lock(self):
        """Takes the lock a serving daemon holds, so an install sees one."""
        lock = self.root / "daemon-runtime" / "instance.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = InstanceCoordinator(str(lock))
        holder.acquire()
        return holder

    def releases(self):
        return sorted((self.path(".local/lib/pi-daemon/releases")).iterdir())

    def release(self, rel):
        """A path inside the generation the manifest selects."""
        return pathlib.Path(self.manifest()["active_root"]) / rel

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def winform(path):
    """Normalize an MSYS-style /c/... path from the bash installer to the
    Windows form pathlib reports, so manifest membership checks hold on
    both platforms."""
    text = str(path)
    if len(text) > 2 and text[0] == "/" and text[2] == "/":
        text = text[1].upper() + ":" + text[2:]
    return os.path.normpath(text)


def msysform(path):
    """The form the bash installer writes into a manifest: its own $var.

    A seeded manifest has to speak that form, because the cleanup compares
    the recorded text against the destination as bash sees it, not against
    what pathlib reports."""
    text = winform(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


@unittest.skipUnless(BASH, "no bash interpreter available")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.scratch = ScratchHome()
        self.addCleanup(self.scratch.cleanup)

    def test_bare_install_points_to_package_install(self):
        result = self.scratch.run()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("pi install", result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/.pi-daemon/manifest.json").exists())
        self.assertFalse(self.scratch.path(".local/bin/pi-daemon").exists())

    def test_package_install_publishes_one_generation(self):
        result = self.scratch.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        for rel in (".local/bin/pi-daemon", ".local/bin/pi-rc", ".local/bin/pi",
                    ".local/bin/pi-comms", ".local/bin/pi-daemon-migrate",
                    ".config/systemd/user/pi-daemon.service"):
            self.assertTrue(self.scratch.path(rel).is_file(), rel)
        for rel in ("daemon/pi-daemon", "bin/pi-rc", "bin/pi-comms",
                    "bin/pi-daemon-migrate", "lib/pi_services.py",
                    "lib/pi_daemon/entry.py", "extensions/daemon.ts",
                    "defaults.json"):
            self.assertTrue(self.scratch.release(rel).is_file(), rel)
        manifest = self.scratch.manifest()
        self.assertEqual(manifest["version"], 3)
        self.assertEqual(manifest["mode"], "package")
        self.assertEqual(len(self.scratch.releases()), 1,
                         "one generation is published by one install")
        self.assertEqual(pathlib.Path(manifest["active_root"]),
                         self.scratch.releases()[0])

    def test_package_install_skips_extension_copies(self):
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/offload.ts").exists())
        owned = [winform(o) for o in self.scratch.manifest()["owned"]]
        self.assertFalse(
            [o for o in owned if o.endswith("daemon.ts")],
            "the extension tree is recorded nowhere under the agent home")

    def test_a_release_is_immutable(self):
        # A published generation is the install for a running daemon: no
        # bytecode may appear beside its modules after one of its own commands
        # has run, which the launcher alone cannot prevent.
        self.assertEqual(self.scratch.package().returncode, 0)
        for name in ("pi-comms", "pi-rc", "pi-daemon-migrate"):
            ran = subprocess.run(
                ["python3", str(self.scratch.release(f"bin/{name}")),
                 "--help"],
                env=self.scratch.env(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            self.assertLess(ran.returncode, 3, ran.stderr)
        stray = [p for p in self.scratch.releases()[0].rglob("__pycache__")]
        self.assertEqual(stray, [], "the release was written after publishing")
        for rel in self.scratch.manifest()["sources"]:
            self.assertNotIn("__pycache__", rel)

    def test_package_install_records_sources(self):
        clone = self.scratch.clone()
        result = self.scratch.package(clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.scratch.manifest()
        sources = manifest["sources"]
        for rendered in ("pi/systemd/pi-daemon.service", "pi/bin/pi-wrapper",
                         "pi/daemon/pi-daemon", "scripts/install.sh"):
            self.assertIn(rendered, sources)
        for rel, digest in sources.items():
            expected = hashlib.sha256((clone / rel).read_bytes()).hexdigest()
            self.assertEqual(digest, expected, rel)
        listed = subprocess.run(
            [BASH, str(clone / "scripts" / "install.sh"), "--sources"],
            cwd=str(clone), env=self.scratch.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), sources,
                         "the recording is the inventory the installer would publish")

    def test_installed_comms_client_runs_from_the_generation(self):
        self.assertEqual(self.scratch.package().returncode, 0)
        result = subprocess.run(
            ["python3", str(self.scratch.path(".local/bin/pi-comms")), "help"],
            env=self.scratch.env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: pi-comms", result.stdout)

    def test_a_rerun_republishes_the_same_generation(self):
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        first = self.scratch.manifest()["active_root"]
        result = self.scratch.package(clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.scratch.manifest()["active_root"], first,
                         "an unchanged clone keeps its generation")
        self.assertEqual(len(self.scratch.releases()), 1)

    def test_an_edited_source_publishes_a_new_generation_and_keeps_the_old(self):
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        first = self.scratch.manifest()["active_root"]
        with open(clone / "pi" / "lib" / "pi_services.py", "a",
                  encoding="utf-8") as handle:
            handle.write("\n# a new build\n")
        result = self.scratch.package(clone)
        self.assertEqual(result.returncode, 0, result.stderr)
        second = self.scratch.manifest()["active_root"]
        self.assertNotEqual(second, first, "the edit publishes a new release")
        self.assertTrue(pathlib.Path(first).is_dir(),
                        "a running daemon keeps the release it started from")
        self.assertIn(first, self.scratch.manifest()["retained"])
        self.assertIn("# a new build",
                      (pathlib.Path(second) / "lib" / "pi_services.py").read_text())

    def test_package_install_removes_manifest_owned_extension_copies(self):
        # An install that predates the package-only rule recorded these as
        # its own, and the current clone's content never matches them once
        # either side is edited, so ownership is what decides.
        ext_dir = self.scratch.path(".pi/agent/extensions")
        ext_dir.mkdir(parents=True)
        for name in ("daemon.ts", "offload.ts"):
            dst = ext_dir / name
            dst.write_text("// an older install's copy\n")
        state = self.scratch.path(".pi/agent/.pi-daemon")
        state.mkdir(parents=True)
        (state / "manifest.json").write_text(json.dumps({
            "version": 1,
            "mode": "manual",
            "owned": [msysform(ext_dir / name)
                      for name in ("daemon.ts", "offload.ts")],
            "hashes": {},
            "sources": {},
        }))
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse((ext_dir / "daemon.ts").exists())
        self.assertFalse((ext_dir / "offload.ts").exists())

    def test_package_install_removes_unrecorded_identical_copy(self):
        ext_dir = self.scratch.path(".pi/agent/extensions")
        ext_dir.mkdir(parents=True)
        for name in ("daemon.ts", "offload.ts"):
            shutil.copyfile(ROOT / "pi" / "extensions" / name,
                            ext_dir / name)
        self.assertFalse(
            self.scratch.path(".pi/agent/.pi-daemon/manifest.json").exists())
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse((ext_dir / "daemon.ts").exists())
        self.assertFalse((ext_dir / "offload.ts").exists())

    def test_package_install_keeps_foreign_extension_copy(self):
        ext_dir = self.scratch.path(".pi/agent/extensions")
        ext_dir.mkdir(parents=True)
        (ext_dir / "daemon.ts").write_text("// a user file\n")
        (ext_dir / "offload.ts").write_text("// a user file\n")
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertTrue((ext_dir / "daemon.ts").exists())
        self.assertTrue((ext_dir / "offload.ts").exists())

    def test_unknown_argument_is_an_error(self):
        self.assertEqual(self.scratch.run("--bogus").returncode, 2)
        self.assertIn("unexpected argument", self.scratch.run("--bogus").stderr)

    def test_uninstall_removes_the_install_and_keeps_foreign_files(self):
        self.assertEqual(self.scratch.package().returncode, 0)
        foreign = self.scratch.path(".local/bin/other-tool")
        foreign.write_text("#!/bin/sh\nexit 0\n")
        result = subprocess.run(
            [BASH, str(UNINSTALL)], cwd=str(ROOT), env=self.scratch.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for rel in (".local/bin/pi-comms", ".local/bin/pi-daemon",
                    ".local/bin/pi",
                    ".config/systemd/user/pi-daemon.service"):
            self.assertFalse(self.scratch.path(rel).exists(), rel)
        self.assertFalse(
            self.scratch.path(".local/lib/pi-daemon").exists(),
            "the private tree above the releases is dropped")
        self.assertTrue(foreign.is_file())

    def test_uninstall_removes_a_retained_generation(self):
        # A retained release is described by the manifest that published it,
        # so its recorded files go and anything else stays.
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        first = pathlib.Path(self.scratch.manifest()["active_root"])
        with open(clone / "pi" / "lib" / "pi_services.py", "a",
                  encoding="utf-8") as handle:
            handle.write("\n# a new build\n")
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        self.assertEqual(self.scratch.manifest()["retained"], [str(first)])
        foreign = first / "notes.txt"
        foreign.write_text("not ours\n")
        result = subprocess.run(
            [BASH, str(UNINSTALL)], cwd=str(ROOT), env=self.scratch.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((first / "lib" / "pi_services.py").exists(),
                         "the retained generation's recorded files go")
        self.assertTrue(foreign.is_file(),
                        "a file the manifest never recorded stays")

    def test_install_refuses_while_locked(self):
        lock = self.scratch.path(".pi/agent/.pi-daemon/install.lock")
        lock.mkdir(parents=True)
        result = self.scratch.package()
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertFalse(self.scratch.path(".local/bin/pi-daemon").exists())
        self.assertTrue(lock.is_dir())

    def test_install_clears_stale_lock(self):
        lock = self.scratch.path(".pi/agent/.pi-daemon/install.lock")
        lock.mkdir(parents=True)
        stale = 3600
        os.utime(lock, (lock.stat().st_mtime - stale,
                        lock.stat().st_mtime - stale))
        result = self.scratch.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.scratch.path(".local/bin/pi-daemon").is_file())
        self.assertFalse(lock.exists())

    def test_install_refuses_beside_a_serving_endpoint(self):
        # A legacy install still running would fight the translation of its
        # own state, so the installer stops before writing anything.
        endpoint = self.scratch.runtime_dir / "pi-pty-host" / "control.json"
        endpoint.parent.mkdir()
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        self.addCleanup(listening.close)
        endpoint.write_text(json.dumps({
            "host": "127.0.0.1", "port": listening.getsockname()[1],
        }))
        accepted = threading.Event()
        self.addCleanup(accepted.set)

        def accept():
            try:
                listening.settimeout(5)
                connection, _ = listening.accept()
                connection.close()
            except OSError:
                pass
            finally:
                accepted.set()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        result = self.scratch.package()
        thread.join(5)
        self.assertEqual(result.returncode, 76, result.stdout + result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/.pi-daemon/manifest.json").exists())
        self.assertFalse(self.scratch.path(".local/bin/pi-daemon").exists())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no unix sockets here")
    def test_install_refuses_beside_a_serving_socket(self):
        # The legacy daemon publishes a unix socket, which no `-f` test sees:
        # the endpoint probe has to reach it as a socket.
        endpoint = self.scratch.runtime_dir / "pi-pty-host.sock"
        listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listening.bind(str(endpoint))
        listening.listen(1)
        self.addCleanup(listening.close)
        result = self.scratch.package()
        self.assertEqual(result.returncode, 76, result.stdout + result.stderr)
        self.assertFalse(self.scratch.path(".local/bin/pi-daemon").exists())

    def test_a_current_install_publishes_beside_a_running_daemon(self):
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        first = self.scratch.manifest()["active_root"]
        marker = self.scratch.path("daemon-state/migration.json").read_bytes()
        with open(clone / "pi" / "bin" / "pi-rc", "a", encoding="utf-8") as handle:
            handle.write("\n# a new build\n")
        holder = self.scratch.hold_instance_lock()
        self.addCleanup(holder.release)
        log = self.scratch.path("systemctl.log")
        self.scratch.env_extra = {"FAKE_SYSTEMCTL_LOG": log,
                                  "FAKE_SYSTEMCTL_ACTIVE": "3"}
        result = self.scratch.package(clone)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(self.scratch.manifest()["active_root"], first)
        self.assertIn(first, self.scratch.manifest()["retained"])
        self.assertEqual(
            self.scratch.path("daemon-state/migration.json").read_bytes(), marker)
        # A held lock means a daemon already serves this root: the unit is
        # enabled, and a start would only restart it into the lock.
        calls = log.read_text().splitlines()
        self.assertIn("--user enable pi-daemon.service", calls)
        self.assertNotIn("--user start pi-daemon.service", calls)
        self.assertIn("the unit is enabled, not started", result.stderr)

    def test_a_first_install_enables_and_starts_the_unit(self):
        log = self.scratch.path("systemctl.log")
        self.scratch.env_extra = {"FAKE_SYSTEMCTL_LOG": log}
        self.assertEqual(self.scratch.package().returncode, 0)
        calls = log.read_text().splitlines()
        self.assertIn("--user enable pi-daemon.service", calls)
        self.assertIn("--user start pi-daemon.service", calls)

    def test_a_held_lock_with_the_unit_serving_starts_nothing(self):
        # The normal state after a takeover: the lock is held and the unit
        # is the holder, so there is no untaken root for the install to
        # report and nothing to start.
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        with open(clone / "pi" / "bin" / "pi-rc", "a", encoding="utf-8") as handle:
            handle.write("\n# a new build\n")
        holder = self.scratch.hold_instance_lock()
        self.addCleanup(holder.release)
        log = self.scratch.path("systemctl.log")
        self.scratch.env_extra = {
            "FAKE_SYSTEMCTL_LOG": log,
            "FAKE_SYSTEMCTL_ACTIVE": "0",
            "FAKE_SYSTEMCTL_FRAGMENT": self.scratch.path(
                ".config/systemd/user/pi-daemon.service")}
        result = self.scratch.package(clone)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = log.read_text().splitlines()
        self.assertIn("--user enable pi-daemon.service", calls)
        self.assertNotIn("--user start pi-daemon.service", calls)
        self.assertNotIn("the unit is enabled, not started", result.stderr)

    def test_an_untranslated_install_still_refuses_beside_a_running_daemon(self):
        holder = self.scratch.hold_instance_lock()
        self.addCleanup(holder.release)
        result = self.scratch.package()
        self.assertEqual(result.returncode, 76, result.stdout + result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/.pi-daemon/manifest.json").exists())

    def test_install_refuses_beside_an_active_unit(self):
        # A legacy unit declares neither new root, so the question is which
        # unit file systemd loaded, not what that file says.
        unit = self.scratch.path(".config/systemd/user/pi-daemon.service")
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=%h/.local/bin/pi-daemon\n")
        self.scratch.env_extra = {"FAKE_SYSTEMCTL_FRAGMENT": unit,
                                  "FAKE_SYSTEMCTL_ACTIVE": "0"}
        result = self.scratch.package()
        self.assertEqual(result.returncode, 76, result.stdout + result.stderr)
        self.assertIn("systemctl --user stop", result.stderr)
        self.assertFalse(self.scratch.path(".local/bin/pi-daemon").exists())

    def test_install_ignores_a_unit_from_another_home(self):
        # Another home's unit is not this install's daemon: its daemon keeps
        # running and this install proceeds. This home's unit exists, so the
        # comparison of the loaded unit with it is what decides.
        own = self.scratch.path(".config/systemd/user/pi-daemon.service")
        own.parent.mkdir(parents=True)
        own.write_text(
            "[Service]\n"
            "Environment=\"PI_DAEMON_STATE_DIR="
            f"{self.scratch.path('daemon-state')}\"\n"
            "Environment=\"PI_DAEMON_RUNTIME_DIR="
            f"{self.scratch.path('daemon-runtime')}\"\n"
            "ExecStart=%h/.local/bin/pi-daemon\n")
        other = self.scratch.path("other-home/pi-daemon.service")
        other.parent.mkdir(parents=True)
        other.write_text("[Service]\nExecStart=%h/.local/bin/pi-daemon\n")
        self.scratch.env_extra = {"FAKE_SYSTEMCTL_FRAGMENT": other,
                                  "FAKE_SYSTEMCTL_ACTIVE": "0"}
        result = self.scratch.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            other.read_text(),
            "[Service]\nExecStart=%h/.local/bin/pi-daemon\n",
            "another home's unit is left alone")
        rendered = own.read_text()
        self.assertIn(
            f"Environment=\"PI_DAEMON_STATE_DIR="
            f"{self.scratch.path('daemon-state')}\"", rendered,
            "this home's unit was rendered by this install")
        self.assertIn("RestartPreventExitStatus=3", rendered,
                      "a held lock must not be restarted")

    def test_a_v2_install_retires_its_flat_seam_module(self):
        # A manifest-only (v2) install owned the seams beside the binaries;
        # the generation owns them now, so those copies go.
        seams = self.scratch.path(".local/bin")
        seams.mkdir(parents=True)
        (seams / "pi_platform.py").write_text("# a flat seam module\n")
        state = self.scratch.path(".pi/agent/.pi-daemon")
        state.mkdir(parents=True)
        (state / "manifest.json").write_text(json.dumps({
            "version": 1, "mode": "manual",
            "owned": [msysform(seams / "pi_platform.py")],
            "hashes": {}, "sources": {},
        }))
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse((seams / "pi_platform.py").exists())
        self.assertEqual(self.scratch.manifest()["version"], 3)

    def test_a_mutated_generation_is_rebuilt_rather_than_reused(self):
        # A release is trusted by the content its name digests, so a tree
        # changed after it was published is not published again.
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        active = pathlib.Path(self.scratch.manifest()["active_root"])
        with open(active / "lib" / "pi_services.py", "a",
                  encoding="utf-8") as handle:
            handle.write("\n# not ours\n")
        with open(clone / "pi" / "lib" / "pi_services.py", "r",
                  encoding="utf-8") as handle:
            pristine = handle.read()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        self.assertEqual(self.scratch.manifest()["active_root"], str(active),
                         "the published content keeps its name")
        self.assertEqual((active / "lib" / "pi_services.py").read_text(),
                         pristine, "the mutated tree is rebuilt")

    def test_a_mutated_retained_generation_is_rebuilt_in_its_own_name(self):
        # A retained generation can be the one a revert selects again. It is
        # replaced by a rename, never deleted in place: a daemon may be
        # importing from it, and nothing may be lost if the run is interrupted.
        clone = self.scratch.clone()
        source = clone / "pi" / "lib" / "pi_services.py"
        original = source.read_bytes()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        first = pathlib.Path(self.scratch.manifest()["active_root"])
        with open(source, "a", encoding="utf-8") as handle:
            handle.write("\n# a new build\n")
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        second = pathlib.Path(self.scratch.manifest()["active_root"])
        self.assertNotEqual(first, second)
        self.assertEqual(self.scratch.manifest()["retained"], [str(first)])
        with open(first / "lib" / "pi_services.py", "a",
                  encoding="utf-8") as handle:
            handle.write("\n# not ours\n")
        source.write_bytes(original)
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        self.assertEqual(self.scratch.manifest()["active_root"], str(first),
                         "a revert selects the retained content again")
        self.assertTrue(second.is_dir(), "the newer generation is kept")
        self.assertEqual((first / "lib" / "pi_services.py").read_bytes(),
                         original, "the mutated tree holds its content again")
        self.assertEqual(
            [p.name for p in self.scratch.releases()
             if p.name.startswith(".stale.")],
            [], "the moved-aside tree does not outlive the run")

    def test_recovery_keeps_a_generation_it_did_not_create(self):
        # A publish that reuses an existing release created nothing, so an
        # interrupted run has nothing of its own to drop.
        clone = self.scratch.clone()
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        first = self.scratch.manifest()["active_root"]
        with open(clone / "pi" / "lib" / "pi_services.py", "a",
                  encoding="utf-8") as handle:
            handle.write("\n# a new build\n")
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        second = self.scratch.manifest()["active_root"]
        self.assertEqual(self.scratch.manifest()["retained"], [first])
        (self.scratch.path(".pi/agent/.pi-daemon/install-transaction.json")
         .write_text(json.dumps({"version": 1,
                                 "active_root": msysform(first),
                                 "created": False, "backups": []})))
        self.assertEqual(self.scratch.package(clone).returncode, 0)
        self.assertTrue(pathlib.Path(first).is_dir(),
                        "a retained release survives recovery")
        self.assertTrue(pathlib.Path(second).is_dir())

    def test_recovery_drops_the_generation_an_interrupted_run_created(self):
        self.assertEqual(self.scratch.package().returncode, 0)
        active = pathlib.Path(self.scratch.manifest()["active_root"])
        orphan = self.scratch.path(".local/lib/pi-daemon/releases/orphan")
        (orphan / "daemon").mkdir(parents=True)
        (self.scratch.path(".pi/agent/.pi-daemon/install-transaction.json")
         .write_text(json.dumps({"version": 1,
                                 "active_root": msysform(orphan),
                                 "created": True, "backups": []})))
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse(orphan.exists(), "an unreferenced generation is dropped")
        self.assertTrue(active.is_dir())

    def test_a_sweep_keeps_the_active_and_the_newest_retained(self):
        # No daemon serves, so an install drops every generation but the
        # active one and the newest retained one: the directory stays bounded.
        self.assertEqual(self.scratch.package().returncode, 0)
        releases = self.scratch.path(".local/lib/pi-daemon/releases")
        active = self.scratch.manifest()["active_root"]
        older, newest = releases / "older", releases / "newest"
        for tree in (older, newest):
            (tree / "daemon").mkdir(parents=True)
        manifest = self.scratch.manifest()
        manifest["retained"] = [msysform(newest), msysform(older)]
        (self.scratch.path(".pi/agent/.pi-daemon/manifest.json")
         .write_text(json.dumps(manifest)))
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse(older.exists(), "an older retained generation goes")
        self.assertTrue(newest.is_dir(), "the newest retained one stays")
        self.assertTrue(pathlib.Path(active).is_dir())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no unix sockets here")
    def test_a_sweep_keeps_every_generation_while_a_daemon_serves(self):
        # A serving daemon may be running any retained generation, so none is
        # removed while one answers.
        self.assertEqual(self.scratch.package().returncode, 0)
        releases = self.scratch.path(".local/lib/pi-daemon/releases")
        older, newest = releases / "older", releases / "newest"
        for tree in (older, newest):
            (tree / "daemon").mkdir(parents=True)
        manifest = self.scratch.manifest()
        manifest["retained"] = [msysform(newest), msysform(older)]
        (self.scratch.path(".pi/agent/.pi-daemon/manifest.json")
         .write_text(json.dumps(manifest)))
        endpoint = self.scratch.runtime_dir / "pi-pty-host.sock"
        listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listening.bind(str(endpoint))
        listening.listen(1)
        self.addCleanup(listening.close)
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertTrue(older.is_dir(), "a serving daemon keeps every one")
        self.assertTrue(newest.is_dir())

    def test_an_unreferenced_generation_is_swept(self):
        # A crash between publishing a generation and writing its journal, or
        # a killed staging directory, leaves nothing the manifest names.
        self.assertEqual(self.scratch.package().returncode, 0)
        releases = self.scratch.path(".local/lib/pi-daemon/releases")
        (releases / "leftover").mkdir()
        (releases / ".staging.999").mkdir()
        self.assertEqual(self.scratch.package().returncode, 0)
        self.assertFalse((releases / "leftover").exists())
        self.assertFalse((releases / ".staging.999").exists())


if __name__ == "__main__":
    unittest.main()
