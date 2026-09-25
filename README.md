# pi-daemon

Persistent backgrounding for the Pi coding agent, maintained like
the sibling pi extension packages: a private repo whose
installer copies manifest-owned resources into the Pi agent home, records
them, and can uninstall exactly what it installed.

## What it provides

- **PTY host daemon** (`pi-daemon`): a stdlib-only Python daemon, run by the
  systemd user service, that owns one PTY per hosted pi TUI session.
  Sessions survive client disconnect and a daemon restart (respawned from
  a state registry); every hosted child gets `PI_HOSTED` and
  `PI_HOSTED_SESSION` in its env. While a session is detached, the daemon
  drains the PTY and discards output, so a headless agent can keep
  working without ever blocking on a full tty buffer. This replaces tmux
  hosting entirely — there is no tmux anywhere.
- **Auto-hosting wrapper** (`pi`): a plain interactive `pi` call attaches
  to the directory's daemon-owned session — the live hosted session
  backing this directory's latest conversation — so terminals become
  interchangeable viewports of one daemon-owned session instead of
  spawning duplicates; the daemon owns the sessions, the terminal just
  views. `pi --new` (a wrapper flag, consumed before pi sees it) keeps
  the old behavior: a fresh conversation in its own new hosted session
  through `pi-rc attach --new`. The owned-session scan only considers
  sessions with a real conversation file on record, so `--no-session`
  children (one-shot workers share the directory in the listing) are
  never attached. Resume flags (--resume / --continue) ride along into
  the hosted session as pi args. Before attaching, the wrapper resolves
  the target conversation file of a
  `--resume` / `--continue` / `--session` call (without spawning pi) and,
  when a live hosted session already backs it, ATTACHES to that session
  instead of starting a second pi — entering an active session is not an
  interruption, and only explicit user actions (the detach key, Ctrl-\)
  ever touch the running model. The interactive `--resume` picker cannot
  be resolved before pi opens; when it still opens a conversation a live
  session already backs, the duplicate yields to a busy holder (its
  viewers follow the work) or absorbs idle-at-prompt holders (it keeps
  the conversation, their views follow it), so no two live pis ever
  run on one file. `/new` inside a hosted session is carried, not
  terminated: the extension cancels pi's in-process switch (which would
  abort an in-flight turn), the daemon spawns a fresh hosted session and
  moves the terminal's bridge onto it, and the old session keeps running
  headless with its conversation. If the attach itself fails (e.g. the
  session died between check and attach), the wrapper degrades open with
  the original args. An unreachable daemon, unresolvable target, or no
  match degrades open exactly as before, and plain `pi` starts are
  unaffected. One-shot invocations (`-p`/`--print`, `--no-session`),
  help/version, and non-tty stdin stay direct. The `PI_HOSTED` guard
  keeps hosted one-shot/worker sessions and nested starts out of the way:
  anything already inside the daemon execs the real pi untouched. The
  wrapper resolves the real binary at install time and is manifest-owned.
  If the service is down, the wrapper starts it first (the systemd user
  unit where installed, otherwise a detached spawn) so the very start
  that brought the daemon up is hosted immediately — no manual `/bg`
  then reenter. If `pi-rc` is missing or the daemon still cannot come
  up, the wrapper degrades to the real pi so a start always works.
- **`daemon` pi extension**: `/bg` runs the moment it is entered,
  even while the agent is mid-turn (pi executes extension commands
  immediately). Inside a hosted session it is an instantaneous detach
  (`pi-rc detach`): the daemon drops the client bridge in milliseconds —
  zero process churn — and the pi keeps running headless until
  reattached; no continuation prompt is sent, so a backgrounded session
  idles once its in-flight work settles. Outside hosting, `/bg` hands
  the session over to the service (`pi-rc handover`): the daemon waits
  for the current pi to exit and hosts it as `pi --session <file>`.
  `/bg` never aborts a running operation: if the agent is mid-turn, pi
  prints a notice, waits for the work to settle (retries and queued
  follow-ups included), then shuts down gracefully. Ephemeral
  (`--no-session`) sessions are refused. (Ctrl+D cannot be rebound:
  pi refuses extension shortcuts that conflict with its built-in
  `app.exit` Ctrl+D binding.) The
  extension also announces each session's file to the daemon so abnormal
  deaths can be revived from the same conversation.
- **Extension reload watch**: the daemon polls the extension roots
  (extensions, skills, prompts, themes, context files); on change it
  signals a reload to every hosted conversation session so pi re-scans
  its loaded resources in place, and persists the file-level diff for
  the in-session daemon extension to relay to the agent once it
  settles. The signal is direct: the daemon writes a one-shot
  token-stamped per-session signal file under the daemon state dir and
  the in-session daemon extension (watching for it) consumes the
  signal and runs pi's own reload flow programmatically (`ctx.reload()`
  — the same flow as a typed `/reload`, with no keystrokes entering
  the TUI); a signal an extension without the watcher does not consume
  within a short grace is deleted again and the daemon falls back to
  typing `/reload` into the session's PTY the old way.
  Sessions that are not idle are skipped for the round instead (busy
  mid-turn, or parked on a blocking daemon wait `waiting`) since pi
  would deny a reload in either state; each skipped session stays owed,
  is retried every poll until it goes idle, and the diff is stamped
  only once every owed session was reloaded or is gone (never dropped,
  with newer edits coalescing into the pending diff). A manual
  `pi-rc extensions-reload` follows the same deferral and the watch
  finishes its busy sessions. The reload is strictly in-place: it only signals
  sessions that already exist and never starts or revives one, and a
  session that dies while processing the reload stays dead
  instead of being respawned, so a broken update cannot cascade into a
  spawn storm.
- **Command offloading** (`offload` pi extension): every `bash` call is
  rerouted to the daemon as a **ticket** — a daemon-owned shell command
  that outlives the submitting agent. Results are delivered in one go
  when the command finishes (nothing streams into the tool result), so
  an agent reads a complete result exactly once. A command outliving
  the wait bound (the tool's timeout, or `piDaemon.offload.waitSeconds`
  / `PI_OFFLOAD_WAIT`, default 120) is handed off instead of killed: the tool result frees
  the agent immediately, the ticket keeps running daemon-side, and the
  full output is delivered as a follow-up message on completion. Every
  ticket is recorded in the daemon's persisted `tickets.json` and
  assigned to the owning session (hosted session name, else the
  conversation file stem), so `daemon_tasks list` shows them and a
  crashed or restarted session re-arms its pending results on
  `session_start`. The ticket id carries a short session segment
  (`t-<seg>-<n>`), so a task names the session it belongs to at a
  glance without a separate lookup. The `daemon_tasks` tool exposes the machinery
  explicitly: `submit` (fire-and-forget background command), `result`
  (one-go fetch, or an active interruptible wait with `wait` that
  returns as soon as the ticket finishes and yields early when a user
  message is queued), `watch` (live output via partial updates), and
  `status`, `list`, `cancel`, `remove`, and `reset`. When the daemon is
  bash tool falls back to pi's local execution transparently, and
  `piDaemon.offload.enabled=false` (or `PI_OFFLOAD=off`) disables
  offloading entirely. Tickets are garbage
  collected by the daemon: finished tickets expire after
  `PI_PTYD_TICKET_TTL_HOURS` hours (default 72h) with the finished set
  capped at 200 (oldest evicted), and each sweep unlinks the expired
  output logs; a daemon restart or shutdown marks running tickets
  `lost` instead of ever claiming a live process.
- **Always backgrounded**: a hosted session never dies silently. When
  its pi process dies abnormally (crash, SIGKILL, OOM), the daemon
  revives it headless as `pi --session <file>` under the same name. Only
  a clean quit (Ctrl+D or the `/quit` command), an explicit `pi-rc stop`, a daemon
  shutdown, or a deleted session file ends one for good, and a revived
  pi that dies again within 30 seconds is left dead so a crash loop
  cannot spin the daemon.
- **Session survival**: pi sessions live on disk regardless of
  processes. When a hosted pi is gone (reboot, daemon restart), the
  daemon respawns it from its registry, and `pi-rc attach` or
  `pi-rc start` resumes the latest session for that directory (`pi -c`),
  falling back to a fresh pi; pass `--fresh` to force an empty session.

## Install

### As a pi package

```bash
pi install git:github.com/ederevx/pi-daemon@v2.9.9
```

`npm install` runs `scripts/postinstall.mjs`, which provisions the
systemd user unit, the `pi-rc` client, the `pi` wrapper, the daemon, and
the seam modules to the same manual paths, while the extensions load
from the package clone. Updating the pinned ref re-runs it. Extension
copies a prior manual install owned are removed so only one loader
source remains. Restart the service to adopt a new daemon binary:
`systemctl --user restart pi-daemon`.

### Manually

```bash
bash scripts/install.sh
```

Re-run to refresh owned copies in place. Requires `pi` on PATH and
`python3` (>= 3.8, stdlib-only) for the pi-daemon daemon and uninstall's
manifest reading.

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
  bin/pi-rc              # client: tickets, bridge, input, reload,
                         # start/attach/detach/announce/ls/which/stop
  daemon/pi-daemon       # stdlib Python PTY host + shell ticket runners
  lib/pi_platform.py     # transport, process, PTY, terminal seams
  lib/pi_conpty.py       # Windows ConPTY backend
  lib/pi_settings.py     # the piDaemon settings reader
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
| `gcIdleHours` | `3` | Detached model-idle sessions are asked to reap themselves through the `daemon_gc_reap` tool after this many hours. `0` disables. |
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
| `offload.enabled` | `true` | Enable bash offloading. |
| `offload.waitSeconds` | `120` | Hand-off bound before a command becomes a background ticket. |

Environment overrides: `PI_DAEMON_GC_IDLE_HOURS`,
`PI_DAEMON_IDLE_TIMEOUT_HOURS`, `PI_PTYD_MIN_REVIVE_LIFE`,
`PI_PTYD_RELOAD_GUARD_GRACE`, `PI_PTYD_RELOAD_SIGNAL_GRACE`,
`PI_PTYD_TICKET_TTL_HOURS`, `PI_PTYD_TICKET_GC`, `PI_PTYD_EXT_WATCH`,
`PI_PTYD_EXT_WATCH_INTERVAL`, `PI_PTYD_EXT_WATCH_DEBOUNCE`,
`PI_PTYD_EXT_WATCH_ROOTS`, `PI_OFFLOAD`, and `PI_OFFLOAD_WAIT`.

`/daemon-settings` opens the same tunables in pi's two-column settings
dock: flag rows toggle in place, number and roots rows open an editor
seeded with the current value, and every accepted change is written
atomically to the `piDaemon` namespace, preserving all other keys and the
file mode. A row whose environment variable is set is marked
`(env-pinned)` because the env value still wins. Daemon-owned values
apply on the next daemon restart; `offload.*` applies after the automatic
extension reload.

The GC reaper windows (`gcIdleHours`,
`daemonIdleTimeoutHours`, `ticketTtlHours`) are configured in hours;
the daemon converts them to seconds internally. A non-finite or
negative number is rejected and the next source applies, so a
hand-written `Infinity` or `nan` cannot make a session un-reapable.
`0` disables the corresponding reaper. The session reaper never
force-kills: it writes a per-session reap request the extension
surfaces, and the session reaps itself by calling `daemon_gc_reap`.
`/daemon-purge` (and `pi-rc daemon-purge`) is the operator's manual
force purge: it stops every detached, model-idle session past the
`gcIdleHours` window immediately, with the same consented-stop
semantics as `daemon_gc_reap` (the conversation is discarded), while
attached and busy sessions are never purged.

## Maintenance conventions

Same as the sibling protocol repos: work on a feature branch, validate, land
through a PR, cut the next tag on the merged HEAD, and reinstall the host
from that tag before relying on the change.

© 2026 Edrick Sinsuan. Licensed under [MIT](LICENSE).
