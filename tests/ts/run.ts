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

import { registry, cleanupScratch } from "./harness.ts";

import "./daemon.test.ts";
import "./completion.test.ts";
import "./offload.test.ts";
import "./offload-policy.test.ts";
import "./pre-daemon.test.ts";
import "./remote-guard.test.ts";
import "./settings.test.ts";
import "./provisioning.test.ts";
import "./discovery.test.ts";
import "./comms-tools.test.ts";
import "./settings-reconciler.test.ts";
import "./transcript.test.ts";
import "./sessions.test.ts";
// Isolated from the process-wide state each test needs: this file drives
// its own SharedState instances, and its last test stops publishing.
import "./session-state.test.ts";

await registry.runAll();
cleanupScratch();
// Exit deterministically: a runner must not depend on an empty event
// loop, and any stray handle is a defect the suite already reports.
process.exit(registry.failed > 0 ? 1 : 0);