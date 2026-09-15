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
  `PI_HOSTED_SESSION` in its env. This replaces tmux hosting entirely —
  there is no tmux anywhere.
- **Auto-hosting wrapper** (`pi`): a bare interactive `pi` start (no
  args, tty stdin, `PI_HOSTED` unset) is routed through `pi-rc attach`,
  so every plain start is hosted automatically. The `PI_HOSTED` guard
  keeps hosted subagent/worker sessions and nested starts out of the
  way: anything already inside the daemon execs the real pi untouched,
  as do any explicit arguments or flags and non-tty stdin. The wrapper
  resolves the real binary at install time and is manifest-owned. If
  `pi-rc` is missing, the wrapper degrades to the real pi so a bare
  start always works.
- **`rc-background` pi extension**: `/bg` inside a hosted session is an
  instantaneous detach (`pi-rc detach`): the daemon drops the client
  bridge in milliseconds — zero process churn — and the pi keeps running
  headless until reattached. Outside hosting, `/bg` hands the session
  over to the service (`pi-rc handover`): the daemon waits for the
  current pi to exit and hosts it as `pi --session <file>`, while pi
  shuts down gracefully. Ephemeral (`--no-session`) sessions are
  refused. (Ctrl+D cannot be rebound: pi refuses extension shortcuts
  that conflict with its built-in `app.exit` Ctrl+D binding.)
- **Session survival**: pi sessions live on disk regardless of
  processes. When a hosted pi is gone (reboot, daemon restart), the
  daemon respawns it from its registry, and `pi-rc attach` or
  `pi-rc start` resumes the latest session for that directory (`pi -c`),
  falling back to a fresh pi; pass `--fresh` to force an empty session.
- **Official subagent extension** (`pi/install-subagent.sh`): installs the
  subagent extension shipped with pi itself using pi's documented symlink
  method, with sample agents converted to local copies that inherit the
  session model.

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
  bin/pi-rc                         # client: start/attach/detach/ls/stop/handover
  ptyd/pi-ptyd                      # stdlib Python PTY host daemon
  systemd/pi-background-service.service
  install-subagent.sh               # official subagent extension installer
scripts/
  install.sh                        # manifest-owned install into the agent home
  uninstall.sh
```

## Maintenance conventions

Same as the sibling protocol repos: work on a feature branch, validate, land
through a PR, cut the next tag on the merged HEAD, and reinstall the host
from that tag before relying on the change.

© 2026 Edrick Sinsuan. Licensed under [CC BY 4.0](LICENSE).
