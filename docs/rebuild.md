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
`unknown` for a run that never announced itself, and otherwise settled.
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

A comms setting whose zero would stop mail rather than disable a
feature is refused, so it falls through to its next source. The comms
sizes also have to agree with each other: a message TTL may not exceed
the maximum TTL, a payload plus the reply margin must fit one frame, and
the reply margin must be at least the agent-id limit plus 256 bytes.

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

## Agent comms

Comms is a message channel between agent identities plus a way for an
agent to spawn another one. There is no roster: no `register`,
`unregister` or `list`, no membership snapshots, no live directory and
no grant model. An identity exists because a mailbox exists, and the
mailbox store is the only record of it.

Every client shares the daemon's token, so the token is the trust
boundary: a presented agent ID selects a mailbox, it does not prove
ownership of it. A connection presents its own saved ID at `rpc.hello`
and is bound to that identity for its life; a second connection with
the same ID shares the same mailbox and acknowledgement state, and
disconnecting one revokes nothing of the other.

Comms speaks `pi-rpc/1` on its own loopback listener. A connection
presents `{token, version, agent}` at `rpc.hello` and is bound to that
agent for its life; the greeting returns the methods, the subscribeable
topics (`comms.message`), the bound agent, its mailbox cursor and the
frame limit. The published methods are `comms.send`, `comms.broadcast`,
`comms.replay`, `comms.ack` and `comms.wait`, plus `comms.spawn` when the
daemon composes a launcher, over the profile's `rpc.subscribe`,
`rpc.unsubscribe` and `rpc.cancel`; there is no `comms.list`,
`comms.register` or `comms.unregister`.

Live notifications are hints, never the record: mail is delivered to a
subscribed connection in sequence order, and a subscriber that falls
behind its bounded queue is disconnected rather than served a gap, so it
reconnects and replays from its own cursor.

This node has one identity, `nodeId`, minted on first use and kept in
the state root beside the mailbox store; it is what agents put in an
address, and an unreadable or empty `node.json` refuses startup rather
than minting a second name. A direct address that names another node is
queued for the peer that reaches that node; when no configured peer
reaches it the send answers `undeliverable` with reason `no-such-peer`,
and a broadcast never leaves the node: an address it cannot reach is
reported with reason `remote-node`.

### Discovery

The daemon publishes `agent-comms/pi-daemon.json` under the services
root (`$PI_SERVICES_DIR`, else the user runtime directory), naming the
comms endpoint file and `pi-rc daemon-start` as its activation, so a
missing entry is a service to start rather than a failure to report. The
listener is bound and answering before the entry is published, so an
entry never names a port that does not greet, and the entry is removed
only while both its `instance` and its `pid` still match this process: a
successor that has already published its own entry keeps it when the
first daemon stops.

Activation exists but is not taken by the CLI: `pi-comms` reports the
missing service and exits 4 rather than starting a daemon behind the
user's back, and binding is explicit. `session-host` keeps its own
`pi-pty-host/1` entry beside it.

The daemon client resolves the control endpoint the same way on both
sides of the repo. `PI_DAEMON_ENDPOINT` (or a leading `--endpoint PATH`
for `pi-rc`) wins without probing anything; otherwise the highest-version
live `session-host` descriptor for `pi-pty-host/1` names the endpoint
file; otherwise the legacy runtime path from `RuntimeLayout` is used.
Resolution happens when the path is used (`HandoverHold` resolves on
`open()`), not at module load, so a provider that published after the
client started is still found. Staleness is the directory's rule -
malformed JSON, a dead pid, or an endpoint file that is missing or
publishes no dialable coordinates - and a stale descriptor is skipped
without being removed.

`pi/lib/pi_rpc_client.py` is the socket-profile library: it discovers,
dials, greets, frames and reports failures as data. `pi/bin/pi-comms` is
its first consumer - `node`, `peer list`, `send`, `broadcast`, `wait`,
`replay`, `ack`, `receipt` and `spawn` - with JSON by default and
`--format text` for people. Its exit codes are the failure vocabulary of
the profile: 1 usage, 2 declined, 3 empty wait, 4 no service, 5 remote
method, 6 authorization, 7 no answer after a request was written.

The pi extension exposes the same surface to the model as `comms_send`,
`comms_wait`, `comms_replay`, `comms_ack`, `comms_spawn` and `peers`,
registered from `daemon.ts` and shelling out to
`pi/bin/pi-comms --format json` (`PI_COMMS_BIN` overrides the binary).
Each tool returns the service's JSON as its structured result, and a
non-zero exit becomes a tool error carrying the CLI's stderr and exit
code; there is no second RPC client in TypeScript.

## Peers

A peer is a direction this node reaches out over: an SSH destination,
the port on both ends, and a reference to the credential file the
outbound link reads. `comms.peer.add`, `comms.peer.list` and
`comms.peer.remove` are the operator surface, and peer configuration
holds endpoints and a SHA-256 verifier per source node, never a
credential's contents; an answer carries no credential field at all.
Each configured peer runs one outbound worker that opens its tunnel and
retries with a bounded backoff, and every state change commits before
a link or worker exists.

The tunnel is one ssh child in its own process group, in the
foreground, so the process the daemon started is the forward it can
stop. It is given the destination and both ports as argv, and the
daemon runs no shell of its own: ssh's own `LocalCommand` carries
nothing but the readiness marker. The argv does not override
`ForkAfterAuthentication`, so a user's own ssh config must not turn the
fork back on for this destination; the daemon's child is the forward it
stops. The
credential it reads lives in the comms domain's peer-credentials
directory, must be a regular file of the daemon's own user with no
group or other permission bits, and is opened without following a
symlink, so a path an agent names can never reach the token file or a
key outside that directory.

A peer connection presents `{version, peer: {nodeId, credential}}` at
`rpc.hello`, never the daemon token and never an agent identity: the
verifier configured for that node decides admission, and the greeting
binds the connection to the peer's own node with the peer methods
(`comms.peer.deliver`, `comms.peer.ping`), no agent method and no
topics. An agent connection is offered the peer administration and
never the peer wire.

Inbound delivery is one envelope, committed before it is published,
and answers `accepted` with the mailbox sequence it took. A delivery ID
is deduplicated per source node: a replay returns the outcome recorded
the first time and stores nothing, and the same ID with a different
body is a `request-conflict`. A peer whose node has no configured
verifier, or whose credential does not match it, is refused at
`rpc.hello` and no mailbox is created.

The remote proposes, this node disposes: an inbound expiry is clamped
to our own maximum TTL, agent ids and kinds are held to our limits, and
the dedupe markers are capped per source node, so a busy peer fills its
own share and is told `capacity` rather than starving the others. A
marker is kept only until the sender's own expiry plus the clock skew
and one request timeout, which is all the margin a last in-flight retry
needs, so the table cannot hold a day of traffic. An answer that does
not carry the delivery it names, with a message id and a sequence this
node can use, is not recorded as an outcome.

A foreign address is routed under the store's own lock, so a removal
cannot slip between the routing decision and the commit, and mail left
queued for a generation that no longer exists is settled when the
manager starts rather than waiting for an expiry.

Removal first marks the peer's generation removing, so no import or
delivery is accepted under it, stops that peer's worker, then settles
the mail still queued for it: mail never attempted is `undeliverable`
with reason `peer-removed`, and mail already attempted is `unknown`,
because this node cannot promise the remote mailbox did not take it.
The answer reports both counts, and adding the peer again mints a new
generation, so mail queued for the removed one can never be delivered
as mail for the new one. A connection still bound to the removed
generation is refused at `comms.peer.ping` and at `comms.peer.deliver`
with `peer-stale`, which sends its worker back to a fresh `rpc.hello`
instead of wedging it: a steady stream of sends cannot keep a dead
generation's link alive.

The message envelope is bounded by the frame, not only by the payload
limit, so a notification always fits the connection that carries it. A
payload that cannot fit answers `payload-too-large`; a replay page is
shortened to the frame and reports `more` with a cursor.
Agent ids, request ids and kinds have their own length limits, and the
reply margin must leave room for the reply wrapper, a short request id
and the cursor, so a message that was accepted can always be paged back.
A JSON-RPC id has no length limit: a reply that does not fit is answered
with `capacity` instead of being dropped.
Acknowledgement markers expire with the messages they name.

Mail is durable before anyone is told about it. Acceptance commits the
envelopes and a send receipt in one write, so a retried `requestId`
replays its first outcome instead of delivering twice, and an agent that
is offline still finds its mail. Sequences belong to one mailbox;
replay pages from a cursor, and a cursor older than the retained
history reports where that history begins.

Acknowledgement marks mail read; it does not free the capacity a
mailbox holds, because acknowledged mail stays replayable until it
expires, and capacity is about the mail the store keeps.

A client subscribes, then replays from its cursor: mail accepted
before the subscription arrives only through that replay, and mail
accepted after it can arrive both live and in the replay, so a client
deduplicates by sequence.

`broadcast` means every mailbox that exists when the broadcast is
accepted, minus the sender. The receipt freezes that audience, so a
later mailbox receives no earlier broadcast and a retry expands no
further.

`comms.wait` long-polls the mailbox: `{cursor?, limit?, timeoutMs?}`
answers in the shape of `comms.replay` with `status` `ok` or `timed-out`,
where a timeout returns the unchanged cursor. The wait is clamped by
`comms_wait_max`, never holds its connection (the socket keeps reading),
is answered `-32800` by `rpc.cancel` and `-32003` by shutdown, and a
second concurrent wait on one connection or a wait over
`comms_wait_cap` (the global count) or the wait already outstanding on
that connection answers `capacity`, with `comms_waits_per_connection`
(default 1) waits allowed on one connection. The same request id reused
while its wait is pending answers `-32602` (`wait-pending`), and
shutdown answers every pending wait `-32003` before the listener closes
its sockets. A cursor the mailbox no longer holds answers `cursor-expired`
with `oldestAvailableSequence`, exactly as `comms.replay` reports it.
Waits are answered from the mailbox, not from the live relay, so a wait
never loses mail that a disconnected notification queue dropped.

`spawn` delegates the process to the sessions domain; comms owns the
spawn record with the child's identity, its parent provenance and the
launch correlation ID. It takes `{requestId, agentId?, name?, dir, argv,
cols?, rows?, env?, envOnce?}` and answers `{status, spawnId,
requestId, agent, parent, session, created, reason?}`. The ledger record
is written before the mailbox is reserved and the process started, so a
retry with the same `requestId` and the same body replays the recorded
verdict while a different body is `request-conflict`. The record never
expires while it is `launching`, and a restart settles it `failed` with
reason `interrupted` rather than launching twice. A launch that started
nothing leaves neither record nor mailbox and answers its reason
(`no-dir`, `name-taken`, `invalid-session`, `spawn-failed`), so a retry
is free. The mailbox such a launch reserved is provisional and belongs
to one launch, named by its `spawnId`. Presenting the identity or
addressing mail to it adopts the mailbox, and only the launch that
reserved an empty, unadopted mailbox can remove it. A ledger that cannot
be written answers `store-failed` (`-32003`). A reservation that failed
leaves no record, while a launch whose settlement failed stays
`launching` and every retry replays that uncertainty instead of
launching twice. A rollback that cannot be written also answers
`store-failed`, so the retry replays that uncertainty rather than
reporting a free failure. The child is told who launched it through
`PI_COMMS_AGENT_ID`, `PI_COMMS_NODE_ID`, `PI_COMMS_PARENT_AGENT_ID` and
`PI_COMMS_SPAWN_ID`, and once, in `envOnce`, where to connect back
through `PI_COMMS_HOST` and `PI_COMMS_PORT`. A client may not set those
variables.
There is no other link: the parent is a provenance line in the child's
environment, never a dependency. The parent reference is informational: a child's
mail, own spawns, tools and memory writes never consult the parent's
liveness, subscription or connection, no termination cascades, and
ending either session leaves both mailboxes intact. Peers carry mail and
delivery receipts only - never membership, spawn or session control.

## The migration

Order matters, and each step is reversible on its own:

1. Ship the new package beside the installed one (`pi/service/`), with
   tests in `tests/run.py`. Nothing installed changes.
2. Bring up the staging entry with explicit roots and verify the
   control contract, tickets, restart recovery and clean shutdown.
3. Add the remaining domains against the same composition: sessions
   (session hosting, run state, revive, transcripts), then comms
   (mailbox, relay, waits, spawn, peers) as the `agent-comms`
   capability over `pi-rpc/1` on its own listener. Hosting, run state,
   attach/detach, announce/handoff and recovery are in place; revive
   by name is the client's own `start`, and transcripts plus the
   deferred operations above are the remaining work in this step.
4. Ship the new client beside the old one and rewire `pi-rc` and the
   extensions to it, keeping it compatible with both servers, then
   remove the legacy aliases from the migration translator.
5. Translate state once, after step 4 and before step 6: the `migrate`
   subcommand of the new entry reads the legacy root and writes the new
   layout, leaving the legacy files untouched for one release. Its
   contract is below; no code can use it before the entry switch, so it
   lands with step 6.
6. Switch the entry point only after the clients and the installer
   land: `pi/daemon/pi-daemon` becomes the thin bootstrap, the
   implementation moves to `pi/lib/pi_daemon/`, and the old monolith
   and the standalone broker are deleted in the same commit. No
   adapter is written between old and new code, so the switch is the
   last commit that can be rolled back without a client change.
7. Update the installer for the private tree (`lib/`, `bin/`,
   `daemon/`, `extensions/`), the unit files and the docs. Done: the
   installer provisions `pi-comms` and `pi_rpc_client.py` beside `pi-rc`,
   records manifest format 2 (with `scripts/install.sh` among its sources,
   so an installer-only advance re-provisions), and manual mode is gone -
   the package is the only install and a bare run writes nothing. The
   private tree and the unit's environment land with step 6.

## Cutover

Two roots, never shared, so the two implementations can never see each
other's lock, registry or descriptor:

| | legacy | new |
|---|---|---|
| state | `$XDG_STATE_HOME/pi-pty-host/` | `PI_DAEMON_STATE_DIR` |
| | `tickets.json`, `tickets/` | `tasks/tickets.json`, `tasks/logs/` |
| | `sessions.json` | `sessions/sessions.json` |
| | `session-files.json` (a transcript-file index) | none: dropped |
| | `run-state/<name>.json` | `sessions/runs/<key>.json` |
| | the `piDaemon` keys of the agent home's `settings.json` | |
| | | `<state>/settings.json`, an optional `daemon` section |
| | | `PI_DAEMON_<FIELD>`, which overrides a field |
| runtime | `$XDG_RUNTIME_DIR/pi-pty-host.sock` | `PI_DAEMON_RUNTIME_DIR` |

`migrate` is the new entry's own subcommand, never `pi-rc` (a client that
must speak to both servers and must never write server state) and never a
one-shot script (which would duplicate the stores' layout). It reads the
legacy root read-only and writes the new one:

    pi-daemon migrate --dry-run        # print the plan as JSON, write nothing
    pi-daemon migrate                  # translate, then record a marker

`migration.json` in the new state root holds the sha256 of every legacy
file it read. A rerun whose hashes still match does nothing; a rerun whose
hashes moved refuses unless `--force` is given, because a translated store
is not a merge; a run while the new instance lock is held refuses
outright. The legacy root is never written, which is what makes rollback
independent of the new code.

The order follows from `migrate` belonging to the new entry, which only
exists after the swap. Stop the unit, then install the new release with
`PI_DAEMON_STATE_DIR` and `PI_DAEMON_RUNTIME_DIR` exported to the values
the unit will use. That installer replaces the launcher and the unit, runs
`migrate --dry-run` and then `migrate`, and only afterwards runs
`systemctl --user enable --now`; a failed translation starts nothing,
because a started service would take the instance lock and the next
`migrate` would refuse on it. The installer runs the translation rather
than `ExecStartPre`: with `Restart=on-failure` and
`StartLimitIntervalSec=0` a failing pre-start command would restart every
two seconds forever.

## Rollback

Rollback never depends on the new code, because the legacy root was never
written:

1. `systemctl --user stop pi-daemon.service`.
2. `pi install git:github.com/ederevx/pi-daemon@<the previous tag>`: its
   postinstall restores the previous `pi-daemon` launcher and unit.
3. `rm -f ~/.local/bin/pi-comms ~/.local/bin/pi_rpc_client.py`: the
   previous release's manifest does not own them, so its `uninstall.sh`
   would leave them behind.
4. `systemctl --user start pi-daemon.service`, unless step 2 already did:
   the postinstall runs `enable --now`, so this step only matters where
   systemd is absent and that call did nothing.

The new state root stays in place as evidence. A rollback moves the
legacy state again, because the legacy daemon keeps writing it, so the
next `migrate` refuses on the changed hashes; `--force` is what
retranslates it, and it replaces the new root rather than merging into it,
which is why the marker never re-adopts a translated root silently. The
instance lock and the runtime descriptor are per-root, so the two
implementations never share a runtime directory.

## Open work

* Transcripts and the deferred session operations (`carry`,
  `extensions-reload`, `handover`, `daemon-restart`).
* The relay uses `select()`, so a descriptor numbered 1024 or higher
  would stall it; move to `selectors` before that is reachable.
* Shutdown terminates children serially, one `cancel_grace` per child
  that ignores SIGTERM; send SIGTERM to all of them first when the
  session count grows.
* A name whose last run was left unsealed (`unknown` identity) is
  reachable by `run-state` only until the name is reserved again; a
  record's run files should be found by id, not by the name's key.
* The outbound queue bounds frames, not bytes: one connection can hold
  `comms_outbound_queue` frames of up to `frame_bytes`, and nothing caps
  the connection count. Give it a byte budget when fan-out grows.
* Remote spawn over a peer link: cross-node mail, admission, removal,
  the operator surface for the peer's mailbox and the outbound retry
  schedule are in place on top of the mailbox, relay, waits, spawn and
  the `agent-comms` listener, so only the `peer-control` frame that
  carries a launch across the link remains.
* Step 6: the entry switch, the private tree in the installer, the unit's
  two roots, and `migrate` itself.
* Discovery activation: the descriptor publishes `pi-rc daemon-start`
  and the client reports a missing service, but nothing yet starts one;
  the CLI's binding is explicit, so a second entry is never dialed.
