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
import "./offload.test.ts";

await registry.runAll();
cleanupScratch();
if (registry.failed > 0) process.exit(1);