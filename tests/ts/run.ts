/**
 * pi-daemon — TS extension test runner.
 *
 * Run from the repo root:
 *
 *   node --experimental-strip-types --experimental-transform-types tests/ts/run.ts
 *
 * Requires the gitignored `node_modules` symlink farm to the pi global
 * install (see repo .gitignore). Scratch lives under ~/tmp.
 */

import { registry, cleanupScratch, isolateHost } from "./harness.ts";

// Before any test module loads a product module: every root, endpoint and
// session identity belongs to this run's scratch tree, never the host's.
isolateHost();

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
await import("./settings-reconciler.test.ts");
await import("./transcript.test.ts");
await import("./sessions.test.ts");
// Isolated from the process-wide state each test needs: this file drives
// its own SharedState instances, and its last test stops publishing.
await import("./session-state.test.ts");

await registry.runAll();
cleanupScratch();
// Exit deterministically: a runner must not depend on an empty event
// loop, and any stray handle is a defect the suite already reports.
process.exit(registry.failed > 0 ? 1 : 0);
