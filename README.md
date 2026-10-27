# pi-daemon

Keeps Pi sessions running when terminals disconnect, with automatic
reattachment, background command tickets, and transcript retrieval.

## What it provides

- **PTY host daemon** - one PTY per hosted pi TUI session; sessions
  survive client disconnect and daemon restart, detached output is
  drained, and no tmux is involved.
- **Auto-hosting wrapper** - `pi` attaches to the directory's
  daemon-owned session so terminals become viewports instead of
  duplicate sessions; `pi --new` starts a fresh one.
- **`daemon` extension** - `/bg` detaches instantly inside a hosted
  session or hands a plain session over, without aborting in-flight
  work.
- **Extension reload watch** - polls extension roots and reloads
  hosted sessions in place on change.
- **Command offloading** - `bash` becomes a daemon-owned ticket, with
  `daemon_tasks` for result, watch, status, cancel, and remove.
- **Transcript retrieval** - `transcript_read` serves indexed
  conversation reads without parsing the whole file.
- **Extension service contract** - `docs/pi-services.md` defines
  `pi-rpc/1`, the shared contract for one extension to call another or
  subscribe to its signals.

## Install

### As a pi package

```bash
pi install git:github.com/ederevx/pi-daemon@v2.9.54
```

`npm install` runs `scripts/postinstall.mjs` on a fresh install, and the
package's `provision` extension reconciles on every session start: when
the clone's sources differ from what the install manifest recorded, it
re-runs `scripts/install.sh --package` in the background. Together they
provision the systemd user unit, the `pi-rc` client, the `pi` wrapper, the
daemon, and the seam modules to the same manual paths, while the
extensions load from the package clone. pi skips `npm install` when an
updated pin resolves to the commit already checked out, so the
session-start reconciliation is what applies such an update. Provisioning
removes extension copies a prior manual install owned so only one loader
source remains; if a leftover copy aborts sessions with tool-conflict
errors before that can happen, run `bash scripts/install.sh --package`.
Adopt a new daemon binary with `/daemon-reload`.

## Uninstall

```bash
bash scripts/uninstall.sh
```

## Layout

```
pi/
  extensions/daemon.ts   # /bg: detach/handover, /daemon-purge, reload relay
                         # + the daemon_gc_reap tool
  extensions/offload.ts  # ticket offloading + daemon_tasks tool
  extensions/tickets/    # multi-ticket stream cursor, feed, wait loop, view
  extensions/pre-daemon.ts # pre_daemon onboarding: catalog + first-call gate
  extensions/provision.ts # session-start provisioning reconciliation
  extensions/transcript.ts # transcript_read: index-backed conversation reads
  extensions/transcript/ # control client + transcript_read implementation
  bin/pi-rc              # client: tickets, bridge, input, reload,
                         # start/attach/detach/announce/ls/which/stop
  daemon/pi-daemon       # stdlib Python PTY host + shell ticket runners
  lib/pi_platform.py     # transport, process, PTY, terminal seams
  lib/pi_conpty.py       # Windows ConPTY backend
  lib/pi_settings.py     # the piDaemon settings reader
  lib/pi_transcript.py   # conversation index + read-only body serving
  systemd/pi-daemon.service
scripts/
  install.sh             # manifest-owned install into the agent home
  uninstall.sh
tests/                   # zero-dependency validation + OOP-enforcement suite
```

## Configuration

Tunables live in the pi settings file (`<agent-dir>/settings.json`, the
agent dir being `PI_CODING_AGENT_DIR` or `~/.pi/agent`) under the
top-level `piDaemon` object. A matching environment variable, when set,
still wins for tests and one-off runs; otherwise the setting applies,
with the built-in default as the fallback.

| Key | Default | Purpose |
|---|---|---|
| `gcIdleHours` | `24` | Detached model-idle sessions are asked to reap themselves through the `daemon_gc_reap` tool after this many hours (the reap keeps the conversation resumable), and the session-store GC discards conversations untouched this long that no live session backs. `0` disables both. |
| `daemonIdleTimeoutHours` | `1` | Self-shutdown after this many hours with nothing attached, all sessions idle, and no tickets. `0` disables. |
| `minReviveLifeSeconds` | `30` | A hosted pi that lived shorter than this is never revived. |
| `reloadGuardGraceSeconds` | `60` | No-spawn guard after an in-place reload. |
| `reloadSignalGraceSeconds` | `2.0` | Wait for an in-session reload signal before typing `/reload`. |
| `ticketTtlHours` | `72` | Finished tickets expire after this many hours. |
| `ticketGcSeconds` | `60` | Ticket garbage-collection cadence. |
| `extWatch` | `true` | Watch extension roots and reload hosted sessions on change. |
| `extWatchIntervalSeconds` | `3.0` | Extension-root poll cadence. |
| `extWatchDebounceSeconds` | `4.0` | Minimum gap between reload bursts. |
| `extWatchRoots` | agent `extensions`/`npm`/`git` + `settings.json` | Roots to watch. |
| `transcriptIndex` | `true` | Keep a positional `.jsonl.idx` sidecar beside each cold conversation so a reader can resolve entry metadata without a full parse. |
| `transcriptIndexIntervalSeconds` | `3.0` | Conversation-index poll cadence. |
| `transcriptIndexMaxMb` | `4` | Reject and rebuild an index larger than this. |
| `transcriptIndexMaxLineMb` | `16` | Longest conversation line the index will cover. |
| `offload.enabled` | `true` | Enable bash offloading; every command is still handed to the daemon immediately when on. |

Environment overrides: `PI_DAEMON_GC_IDLE_HOURS`,
`PI_DAEMON_IDLE_TIMEOUT_HOURS`, `PI_PTYD_MIN_REVIVE_LIFE`,
`PI_PTYD_RELOAD_GUARD_GRACE`, `PI_PTYD_RELOAD_SIGNAL_GRACE`,
`PI_PTYD_TICKET_TTL_HOURS`, `PI_PTYD_TICKET_GC`, `PI_PTYD_EXT_WATCH`,
`PI_PTYD_EXT_WATCH_INTERVAL`, `PI_PTYD_EXT_WATCH_DEBOUNCE`,
`PI_PTYD_EXT_WATCH_ROOTS`, `PI_PTYD_TRANSCRIPT_INDEX`,
`PI_PTYD_TRANSCRIPT_INDEX_INTERVAL`, `PI_PTYD_TRANSCRIPT_INDEX_MAX_MB`,
`PI_PTYD_TRANSCRIPT_INDEX_MAX_LINE_MB`, and `PI_OFFLOAD`.

The remote-work guard keeps `ssh` for file transfer only: a remote
shell or remote command is refused in `bash`, `powershell`, and
`daemon_tasks submit`, while `scp`, `sftp`, `rsync`, and a `cat`/`tar`
pipe over `ssh` stay allowed. It is on by default; `PI_REMOTE_GUARD=off`
or `piDaemon.remoteGuard.enabled=false` disables it (environment
precedence is the usual one). This switch is not a `/daemon-settings`
row.

`/daemon-settings` opens the same tunables in pi's two-column settings
dock: flag rows toggle in place, number and roots rows open an editor
seeded with the current value, and every accepted change is written
atomically to the `piDaemon` namespace, preserving all other keys and the
file mode. A row whose environment variable is set is marked
`(env-pinned)` because the env value still wins. The trailing
`Restore default configuration` action deletes the whole `piDaemon`
namespace after a confirmation, returning every option to its built-in
default while env-pinned options still win. Daemon-owned values
apply on the next daemon restart; `offload.*` applies after the automatic
extension reload.

The GC reaper windows (`gcIdleHours`,
`daemonIdleTimeoutHours`, `ticketTtlHours`) are configured in hours;
the daemon converts them to seconds internally. A non-finite or
negative number is rejected and the next source applies, so a
hand-written `Infinity` or `nan` cannot make a session un-reapable.
`0` disables the corresponding reaper. The session reaper never
force-kills: it writes a per-session reap request the extension
surfaces, and the session reaps itself by calling `daemon_gc_reap`. A
reap, a clean quit, and an explicit `pi-rc stop` all keep the
conversation resumable, and the daemon remembers which file a session
name backed so a later `pi-rc attach <name>` resumes that conversation
rather than the directory's latest. The session store GC is what
eventually reclaims conversations: it sweeps pi's conversation store
and discards `.jsonl` files untouched past `gcIdleHours` that no live
hosted session backs, so pi-daemon remains the owner of cleanup; a
conversation a live session holds is left to the ask-only reaper.
`/daemon-purge` (and `pi-rc daemon-purge`) is the operator's manual
force purge: it stops every detached, model-idle session past the
`gcIdleHours` window immediately and discards its conversation, while
attached and busy sessions are never purged.

## Usage

- **`pre_daemon`** - call once before `bash` when offloading is on; it
  returns the daemon catalogue and gates the offload tool.
- **`/bg`** - inside a hosted session it detaches instantly
  (`pi-rc detach`); outside hosting it hands the session over
  (`pi-rc handover`). It never aborts in-flight work and refuses
  ephemeral (`--no-session`) sessions. Ctrl+D cannot be rebound.
- **Tickets** - with offloading on, a `bash` call becomes a daemon-owned
  ticket at once; `daemon_tasks result` blocks or `watch` streams, and
  `status`, `list`, `cancel`, and `remove` manage it. A `sleep` or
  polling loop is refused. `piDaemon.offload.enabled=false` (or
  `PI_OFFLOAD=off`) makes `bash` the plain local shell. Finished
  tickets expire after `PI_PTYD_TICKET_TTL_HOURS` (default 72h), capped
  at 200.
- **`/daemon-reload`** - adopt a new daemon binary through the systemd unit
  where available, otherwise through the daemon's successor process.
- **`transcript_read`** - read the current or a named conversation
  through the daemon's positional index (`stat`, `path`, `entries`,
  `tree`, `range`); read-only, nothing appended.
- **Reload watch** - the daemon reloads hosted sessions in place when
  extension roots change; a busy session stays owed and is retried, and
  a session that dies during a reload is not respawned.

A hosted session survives client disconnect and daemon restart: when its
pi dies abnormally it is revived headless from the same conversation,
unless it lived under `minReviveLifeSeconds` (30) or was ended cleanly,
stopped, or GC-aged. `/daemon-purge` stops detached model-idle sessions
past `gcIdleHours` and discards their conversations; attached and busy
sessions are never purged.

The wrapper degrades open when the daemon is unreachable; the `PI_HOSTED`
guard keeps hosted or nested starts out of the way; and the session reaper
never force-kills - it asks the session to call `daemon_gc_reap`.

© 2026 Edrick Sinsuan. Licensed under [MIT](LICENSE).
