# pi-background-service

Persistent backgrounding for the Pi coding agent, maintained like
the sibling pi extension packages: a private repo whose
installer copies manifest-owned resources into the Pi agent home, records
them, and can uninstall exactly what it installed.

## What it provides

- **PTY host daemon** (`pi-ptyd`): a stdlib-only Python daemon, run by the
  systemd user service, that owns one PTY per hosted pi TUI session.
  Sessions survive client disconnect and a daemon restart (respawned from
  a state registry); every hosted child gets `PI_HOSTED` and
  `PI_HOSTED_SESSION` in its env. While a session is detached, the daemon
  drains the PTY and discards output, so a headless agent can keep
  working without ever blocking on a full tty buffer. This replaces tmux
  hosting entirely — there is no tmux anywhere.
- **Auto-hosting wrapper** (`pi`): every interactive `pi` call creates its
  OWN new hosted session through `pi-rc attach --new` — a fresh
  conversation under a unique session name, attached to the terminal.
  Nothing is resumed implicitly; pass `--resume` / `--continue` to pi
  explicitly when an old conversation is wanted (they ride along into the
  hosted session). Backgrounding stays solely a `/bg` feature: before
  attaching, the wrapper resolves the target conversation file of a
  `--resume` / `--continue` / `--session` call (without spawning pi) and,
  when a live hosted session already backs it, ATTACHES to that session
  instead of starting a second pi — entering an active session is not an
  interruption, and only explicit user actions (the detach key, Ctrl-\)
  ever touch the running model. The interactive `--resume` picker cannot
  be resolved before pi opens; when it still opens a conversation a live
  session already backs, the duplicate hands its viewers to the live
  holder (a busy one first) and shuts down, so the terminal lands on the
  live view. If the attach itself fails (e.g. the
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
- **`rc-background` pi extension**: `/bg` runs the moment it is entered,
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
`python3` (>= 3.8, stdlib-only) for the pi-ptyd daemon and uninstall's
manifest reading.

## Uninstall

```bash
bash scripts/uninstall.sh
```

## Layout

```
pi/
  extensions/rc-background.ts       # /bg: instant detach when hosted, handover otherwise
  bin/pi-rc                         # client: start/attach/detach/announce/ls/which/stop/handover
  ptyd/pi-ptyd                      # stdlib Python PTY host daemon
  systemd/pi-background-service.service
scripts/
  install.sh                        # manifest-owned install into the agent home
  uninstall.sh
```

## Maintenance conventions

Same as the sibling protocol repos: work on a feature branch, validate, land
through a PR, cut the next tag on the merged HEAD, and reinstall the host
from that tag before relying on the change.

© 2026 Edrick Sinsuan. Licensed under [CC BY 4.0](LICENSE).
