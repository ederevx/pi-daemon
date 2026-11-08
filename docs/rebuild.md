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
pi/service/daemon_service/sessions/error.py      SessionError
pi/service/daemon_service/sessions/identity.py   ProcessIdentity
pi/service/daemon_service/sessions/model.py      SessionRecord
pi/service/daemon_service/sessions/run_outcome.py RunOutcome
pi/service/daemon_service/sessions/store.py      SessionStore
pi/service/daemon_service/sessions/service.py    SessionService
pi/service/daemon_service/sessions/operations.py SessionOperations
pi/service/daemon_service/sessions/runner.py     SessionRunner
pi/service/daemon_service/sessions/relay.py      SessionRelay
pi/service/daemon_service/sessions/relay_session.py RelaySession
pi/service/daemon_service/sessions/pty_backend.py PosixPtyBackend
pi/service/daemon_service/sessions/pty_child.py  PtyChild
pi/service/daemon_service/sessions/pty_exec.py   PtyExec
pi/service/daemon_service/transport/control_action.py ControlAction
```

Each file owns one cohesive responsibility and one primary class. The
entry point only bootstraps: it requires `PI_DAEMON_STATE_DIR` and
`PI_DAEMON_RUNTIME_DIR`, composes `DaemonApplication`, and runs it. The
staging entry never falls back to the live roots.

`DaemonApplication` starts the instance lock, the tasks domain, the
sessions domain and the control endpoint in that order, and stops them
in reverse: close admission, stop accepting, settle sessions, cancel and
join children, persist both stores, remove the owned descriptor, release
the lock.

## The control contract

The listener speaks the published protocol, reimplemented field by
field against the installed clients:

```
client → {"cmd": "hello", "token": "<from descriptor>"}
server → {"ok": true, "srv": "pi-daemon", "proto": 1, "caps": [],
          "index": 1}
client → {"cmd": "<command>", ...}
server → {"ok": true, ...} | {"ok": false, "error": "<name>"}
```

The greeting carries exactly those keys: a malformed frame or a hello
that is not `cmd: hello` with the descriptor's token is answered
`bad-handshake`. The error names are the published ones:
`bad-handshake`, `bad-request`, `no-ticket`, `running`, `too-busy`,
`spawn-failed`, and every failure reply is built by `error_reply` in
`wire.py`, the single owner of the shape. The greeting advertises only
capability this build serves, so `caps` stays empty until the
transcript commands land; `index` stays at the published `1`.

Request coercion matches the published server, because the installed
clients rely on it: `timeout` and `offset` accept numbers and their
string forms (a `null` wait timeout is `bad-request`), `ticket-list`
accepts an empty session filter and answers the whole list, and an
unknown command is answered `bad-request` without closing the
connection.

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
  started twice, and that replay is decided before the running cap, so
  a re-sent identical command never fails `too-busy`.
* A submission that loses its record to a reset before the run is
  recorded ends its child and is answered `spawn-failed`, so no run is
  ever left untracked.
* `timeout` and `offset` refuse booleans, `nan` and `inf`, which the
  published server accepted by accident.
* A paged read clamps an offset past the end and reports the clamped
  offset, so `offset`, `next` and `size` always agree.

## The sessions domain

The domain hosts conversations, not transports. `SessionStore` owns
names, generations, conversation bindings, handoffs and run records;
`SessionRunner` is the only owner of a child process; `SessionRelay` and
its single thread own every PTY byte; `SessionService` is the surface
the operations call; `SessionOperations` validates the wire and
projects errors.

### The served contract

```
list | which | whichall | start | attach | detach | stop | input |
resize | state | run-state | announce | resume | handoff-target |
gc-reap-ack | daemon-purge | daemon-restart | shutdown
```

* `list` answers the published session view: `name`, `dir`, `pid`,
  `attached`, `clients`, `file`, `state`, `argv`; the client counts are
  read from the relay, never from the stored copy, which can lag a
  viewer that went away.
* `which` names the session a conversation is hosted by; `whichall`
  answers `names`, one `name`/`state` entry per live holder.
* `start` answers `created` (a live name is replayed, never replaced);
  the child environment is `os.environ` plus the stored `env`, plus
  `PI_DAEMON_SESSION`, plus the transient `env_once` overlay.
* `attach` answers first and then hands the socket to the relay: the
  greeting and the reply are one JSON line, everything after it is raw
  PTY bytes in both directions. A failed attach carries the recorded
  conversation `file`, so a stale name still resumes its own
  conversation. An attach to a name that handed its viewers elsewhere
  answers `handoff` instead of taking the socket.
* `state` publishes `busy`, `idle` or `waiting` with optional `seq`,
  `pid` and `aborted`; the record accepts it only from the live
  generation, only forward in `seq`, and only from the recorded `pid`.
* `run-state` answers the durable record even when the name is no longer
  hosted, with `name`, `open`, `state`, `run`, `seq`, `ended`, `history`
  and `file`.
* `announce` binds a name to a conversation; with `takeover` a duplicate
  hands its viewers to the busier holder (`handoff`) or takes the other
  holder over (`tookOver`), and always names `alsoLive`.
* `resume` moves a session's viewers to the live holder of a
  conversation; with no live holder it answers `no-session` (a fresh
  resume is the client's own `start`, which pi-rc already performs).
* `detach` closes every viewer and leaves the child running; `stop`
  asks the child to end and answers `queued`, `alive`, `state`.
* `daemon-purge` stops idle sessions with no viewer; `shutdown`
  answers before the application stops and carries the same `mode` the
  run ends with.

Deferred to the next increment, and answered `bad-request` today:
`carry`, `extensions-reload`, `handover`, `daemon-restart`, the
transcript commands and the comms capabilities. A restart is a handover
to a successor process, so promising it before a successor exists would
leave pi-rc waiting for an endpoint that never comes.

### Durable records

`<state>/sessions/sessions.json` holds the names and one record per
name: `id`, `generation`, `dir`, `argv`, `resume_argv`, `env`, `file`,
`cols`, `rows`, `desired`, `phase`, `owner`, `child`, `created`,
`started`, plus the conversation bindings and handoffs. `<state>/sessions/runs/<id>.json`
holds one run's `state`, `run`, `seq`, `seen`, `changed`, `opened`,
`open`, `ended` and `history`; the two writes are separate, so a torn
registry can never invent a verdict. A registry that cannot be read
refuses startup rather than starting empty, because an empty registry
would let a new instance mint names that are still someone's.

A verdict is decided in one place, `RunOutcome.seal`, and the order is
the contract: daemon stop, then explicit stop, then reload death, then
lost ownership, then a death mid-run (a signal or a non-zero exit) or
an exit mid-run, then a crash at the prompt, then an aborted run, then
settled, and finally `unknown` for a run that never announced itself.
`ended` is the current run's verdict and `history` holds only earlier
runs: a reopen moves the previous `ended` into `history`, so a death
and a revive never erase the death and never repeat it. A run is sealed
once; a second finish answers the first verdict unchanged.

### Process identity

A PID alone proves nothing. `ProcessIdentity` records the boot id and
the `/proc` start tick, and `classify` answers `same`, `gone` or
`unknown`; only `same` permits signalling, `gone` refuses it, and
`unknown` blocks recovery rather than guessing: a record whose process
cannot be proven gone is released but its run stays open, unsealed. A
child is recorded before it execs: the helper holds a gate until the
parent has written the record and registered the PTY, then `execvpe`s in
place, so the recorded PID is the conversation's PID. The helper closes
the gate and makes the error pipe non-inheritable, so a successful exec
closes it and the parent sees EOF instead of waiting out a timeout. An
exec failure travels back through that pipe and the start is refused as
`spawn-failed` with the child reaped. The PTY is handed to the relay at
registration and the child never closes it again.

### The relay

One thread selects over every master and viewer. Each viewer has its
own bounded queue and is dropped, not blocked on, when it falls behind.
Output produced while nobody watches is kept in one bounded backlog and
handed to the next viewer as its first chunk, so an attach never opens
on a half-drawn screen. Viewer input and resizes are queued commands, so
the request threads never touch a descriptor. On EOF the viewers keep
receiving until their queues drain, and only then are they released, so
a child's last line is never lost. `finish` waits for that drain or a
grace deadline; `move_viewers` transfers sockets and their queues,
prefixed with the target's backlog when it had no viewer, without a
byte loss. A descriptor that went bad is pruned instead of stalling the
select loop.

### Recovery and shutdown

A new instance owns no child, so every record another instance left
`running` is marked exited and its open run is sealed `lost-mid-run` or
`lost-at-prompt`; names and conversations survive, the process does not.
A record whose process is still provably the same is ended first, and
one that cannot be classified is left open as unsealed evidence.
Shutdown marks every record `stopped`, terminates each child, lets each
watcher seal its run `daemon-stopped`, drains the relay, then persists
both stores. A `reboot` mode keeps `desired: running` for the next
instance.

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
   capability over `pi-rpc/1` on its own listener. Hosting, run state,
   attach/detach, announce/handoff and recovery are in place; revive
   by name is the client's own `start`, and transcripts plus the
   deferred operations above are the remaining work in this step.
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
