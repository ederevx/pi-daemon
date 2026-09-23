#!/usr/bin/env python3
"""Tests for scripts/install.sh, in particular its --package mode.

Each case runs the installer against a scratch HOME with a fake `pi` and
fake systemd tools on PATH, then inspects the files it wrote. Scratch
lives under ~/tmp, never the system /tmp. The suite skips without bash.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
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


@unittest.skipUnless(BASH, "no bash interpreter available")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.scratch = ScratchHome()
        self.addCleanup(self.scratch.cleanup)

    def test_manual_install_writes_extensions(self):
        result = self.scratch.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.scratch.path(".pi/agent/extensions/daemon.ts").is_file())
        self.assertTrue(
            self.scratch.path(".pi/agent/extensions/offload.ts").is_file())
        owned = self.scratch.manifest()["owned"]
        self.assertIn(winform(self.scratch.path(".pi/agent/extensions/daemon.ts")), [winform(o) for o in owned])

    def test_package_install_skips_extensions_and_writes_service_files(self):
        result = self.scratch.run("--package")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/offload.ts").exists())
        for rel in (".local/bin/pi-daemon", ".local/bin/pi-rc", ".local/bin/pi",
                    ".local/bin/pi_platform.py", ".local/bin/pi_conpty.py",
                    ".config/systemd/user/pi-daemon.service"):
            self.assertTrue(self.scratch.path(rel).is_file(), rel)
        owned = self.scratch.manifest()["owned"]
        self.assertNotIn(winform(self.scratch.path(".pi/agent/extensions/daemon.ts")), [winform(o) for o in owned])

    def test_package_install_removes_prior_manual_extensions(self):
        self.assertEqual(self.scratch.run().returncode, 0)
        self.assertTrue(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())
        self.assertEqual(self.scratch.run("--package").returncode, 0)
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/offload.ts").exists())

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

    def test_manual_install_refuses_when_pinned(self):
        settings = self.scratch.path(".pi/agent/settings.json")
        settings.write_text(json.dumps({"packages": [
            "git:github.com/ederevx/pi-daemon@v2.9.24"]}))
        result = self.scratch.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed as a pi package", result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())
        self.assertFalse(
            self.scratch.path(".pi/agent/.pi-daemon/manifest.json").exists())

    def test_manual_install_refuses_local_path_entry(self):
        settings = self.scratch.path(".pi/agent/settings.json")
        settings.write_text(json.dumps({"packages": [
            str(ROOT)]}))
        result = self.scratch.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed as a pi package", result.stderr)
        self.assertFalse(
            self.scratch.path(".pi/agent/extensions/daemon.ts").exists())

    def test_manual_install_allowed_without_pin(self):
        settings = self.scratch.path(".pi/agent/settings.json")
        settings.write_text(json.dumps({"packages": [
            "git:github.com/ederevx/pi-teams@v0.4.18"]}))
        result = self.scratch.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.scratch.path(".pi/agent/extensions/daemon.ts").is_file())


if __name__ == "__main__":
    unittest.main()