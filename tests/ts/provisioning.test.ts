/**
 * pi-daemon — provisioning tests.
 *
 * pi runs the package's postinstall only when a git checkout's HEAD moves,
 * so a tag cut on the commit already checked out provisions nothing. The
 * stamp must therefore notice a clone that no longer matches what the
 * installer recorded, and the guard must re-provision it without touching
 * a current one.
 */

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  appendFileSync,
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { test, assert, assertEq, scratchDir, withEnv } from "./harness.ts";
import {
  PackageProvisioner,
  ProvisioningGuard,
  ProvisionStamp,
} from "../../scripts/package-provisioner.mjs";

const REPO = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

/** A scratch agent home plus a clone holding only what provisioning reads,
 *  so an install writes nothing outside the scratch tree. */
class ScratchPackage {
  root: string;
  home: string;
  clone: string;
  manifest: string;

  constructor(name: string) {
    this.root = join(scratchDir(), `provisioning-${name}`);
    this.home = join(this.root, "home");
    this.clone = join(this.root, "pi-daemon");
    this.manifest = join(this.home, ".pi", "agent", ".pi-daemon", "manifest.json");
    mkdirSync(join(this.home, ".pi", "agent"), { recursive: true });
    const bin = join(this.root, "fakebin");
    mkdirSync(bin, { recursive: true });
    for (const tool of ["pi", "systemctl", "loginctl"]) {
      const path = join(bin, tool);
      writeFileSync(path, "#!/bin/sh\nexit 0\n");
      chmodSync(path, 0o755);
    }
    cpSync(join(REPO, "pi"), join(this.clone, "pi"), { recursive: true });
    cpSync(join(REPO, "scripts"), join(this.clone, "scripts"), { recursive: true });
  }

  /** Runs the clone's own installer, exactly as a fresh install would. */
  install(): void {
    const result = spawnSync(
      "bash", [join(this.clone, "scripts", "install.sh"), "--package"], {
        cwd: this.clone,
        env: this.env(),
        encoding: "utf8",
      });
    assertEq(result.status, 0, `install.sh failed: ${result.stderr}`);
  }

  path(): string {
    return `${join(this.root, "fakebin")}:${process.env.PATH ?? ""}`;
  }

  env(): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = { ...process.env, HOME: this.home };
    env.PATH = this.path();
    delete env.PI_CODING_AGENT_DIR;
    return env;
  }

  stamp(): ProvisionStamp {
    return new ProvisionStamp(this.clone, this.manifest);
  }

  guard(): ProvisioningGuard {
    return new ProvisioningGuard(this.stamp(), new PackageProvisioner(this.clone));
  }
}

test("stamp: a package install of this clone is current", () => {
  const scratch = new ScratchPackage("current");
  scratch.install();
  assert(scratch.stamp().isCurrent(), "the recorded sources match the clone");
});

test("stamp: an edited source is stale", () => {
  const scratch = new ScratchPackage("edited");
  scratch.install();
  appendFileSync(join(scratch.clone, "pi", "lib", "pi_services.py"), "\n");
  assert(!scratch.stamp().isCurrent(), "a changed source makes the clone stale");
});

test("stamp: a manifest without package-mode sources is stale", () => {
  const scratch = new ScratchPackage("manual");
  mkdirSync(dirname(scratch.manifest), { recursive: true });
  writeFileSync(scratch.manifest, JSON.stringify({
    version: 1, repo: scratch.clone, mode: "manual", owned: [], hashes: {},
    sources: { "pi/bin/pi-rc": "0".repeat(64) },
  }));
  assert(!scratch.stamp().isCurrent(), "a manual install is never current");
  writeFileSync(scratch.manifest, "{ not json");
  assert(!scratch.stamp().isCurrent(), "an unreadable manifest is stale");
  writeFileSync(scratch.manifest, JSON.stringify({ version: 1, repo: scratch.clone }));
  assert(!scratch.stamp().isCurrent(), "an older manifest is stale");
});

test("guard: a current clone provisions nothing", () => {
  const scratch = new ScratchPackage("noop");
  scratch.install();
  const guard = scratch.guard();
  const before = guard.logPath;
  assertEq(guard.run(() => {
    throw new Error("a current clone must not report");
  }), null, "nothing is spawned");
  assert(!existsSync(before), "no provisioning log is written");
});

test("guard: a manual layout without an installer is a no-op", () => {
  const scratch = new ScratchPackage("no-installer");
  const guard = new ProvisioningGuard(
    scratch.stamp(),
    new PackageProvisioner(join(scratch.root, "missing")));
  assertEq(guard.run(() => {
    throw new Error("a missing installer must not report");
  }), null, "nothing is spawned");
});

test("guard: a stale clone re-provisions the installed copies", async () => {
  const scratch = new ScratchPackage("stale");
  scratch.install();
  const source = join(scratch.clone, "pi", "lib", "pi_services.py");
  appendFileSync(source, "# reconciled\n");

  const failures: Array<string | null | undefined> = [];
  const guard = scratch.guard();
  let spawned: ReturnType<ProvisioningGuard["run"]> = null;
  await withEnv({ HOME: scratch.home, PATH: scratch.path() }, async () => {
    spawned = guard.run((failure) => failures.push(failure));
    assert(spawned, "a stale clone spawns provisioning");
    await new Promise<void>((resolve) => spawned!.on("exit", () => resolve()));
  });
  assertEq(failures.length, 1, "the guard reported once");
  assertEq(failures[0], null, "provisioning succeeded");

  const installed = join(scratch.home, ".local", "bin", "pi_services.py");
  const digest = (path: string) =>
    createHash("sha256").update(readFileSync(path)).digest("hex");
  assertEq(digest(installed), digest(source), "the installed copy followed");
  assert(scratch.stamp().isCurrent(), "the manifest now matches the clone");
});
