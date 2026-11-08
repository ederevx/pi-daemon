# The unified service: framework and migration

This branch builds one service from scratch: one process, one control
contract, and every domain owned by an object instead of a module. No
implementation from the old daemon or from pi-teams is imported; the
existing public contract is reimplemented against the same wire, so the
installed clients keep working when the entry point switches.

## What exists now

The staging tree lives beside the installed one until the switch:

```
pi/service/pi-daemon                     staging entry (bootstrap only)
pi/service/daemon_service/application.py  DaemonApplication
pi/service/daemon_service/config.py       ServiceConfig
pi/service/daemon_service/runtime_layout.py     RuntimeLayout
pi/service/daemon_service/atomic_state_file.py  AtomicStateFile
pi/service/daemon_service/wire.py               WireCodec (pi-daemon/1)
pi/service/daemon_service/lifecycle/instance_coordinator.py
pi/service/daemon_service/transport/json_line_stream.py
pi/service/daemon_service/transport/endpoint_file.py
pi/service/daemon_service/transport/endpoint_probe.py
pi/service/daemon_service/transport/control_handshake.py
pi/service/daemon_service/transport/control_endpoint.py
pi/service/daemon_service/transport/control_router.py
pi/service/daemon_service/tasks/error.py    TaskError
pi/service/daemon_service/tasks/model.py    Ticket
pi/service/daemon_service/tasks/store.py    TicketStore
pi/service/daemon_service/tasks/runner.py   TicketRunner
pi/service/daemon_service/tasks/service.py  TaskService
pi/service/daemon_service/tasks/operations.py   TaskOperations
```

Each file owns one cohesive responsibility and one primary class. The
entry point only bootstraps: it requires `PI_DAEMON_STATE_DIR` and
`PI_DAEMON_RUNTIME_DIR`, composes `DaemonApplication`, and runs it. The
staging entry never falls back to the live roots.

`DaemonApplication` starts the instance lock, the tasks domain and the
control endpoint in that order, and stops them in reverse: close
admission, stop accepting, cancel and join children, persist, remove
the owned descriptor, release the lock.

## The control contract

The listener speaks the published protocol, reimplemented field by
field against the installed clients:

```
client → {"cmd": "hello", "token": "<from descriptor>"}
server → {"ok": true, "srv": "pi-daemon", "proto": 1, "caps": [],
          "index": 1, "commands": ["ticket-cancel", ...]}
client → {"cmd": "<command>", ...}
server → {"ok": true, ...} | {"ok": false, "error": "<name>"}
```

The error names are the published ones: `bad-handshake`,
`bad-request`, `no-ticket`, `running`, `too-busy`, `spawn-failed`.
The greeting advertises only capability this build serves, so `caps`
stays empty until the transcript commands land; `commands` names the
dispatch table.

The ticket records use the published field names: `id` (never
`ticket_id`), `exit` (never `exit_code`), `finished` (never `ended`),
plus `status`, `pid`, `started`, `output_bytes`, `truncated`, `error`.
A submission answers `{"ok": true, "id", "ticket"}` and adds
`"reused": true` when it joins an identical running ticket. A wait and
a cancel answer `{"ok": true, "ticket": ...}`. Output has two modes:
without `offset` it answers the whole text as `output`, with `offset`
it answers raw bytes as base64 `data` plus `offset`, `next` and `size`,
so a page boundary never splits or rescales a character. `env` is
transient per submission and is never stored in a record.

* Frames are one JSON object per line, bounded by `frame_bytes`, and a
  connection's buffer survives between frames.
* The token is minted per instance, published only in the runtime
  descriptor (`control.json`, mode `0600`), and compared in constant
  time.
* The descriptor names its instance; only that instance removes it.
* A ticket that exceeds its timeout ends `failed` with `timed out`,
  never a new status.
* A running ticket that matches a new submission is replayed, not
  started twice.

## Configuration

Every operational value is a `ServiceConfig` field, resolved once from
environment (`PI_DAEMON_<FIELD>`) over the settings file over the
default, and invalid values fall through to the next source. Booleans
and non-finite numbers are refused, and zero is a value, not "unset".

There are no legacy names, no `PI_PTYD_*` fallbacks and no pi-teams
aliases: migration translates configuration once, in the migration
step, and the new service only reads its own names.

## Domain boundaries

| Domain | Owns | Never |
|---|---|---|
| tasks | tickets, IDs, output logs, child execution | session, comms or peer state |
| transport | framing, descriptor, accepted connections | domain records |
| lifecycle | instance lock and generation | readiness decisions |
| application | composition and start/stop order | domain dictionaries or locks |

Cross-domain access is through public methods that return detached
snapshots. The store is the sole owner of ticket records: the runner
receives IDs and keeps process bookkeeping only.

## The migration

Order matters, and each step is reversible on its own:

1. Ship the new package beside the installed one (`pi/service/`), with
   tests in `tests/run.py`. Nothing installed changes.
2. Bring up the staging entry with explicit roots and verify the
   control contract, tickets, restart recovery and clean shutdown.
3. Add the remaining domains against the same composition: sessions
   (session hosting, run state, revive, transcripts), then comms
   (registry, relay, mailbox, waits, peers) as the `agent-comms`
   capability over `pi-rpc/1` on its own listener.
4. Ship the new client beside the old one and rewire `pi-rc` and the
   extensions to it, keeping it compatible with both servers, then
   remove the legacy aliases from the migration translator.
5. Translate state once: read the old ticket and session files, write
   the new layout, and leave the old files untouched for one release.
6. Switch the entry point only after the clients and the installer
   land: `pi/daemon/pi-daemon` becomes the thin bootstrap, the
   implementation moves to `pi/lib/pi_daemon/`, and the old monolith
   and the standalone broker are deleted in the same commit. No
   adapter is written between old and new code, so the switch is the
   last commit that can be rolled back without a client change.
7. Update the installer for the private tree (`lib/`, `bin/`,
   `daemon/`, `extensions/`), the unit files and the docs.

## Rollback

Rollback never depends on the new code. Every step keeps the previous
release installed and its state files in place, so rollback is: stop
the new service, restore the previous entry point and launchers, start
the old service, and leave the new state directory in place as
evidence. The instance lock and the runtime descriptor are per-root, so
the two implementations never share a runtime directory.

## Open work

* Sessions, run state, revive, handoff and transcripts.
* Native comms: registry, relay, mailbox, waits, peers, and the
  `agent-comms` RPC listener.
* Extension and `pi-rc` rewiring; installer and migration tooling.
* Discovery: `ServiceDirectory`, activation, binding and self
  provisioning, which the descriptor work above prepares for.
