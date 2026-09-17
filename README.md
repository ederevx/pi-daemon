# pi-daemon

Persistent backgrounding for the Pi coding agent, maintained like
`agent-delegation-protocol` and `agent-mem-struct`: a private repo whose
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
  children (subagent workers share the directory in the listing) are
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
  keeps hosted subagent/worker sessions and nested starts out of the way:
  anything already inside the daemon execs the real pi untouched. The
  wrapper resolves the real binary at install time and is manifest-owned.
  If `pi-rc` is missing or the service is down, the wrapper degrades to
  the real pi so a start always works.
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
  (`--no-session`) sessions are refused. (Ctrl+D cannot be rebound: pi refuses extension shortcuts
  that conflict with its built-in `app.exit` Ctrl+D binding.) The
  extension also announces each session's file to the daemon so abnormal
  deaths can be revived from the same conversation.
- **Command offloading** (`offload` pi extension): every `bash` call is
  rerouted to the daemon as a **ticket** — a daemon-owned shell command
  that outlives the submitting agent. Results are delivered in one go
  when the command finishes (nothing streams into the tool result), so
  an agent reads a complete result exactly once. A command outliving
  the wait bound (the tool's timeout, or `PI_OFFLOAD_WAIT` seconds,
  default 120) is handed off instead of killed: the tool result frees
  the agent immediately, the ticket keeps running daemon-side, and the
  full output is delivered as a follow-up message on completion. Every
  ticket is recorded in the daemon's persisted `tickets.json` and
  assigned to the owning session (hosted session name, else the
  conversation file stem), so `daemon_tasks list` shows them and a
  crashed or restarted session re-arms its pending results on
  `session_start`. The `daemon_tasks` tool exposes the machinery
  explicitly: `submit` (fire-and-forget background command), `result`
  (one-go fetch), `watch` (live output via partial updates), plus
  `status`, `list` and `cancel`. When the daemon is unreachable the
  bash tool falls back to pi's local execution transparently, and
  `PI_OFFLOAD=off` disables offloading entirely. Tickets are garbage
  collected by the daemon: finished tickets expire after
  `PI_PTYD_TICKET_TTL` seconds (default 24h) with the finished set
  capped at 200 (oldest evicted), and each sweep unlinks the expired
  output logs; a daemon restart or shutdown marks running tickets
  `lost` instead of ever claiming a live process.
- **Always backgrounded**: a hosted session never dies silently. When
  its pi process dies abnormally (crash, SIGKILL, OOM), the daemon
  revives it headless as `pi --session <file>` under the same name. Only
  a clean quit (Ctrl+D or `/exit`), an explicit `pi-rc stop`, a daemon
  shutdown, or a deleted session file ends one for good, and a revived
  pi that dies again within 30 seconds is left dead so a crash loop
  cannot spin the daemon.
- **Session survival**: pi sessions live on disk regardless of
  processes. When a hosted pi is gone (reboot, daemon restart), the
  daemon respawns it from its registry, and `pi-rc attach` or
  `pi-rc start` resumes the latest session for that directory (`pi -c`),
  falling back to a fresh pi; pass `--fresh` to force an empty session.

## Install

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
  extensions/daemon.ts       # /bg: instant detach when hosted, handover otherwise
  extensions/offload.ts      # ticket-based command offloading + daemon_tasks tool
  bin/pi-rc                         # client: start/attach/detach/announce/ls/which/stop/handover/tickets
  daemon/pi-daemon                      # stdlib Python PTY host daemon
  systemd/pi-daemon.service
scripts/
  install.sh                        # manifest-owned install into the agent home
  uninstall.sh
```

## Maintenance conventions

Same as the sibling protocol repos: work on a feature branch, validate, land
through a PR, cut the next tag on the merged HEAD, and reinstall the host
from that tag before relying on the change.

© 2026 Edrick Sinsuan. Licensed under [CC BY 4.0](LICENSE).
