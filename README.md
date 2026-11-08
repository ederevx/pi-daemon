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
- **Agent comms** - `pi-comms` and the `comms_*` tools mail another
  agent identity on this node or over a peer link, wait for mail, and
  launch another agent as a hosted session.
- **Extension service contract** - `docs/pi-services.md` defines
  `pi-rpc/1`, the shared contract for one extension to call another or
  subscribe to its signals.

## Install

### As a pi package

The package is the only install: it loads the extensions from its own
clone, and a bare `bash scripts/install.sh` writes nothing.

```bash
pi install git:github.com/ederevx/pi-daemon@v2.9.59
```

`npm install` runs `scripts/postinstall.mjs` on a fresh install, and the
package's `provision` extension reconciles on every session start: when
the clone's sources differ from what the install manifest recorded, it
re-runs `scripts/install.sh --package` in the background. Together they
provision the systemd user unit, the `pi-rc` and `pi-comms` clients, the
`pi` wrapper, the daemon, and the seam modules to the same manual paths.
pi skips `npm install` when an updated pin resolves to the commit already
checked out, so the session-start reconciliation is what applies such an
update. Provisioning removes extension copies a prior manual install owned
so only one loader source remains; if a leftover copy aborts sessions with
tool-conflict errors before that can happen, run
`bash scripts/install.sh --package`. Adopt a new daemon binary with
`/daemon-reload`.

## Agent comms

```bash
pi-comms node                       # this node's identity and endpoints
pi-comms send --to reviewer --text "the rebuild branch is green"
pi-comms wait --timeout-ms 600000   # the next mail for my identity
```

The identity is `--agent` or `PI_COMMS_AGENT_ID`; there is no default,
because binding an identity creates its mailbox. Exit codes: 0 ok, 1
usage, 2 the service declined, 3 the wait timed out, 4 no service
(nothing was sent), 5 a JSON-RPC error, 6 the token was refused, 7 the
request may have been acted on (retry with the same `--request-id`). The
extension exposes the same surface as
`comms_send`, `comms_wait`, `comms_replay`, `comms_ack`, `comms_spawn`
and `peers`, behind the `pre_daemon` gate, and shells out to
`pi-comms --format json` (`PI_COMMS_BIN` overrides the binary).

The client finds the service by capability, not by path: it reads the
`agent-comms` descriptor from the services root (`PI_SERVICES_DIR`,
otherwise the user runtime directory, otherwise the temp directory), and
reports "no service" when nothing published one. The installed daemon is
still the previous implementation, so until the service entry point
switches, `pi-comms` and the comms tools answer `no service` unless a
staging service runs with explicit roots.

## Uninstall

```bash
pi remove github.com/ederevx/pi-daemon
bash scripts/uninstall.sh
```

Remove the pin first: with the manifest gone and the package still
pinned, the next session start sees a stale clone and provisions again.

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
  extensions/comms/      # comms_* + peers tools over the pi-comms CLI
  extensions/services/   # session-host discovery for the control endpoint
  bin/pi-rc              # client: tickets, bridge, input, reload,
                         # start/attach/detach/announce/ls/which/stop
  bin/pi-comms           # agent-comms client: mail, waits, launches, peers
  daemon/pi-daemon       # stdlib Python PTY host + shell ticket runners
  lib/pi_platform.py     # transport, process, PTY, terminal seams
  lib/pi_services.py     # service descriptors: discovery, publish, sweep
  lib/pi_rpc_client.py   # pi-rpc/1 socket client: discover, greet, call
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
| `runStateRetentionHours` | `24` | Keep a closed run-outcome record this long; `0` is not special here, because a disabled idle policy must not stop this cleanup. |
| `runBeatSeconds` | `10` | How often the daemon re-reads its run records to look for a stalled run. |
| `runBeatStaleSeconds` | `45` | No progress for this long flags a `busy` run as suspect, once, and never stops it. |

Environment overrides: `PI_DAEMON_GC_IDLE_HOURS`,
`PI_DAEMON_IDLE_TIMEOUT_HOURS`, `PI_PTYD_MIN_REVIVE_LIFE`,
`PI_PTYD_RELOAD_GUARD_GRACE`, `PI_PTYD_RELOAD_SIGNAL_GRACE`,
`PI_PTYD_TICKET_TTL_HOURS`, `PI_PTYD_TICKET_GC`, `PI_PTYD_EXT_WATCH`,
`PI_PTYD_EXT_WATCH_INTERVAL`, `PI_PTYD_EXT_WATCH_DEBOUNCE`,
`PI_PTYD_EXT_WATCH_ROOTS`, `PI_PTYD_TRANSCRIPT_INDEX`,
`PI_PTYD_TRANSCRIPT_INDEX_INTERVAL`, `PI_PTYD_TRANSCRIPT_INDEX_MAX_MB`,
`PI_PTYD_TRANSCRIPT_INDEX_MAX_LINE_MB`, `PI_PTYD_RUN_STATE_RETENTION_HOURS`,
`PI_PTYD_RUN_BEAT`, `PI_PTYD_RUN_BEAT_STALE`, and `PI_OFFLOAD`.

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
- **`pi-rc run-state <name>`** - how that session's last run ended:
  `verdict`, `cause`, `state`. The daemon, not the session, keeps the
  record (`run-state/<name>.json`), and it is written from the wire
  endpoint the extension publishes to (`pi-rc state ... --seq N
  [--aborted] --pid N`), so the recorded state - not the exit status -
  decides whether a run that stopped looks finished. The publish is
  best-effort: one that never reached the daemon leaves the record at its
  last state, and the session's conversation is then the only other
  evidence. The pid names the publishing process, and a state from a child
  the record was not opened for is refused: the client a session launches
  can outlive the pi that launched it.
  `settled` (idle published, clean exit), `run-aborted` (the user
  interrupted it), `exited-mid-run` (the process went away
  mid-turn but exited 0 - including a handled `SIGTERM`/`SIGHUP`, which is
  pi's own signal path: it disposes the runtime, then exits 0) and
  `died-mid-run` (a nonzero exit or an uncaught signal), `lost-mid-run`
  (the daemon or its host died and no exit was seen), `lost-at-prompt`
  (the same, with the record idle), `crashed-at-prompt`, `reload-death`,
  `daemon-stopped`, `stopped`, and `unknown` (nothing was ever
  published). A live session prints `running <name> <state>` and then the
  last seal it reopened from; a record left open by a child this daemon
  does not host prints `unsealed <name> <state>` instead of pretending
  the session runs. A run whose record stays `busy` or `waiting` past
  `PI_PTYD_RUN_BEAT_STALE` (45s, polled every `PI_PTYD_RUN_BEAT` seconds)
  is logged as suspect once, and is never stopped: the record reports, it
  does not police. A reopen (a revive, a respawn at boot, a daemon
  restart) carries the last few seals forward, so a death survives the
  session that inherits its name; the newest line is the last run, and
  every seal carried forward follows it as a `previous <verdict> <cause>
  <state>` line - a death, a revive and then an exit at the prompt still
  shows the death.

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
