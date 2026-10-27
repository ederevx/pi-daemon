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
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  test,
  assert,
  assertEq,
  scratchDir,
  waitFor,
  withEnv,
} from "./harness.ts";
import provisionExtension, {
  repoRoot as extensionRoot,
} from "../../pi/extensions/provision.ts";
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

test("extension: resolves the clone that holds the installer", () => {
  assertEq(extensionRoot, REPO, "the extension finds the package clone");
  assert(existsSync(join(extensionRoot, "scripts", "install.sh")),
    "the clone holds the installer the guard runs");
});

/** Registers the extension against a stand-in pi and returns the one
 *  session_start handler it captured. */
function sessionStartHandler(): (ctx: unknown) => Promise<void> {
  let handler: ((event: unknown, ctx: unknown) => Promise<void>) | undefined;
  const pi = {
    on(name: string, fn: (event: unknown, ctx: unknown) => Promise<void>) {
      if (name === "session_start") handler = fn;
    },
  };
  provisionExtension(pi as never);
  assert(handler, "the extension registers session_start");
  const captured = handler;
  return (ctx: unknown) => captured({}, ctx);
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

test("guard: a missing state directory is created for the log", async () => {
  const scratch = new ScratchPackage("missing-state");
  const guard = new ProvisioningGuard(
    new ProvisionStamp(scratch.clone, join(scratch.root, "state", "manifest.json")),
    new PackageProvisioner(scratch.clone));
  await withEnv({ HOME: scratch.home, PATH: scratch.path() }, async () => {
    const spawned = guard.run(() => undefined);
    assert(spawned, "a missing manifest provisions");
    await new Promise<void>((resolve) => spawned.on("exit", () => resolve()));
  });
  assert(existsSync(guard.logPath), "the log directory is created");
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

test("extension: a headless session start provisions without a UI", async () => {
  const scratch = new ScratchPackage("extension-headless");
  scratch.install();
  unlinkSync(scratch.manifest);

  const start = sessionStartHandler();
  await withEnv({ HOME: scratch.home, PATH: scratch.path() }, async () => {
    await start({ ui: undefined });
    await waitFor(() => scratch.stamp().isCurrent(),
      "the session start provisions a stale clone");
  });
  assert(existsSync(join(scratch.home, ".local", "bin", "pi-daemon")),
    "the installed daemon came from the session start");
});

test("extension: a failing notice never escapes the session start", async () => {
  const scratch = new ScratchPackage("extension-broken-ui");
  scratch.install();
  unlinkSync(scratch.manifest);

  const start = sessionStartHandler();
  await withEnv({ HOME: scratch.home, PATH: scratch.path() }, async () => {
    await start({
      ui: {
        notify: () => {
          throw new Error("ui gone");
        },
      },
    });
    await waitFor(() => scratch.stamp().isCurrent(),
      "provisioning finished despite the throwing notice");
  });
});
