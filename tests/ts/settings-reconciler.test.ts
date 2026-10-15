/**
 * pi-daemon — settings-reconciler tests.
 *
 * The one-loader rule must recognize a local install that points at this
 * checkout whatever the directory is named, so a worktree or renamed
 * clone never survives as a second entry. An unrelated local path is not
 * this package and must be left alone.
 */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { test, assertEq, scratchDir } from "./harness.ts";
import { SettingsReconciler } from "../../scripts/settings-reconciler.mjs";

function settingsWith(name: string, packages: string[]): string {
  const dir = join(scratchDir(), name);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "settings.json");
  writeFileSync(path, JSON.stringify({ packages }, null, 2) + "\n");
  return path;
}

test("reconciler: a differently named local checkout is a duplicate", () => {
  const checkout = join(scratchDir(), `renamed-checkout-${Date.now()}`);
  mkdirSync(checkout, { recursive: true });
  const path = settingsWith("reconciler-renamed", [
    "git:github.com/ederevx/pi-daemon@v1.2.3",
    checkout,
  ]);
  const dropped = new SettingsReconciler(
    path, "1.2.3", "pi-daemon", checkout).reconcile();
  assertEq(dropped.length, 1, "the differently named copy is dropped");
  assertEq(dropped[0], checkout, "the local copy is the duplicate");
  const saved = JSON.parse(readFileSync(path, "utf8"));
  assertEq(saved.packages.length, 1, "one entry is kept");
  assertEq(saved.packages[0], "git:github.com/ederevx/pi-daemon@v1.2.3",
    "the version-matching pin wins");
});

test("reconciler: an unrelated local path is left alone", () => {
  const checkout = join(scratchDir(), `checkout-${Date.now()}`);
  const other = join(scratchDir(), `other-${Date.now()}`);
  mkdirSync(checkout, { recursive: true });
  mkdirSync(other, { recursive: true });
  const path = settingsWith("reconciler-other", [
    "git:github.com/ederevx/pi-daemon@v1.2.3",
    other,
  ]);
  const dropped = new SettingsReconciler(
    path, "1.2.3", "pi-daemon", checkout).reconcile();
  assertEq(dropped.length, 0, "an unrelated path is not this package");
  const saved = JSON.parse(readFileSync(path, "utf8"));
  assertEq(saved.packages.length, 2, "both entries stay");
});
