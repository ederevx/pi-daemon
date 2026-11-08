#!/usr/bin/env python3
"""Tests for scripts/install.sh, in particular its --package mode.

Each case runs the installer against a scratch HOME with a fake `pi` and
fake systemd tools on PATH, then inspects the files it wrote. Scratch
lives under ~/tmp, never the system /tmp. The suite skips without bash.
"""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
BASH = shutil.which("bash")
SCRATCH = os.path.expanduser("~/tmp")


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
        self._write_exe("systemctl")
        self._write_exe("loginctl")

    def _write_exe(self, name):
        path = self.fakebin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    def env(self):
        env = dict(os.environ)
        env["HOME"] = str(self.root)
        env["PATH"] = f"{self.fakebin}:{env.get('PATH', '')}"
        env.pop("PI_CODING_AGENT_DIR", None)
        return env

    def run(self, *args):
        return subprocess.run(
            [BASH, str(INSTALL), *args], cwd=str(ROOT), env=self.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def path(self, rel):
        return self.root / rel

    def manifest(self):
        return json.loads(
            self.path(".pi/agent/.pi-daemon/manifest.json").read_text())

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

    def test_package_install_skips_extensions_and_writes_service_files(self):
        result = self.scratch.run("--package")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/offload.ts").exists())
        for rel in (".local/bin/pi-daemon", ".local/bin/pi-rc", ".local/bin/pi",
                    ".local/bin/pi_platform.py", ".local/bin/pi_conpty.py",
                    ".local/bin/pi_settings.py", ".local/bin/pi_transcript.py",
                    ".local/bin/pi_services.py", ".local/bin/pi_rpc_client.py",
                    ".local/bin/pi-comms",
                    ".config/systemd/user/pi-daemon.service"):
            self.assertTrue(self.scratch.path(rel).is_file(), rel)
        owned = self.scratch.manifest()["owned"]
        self.assertNotIn(winform(self.scratch.path(".pi/agent/extensions/daemon.ts")), [winform(o) for o in owned])

    def test_package_install_writes_comms_client(self):
        result = self.scratch.run("--package")
        self.assertEqual(result.returncode, 0, result.stderr)
        comms = self.scratch.path(".local/bin/pi-comms")
        self.assertTrue(comms.is_file())
        if os.name != "nt":
            self.assertTrue(os.access(comms, os.X_OK))
        owned = [winform(o) for o in self.scratch.manifest()["owned"]]
        for rel in (".local/bin/pi-comms", ".local/bin/pi_rpc_client.py"):
            self.assertIn(winform(self.scratch.path(rel)), owned, rel)

    def test_installed_comms_client_imports_from_local_bin(self):
        # The installed copy resolves its modules beside itself, so a
        # missing pi_rpc_client.py fails here and nowhere else.
        self.assertEqual(self.scratch.run("--package").returncode, 0)
        result = subprocess.run(
            ["python3", str(self.scratch.path(".local/bin/pi-comms")), "help"],
            env=self.scratch.env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: pi-comms", result.stdout)

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
        self.assertEqual(self.scratch.run("--package").returncode, 0)
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
        self.assertEqual(self.scratch.run("--package").returncode, 0)
        self.assertFalse((ext_dir / "daemon.ts").exists())
        self.assertFalse((ext_dir / "offload.ts").exists())

    def test_package_install_keeps_foreign_extension_copy(self):
        ext_dir = self.scratch.path(".pi/agent/extensions")
        ext_dir.mkdir(parents=True)
        (ext_dir / "daemon.ts").write_text("// a user file\n")
        (ext_dir / "offload.ts").write_text("// a user file\n")
        self.assertEqual(self.scratch.run("--package").returncode, 0)
        self.assertTrue((ext_dir / "daemon.ts").exists())
        self.assertTrue((ext_dir / "offload.ts").exists())

    def test_unknown_argument_is_an_error(self):
        self.assertEqual(self.scratch.run("--bogus").returncode, 2)
        self.assertIn("unexpected argument", self.scratch.run("--bogus").stderr)

    def test_package_install_records_sources(self):
        result = self.scratch.run("--package")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = self.scratch.manifest()
        self.assertEqual(manifest["mode"], "package")
        self.assertEqual(manifest["version"], 2)
        sources = manifest["sources"]
        for rendered in ("pi/systemd/pi-daemon.service", "pi/bin/pi-wrapper",
                         "scripts/install.sh"):
            self.assertIn(rendered, sources)
        for rel, digest in sources.items():
            expected = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
            self.assertEqual(digest, expected, rel)

    def test_uninstall_removes_comms_client(self):
        self.assertEqual(self.scratch.run("--package").returncode, 0)
        foreign = self.scratch.path(".local/bin/other-tool")
        foreign.write_text("#!/bin/sh\nexit 0\n")
        result = subprocess.run(
            [BASH, str(UNINSTALL)], cwd=str(ROOT), env=self.scratch.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.scratch.path(".local/bin/pi-comms").exists())
        self.assertFalse(
            self.scratch.path(".local/bin/pi_rpc_client.py").exists())
        self.assertTrue(foreign.is_file())

    def test_install_refuses_while_locked(self):
        lock = self.scratch.path(".pi/agent/.pi-daemon/install.lock")
        lock.mkdir(parents=True)
        result = self.scratch.run("--package")
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertFalse(self.scratch.path(".local/bin/pi-daemon").exists())
        self.assertTrue(lock.is_dir())

    def test_install_clears_stale_lock(self):
        lock = self.scratch.path(".pi/agent/.pi-daemon/install.lock")
        lock.mkdir(parents=True)
        stale = 3600
        os.utime(lock, (lock.stat().st_mtime - stale,
                        lock.stat().st_mtime - stale))
        result = self.scratch.run("--package")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.scratch.path(".local/bin/pi-daemon").is_file())
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()