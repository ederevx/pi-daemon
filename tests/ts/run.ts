/**
 * pi-daemon — TS extension test runner.
 *
 * Run from the repo root:
 *
 *   node --experimental-strip-types --experimental-transform-types tests/ts/run.ts
 *
 * Requires package.json's peerDependencies linked from the pi install into
 * the gitignored `node_modules/`; without them the suite reports itself
 * blocked. Scratch lives under $TMPDIR when a harness provides one, else
 * ~/.tmp/pdt.
 */

import {
  registry, cleanupScratch, isolateHost, skip, test, unresolvedPeers,
} from "./harness.ts";

// Before any test module loads a product module: every root, endpoint and
// session identity belongs to this run's scratch tree, never the host's.
isolateHost();

// The product imports pi's own packages as peers. Without them no module
// loads, so the prerequisite is reported blocked, never a cryptic failure.
const missing = unresolvedPeers();
if (missing.length > 0) {
  test("extension suite", () =>
    skip(`blocked: ${missing.join(", ")} unresolved; link package.json's ` +
      "peerDependencies from the pi install into node_modules/"));
} else {
  await import("./daemon.test.ts");
  await import("./completion.test.ts");
  await import("./offload.test.ts");
  await import("./offload-policy.test.ts");
  await import("./pre-daemon.test.ts");
  await import("./remote-guard.test.ts");
  await import("./settings.test.ts");
  await import("./provisioning.test.ts");
  await import("./discovery.test.ts");
  await import("./comms-tools.test.ts");
  await import("./comms-delivery.test.ts");
  // The four gates composed: every handler order, both full walk orders.
  await import("./gate-composition.test.ts");
  await import("./settings-reconciler.test.ts");
  await import("./transcript.test.ts");
  await import("./sessions.test.ts");
  // Isolated from the process-wide state each test needs: this file drives
  // its own SharedState instances, and its last test stops publishing.
  await import("./session-state.test.ts");
}

await registry.runAll();
cleanupScratch();
// Exit deterministically: a runner must not depend on an empty event
// loop, and any stray handle is a defect the suite already reports.
process.exit(registry.failed > 0 ? 1 : 0);
