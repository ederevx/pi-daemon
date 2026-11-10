/**
 * pi-daemon — minimal zero-dependency TS test harness (same shape as the
 * pi-cache suite). Run from the repo root:
 *
 *   node --experimental-strip-types --experimental-transform-types tests/ts/run.ts
 */

import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export type TestFn = () => void | Promise<void>;

interface TestCase {
  name: string;
  fn: TestFn;
}

/** Thrown by `skip`, so a host without the property still reports it. */
class Skipped extends Error {}

class TestRegistry {
  private readonly cases: TestCase[] = [];
  private passed = 0;
  private readonly failures: string[] = [];
  private readonly skipped: string[] = [];

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
        if (error instanceof Skipped) {
          this.skipped.push(`${testCase.name}: ${error.message}`);
          console.log(`  skip ${testCase.name} (${error.message})`);
          continue;
        }
        this.failures.push(testCase.name);
        console.error(`  FAIL ${testCase.name}`);
        console.error(
          `       ${error instanceof Error ? error.stack ?? error.message : String(error)}`,
        );
      }
    }
    console.log(
      `\n${this.passed}/${this.cases.length} passed` +
        (this.skipped.length > 0 ? `, ${this.skipped.length} skipped` : ""),
    );
    if (this.failures.length > 0) {
      console.error(`Failed: ${this.failures.join(", ")}`);
    }
    if (this.skipped.length > 0) {
      console.error(`Unsupported on this host: ${this.skipped.join("; ")}`);
    }
  }

  get failed(): number {
    return this.failures.length;
  }

  get unsupported(): number {
    return this.skipped.length;
  }
}

export const registry = new TestRegistry();

export function test(name: string, fn: TestFn): void {
  registry.register(name, fn);
}

/** Report this test as not applicable on this host, with the reason. */
export function skip(reason: string): never {
  throw new Skipped(reason);
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

/** The roots, service identities and session identities the suite owns.
 *  An inherited value names the live host instead. */
const OWNED_ROOTS: Record<string, string[]> = {
  home: ["HOME", "USERPROFILE"],
  appdata: ["APPDATA"],
  localappdata: ["LOCALAPPDATA"],
  temp: ["TEMP", "TMP", "TMPDIR"],
  runtime: ["XDG_RUNTIME_DIR"],
  state: ["XDG_STATE_HOME"],
  config: ["XDG_CONFIG_HOME"],
  data: ["XDG_DATA_HOME"],
  agent: ["PI_CODING_AGENT_DIR"],
  "daemon-state": ["PI_DAEMON_STATE_DIR"],
  "daemon-runtime": ["PI_DAEMON_RUNTIME_DIR"],
  services: ["PI_SERVICES_DIR"],
};

const CLEARED_VARIABLES = [
  "PI_DAEMON_ENDPOINT",
  "PI_HOSTED",
  "PI_HOSTED_SESSION",
  "PI_DAEMON_RUN_ID",
  "PI_DAEMON_RUN_GENERATION",
  "PI_COMMS_AGENT_ID",
];

/** Point this process at a scratch tree before any product module resolves
 *  a host root, so no test dials, writes or signals the live daemon. */
export function isolateHost(): void {
  const root = scratchDir();
  for (const [directory, names] of Object.entries(OWNED_ROOTS)) {
    const target = join(root, directory);
    mkdirSync(target, { recursive: true });
    for (const name of names) process.env[name] = target;
  }
  mkdirSync(join(root, "home", "tmp"), { recursive: true });
  for (const name of CLEARED_VARIABLES) delete process.env[name];
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