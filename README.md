# pi-background-service

Persistent backgrounding for the Pi coding agent, maintained like
the sibling pi extension packages: a private repo whose
installer copies manifest-owned resources into the Pi agent home, records
them, and can uninstall exactly what it installed.

## What it provides

- **Hosted pi sessions** (`pi-rc`): a systemd user service owns a persistent
  tmux server that hosts detached pi TUI sessions. Detaching (`/bg` inside a
  hosted pi) backgrounds the session; the pi process keeps
  running until it exits or its session is deleted through pi's own session
  manager. Survives terminal exit, SSH logout, and reboot (user linger).
- **`rc-background` pi extension**: the `/bg` command detaches the tmux
  client when pi runs hosted. (Ctrl+D cannot be rebound: pi refuses
  extension shortcuts that conflict with its built-in `app.exit` Ctrl+D
  binding.)
- **Session survival**: pi sessions live on disk regardless of processes.
  When a hosted pi is gone (reboot, server restart), the next `pi-rc attach`
  or `pi-rc start` recreates it and resumes the latest session for that
  directory (`pi -c`), falling back to a fresh pi; pass `--fresh` to force
  an empty session.
- **Official subagent extension** (`pi/install-subagent.sh`): installs the
  subagent extension shipped with pi itself using pi's documented symlink
  method, with sample agents converted to local copies that inherit the
  session model.

## Install

```bash
bash scripts/install.sh
```

Re-run to refresh owned copies in place. Requires `pi` and `tmux` on PATH
and `python3` for uninstall's manifest reading.

## Uninstall

```bash
bash scripts/uninstall.sh
```

## Layout

```
pi/
  extensions/rc-background.ts       # /bg + hosted Ctrl+D detach
  bin/pi-rc                         # launcher (start/attach/ls/stop)
  tmux/pi-rc.conf                   # session-host tmux config
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
