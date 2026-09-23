/**
 * pi-daemon — minimal zero-dependency TS test harness (same shape as the
 * pi-cache suite). Run from the repo root:
 *
 *   node --experimental-strip-types --experimental-transform-types tests/ts/run.ts
 */

import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export type TestFn = () => void | Promise<void>;

interface TestCase {
  name: string;
  fn: TestFn;
}

class TestRegistry {
  private readonly cases: TestCase[] = [];
  private passed = 0;
  private readonly failures: string[] = [];

  register(name: string, fn: TestFn): void {
    this.cases.push({ name, fn });
  }

  async runAll(): Promise<void> {
    for (const testCase of this.cases) {
      try {
        await testCase.fn();
        this.passed++;
        console.log(`  ok   ${testCase.name}`);
      } catch (error) {
        this.failures.push(testCase.name);
        console.error(`  FAIL ${testCase.name}`);
        console.error(
          `       ${error instanceof Error ? error.stack ?? error.message : String(error)}`,
        );
      }
    }
    console.log(`\n${this.passed}/${this.cases.length} passed`);
    if (this.failures.length > 0) {
      console.error(`Failed: ${this.failures.join(", ")}`);
    }
  }

  get failed(): number {
    return this.failures.length;
  }
}

export const registry = new TestRegistry();

export function test(name: string, fn: TestFn): void {
  registry.register(name, fn);
}

export function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

export function assertEq<T>(actual: T, expected: T, message?: string): void {
  if (actual !== expected) {
    throw new Error(
      `${message ?? "assertEq"} — expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );
  }
}

export function assertMatches(actual: string, pattern: RegExp, message?: string): void {
  if (!pattern.test(actual)) {
    throw new Error(`${message ?? "assertMatches"} — ${JSON.stringify(actual)} !~ ${pattern}`);
  }
}

export async function waitFor(
  predicate: () => boolean,
  message: string,
  timeoutMs = 3000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`waitFor timeout: ${message}`);
}

let scratchRoot: string | undefined;

/** One scratch root per run, under ~/tmp (never /tmp). */
export function scratchDir(): string {
  if (scratchRoot === undefined) {
    scratchRoot = mkdtempSync(join(homedir(), "tmp", "pi-daemon-ts-tests-"));
  }
  return scratchRoot;
}

export function cleanupScratch(): void {
  if (scratchRoot !== undefined) {
    rmSync(scratchRoot, { recursive: true, force: true });
    scratchRoot = undefined;
  }
}

/** Save/restore env around a test body (async-aware). */
/** Forces `process.platform` for the body — the extension's launch
 *  shape (interpreter prefix, probe paths) follows the host platform,
 *  so POSIX-path tests must pin it explicitly to be portable to a
 *  Windows test host. Restores the real platform afterwards. */
export async function withPlatform(
  platform: string,
  fn: () => void | Promise<void>,
): Promise<void> {
  const real = process.platform;
  Object.defineProperty(process, "platform", {
    value: platform, configurable: true,
  });
  try {
    await fn();
  } finally {
    Object.defineProperty(process, "platform", {
      value: real, configurable: true,
    });
  }
}

export async function withEnv(
  overrides: Record<string, string | undefined>,
  fn: () => void | Promise<void>,
): Promise<void> {
  const saved = new Map<string, string | undefined>();
  for (const key of Object.keys(overrides)) saved.set(key, process.env[key]);
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  try {
    await fn();
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}