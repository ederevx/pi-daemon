/**
 * pi-daemon — offload policy tests.
 * OffloadPolicy is the one owner of "is command offloading on": the
 * PI_OFFLOAD environment variable wins, then `piDaemon.offload.enabled`,
 * then enabled — resolved per decision, never cached, so the offload
 * backend and the onboarding gate cannot disagree.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { OffloadPolicy } from "../../pi/extensions/settings/offload-policy.ts";
import { SettingsStore } from "../../pi/extensions/settings/store.ts";
import { assert, assertEq, scratchDir, test, withEnv } from "./harness.ts";

/** A policy over a fixed section, so no file is read. */
function policy(section: Record<string, unknown>): OffloadPolicy {
  return new OffloadPolicy({ section: () => section });
}

test("offload policy: PI_OFFLOAD wins and only off disables it", async () => {
  const cases: Array<[string | undefined, unknown, boolean]> = [
    [undefined, undefined, true],
    ["", undefined, true],
    [undefined, false, false],
    [undefined, true, true],
    ["off", true, false],
    ["on", false, true],
    ["false", false, true],
    ["0", false, true],
    ["OFF", false, true],
  ];
  for (const [env, stored, expected] of cases) {
    await withEnv({ PI_OFFLOAD: env }, () => {
      const section = stored === undefined ? {} : { offload: { enabled: stored } };
      assertEq(
        policy(section).enabled(),
        expected,
        `PI_OFFLOAD=${String(env)} stored=${String(stored)}`,
      );
    });
  }
});

test("offload policy: a malformed section keeps offloading enabled", () => {
  const sections: Array<Record<string, unknown>> = [
    {},
    { offload: "nope" },
    { offload: [] },
    { offload: { enabled: "yes" } },
  ];
  for (const section of sections) {
    assertEq(policy(section).enabled(), true, JSON.stringify(section));
  }
});

test("offload policy: one instance sees a changed section on its next call", async () => {
  await withEnv({ PI_OFFLOAD: undefined }, () => {
    let section: Record<string, unknown> = { offload: { enabled: true } };
    const instance = new OffloadPolicy({ section: () => section });
    assertEq(instance.enabled(), true, "enabled at first");
    section = { offload: { enabled: false } };
    assertEq(instance.enabled(), false, "no cached copy");
  });
});

test("offload policy: the real store is re-read from the agent directory", async () => {
  const dir = join(scratchDir(), `offload-policy-${Date.now()}`);
  mkdirSync(dir, { recursive: true });
  const file = join(dir, "settings.json");
  await withEnv({ PI_OFFLOAD: undefined, PI_CODING_AGENT_DIR: dir }, () => {
    writeFileSync(file, JSON.stringify({ piDaemon: { offload: { enabled: false } } }));
    const instance = new OffloadPolicy(new SettingsStore());
    assertEq(instance.enabled(), false, "the stored setting disables offloading");
    writeFileSync(file, JSON.stringify({ piDaemon: { offload: { enabled: true } } }));
    assertEq(instance.enabled(), true, "the same instance sees the file change");
  });
});

test("offload policy: sibling piDaemon settings do not affect it", async () => {
  await withEnv({ PI_OFFLOAD: undefined }, () => {
    assert(policy({ extWatch: false, gcIdleHours: 0 }).enabled(), "unrelated keys are ignored");
  });
});
