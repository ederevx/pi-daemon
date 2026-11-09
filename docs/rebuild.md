# The unified service: framework and migration

This branch builds one service from scratch: one process, one control
contract, and every domain owned by an object instead of a module. No
implementation from the old daemon or from pi-teams is imported; the
existing public contract is reimplemented against the same wire, so the
installed clients keep working when the entry point switches.

## What exists now

The unified service's package lives in `pi/lib/pi_daemon/`, beside the
seam modules the clients already import, and the installed entry is the
bootstrap in the same tree; the one verb that runs before an install is
its own command:

```
pi/daemon/pi-daemon                     the entry (bootstrap only)
pi/bin/pi-daemon-migrate                 MigrateCommand, the one-time
                                         translation
pi/lib/pi_daemon/application.py           DaemonApplication
pi/lib/pi_daemon/config.py                ServiceConfig
pi/lib/pi_daemon/runtime_layout.py        RuntimeLayout
pi/lib/pi_daemon/atomic_state_file.py     AtomicStateFile
pi/lib/pi_daemon/wire.py                  WireCodec (pi-daemon/1)
pi/lib/pi_daemon/lifecycle/instance_coordinator.py
pi/lib/pi_daemon/transport/json_line_stream.py
pi/lib/pi_daemon/transport/endpoint_file.py
pi/lib/pi_daemon/transport/endpoint_probe.py
pi/lib/pi_daemon/transport/control_handshake.py
pi/lib/pi_daemon/transport/control_endpoint.py
pi/lib/pi_daemon/transport/control_router.py
pi/lib/pi_daemon/tasks/error.py           TaskError
pi/lib/pi_daemon/tasks/model.py           Ticket
pi/lib/pi_daemon/tasks/store.py           TicketStore
pi/lib/pi_daemon/tasks/runner.py          TicketRunner
pi/lib/pi_daemon/tasks/service.py         TaskService
pi/lib/pi_daemon/tasks/operations.py      TaskOperations
pi/lib/pi_daemon/sessions/error.py        SessionError
pi/lib/pi_daemon/sessions/identity.py     ProcessIdentity
pi/lib/pi_daemon/sessions/model.py        SessionRecord
pi/lib/pi_daemon/sessions/run_outcome.py RunOutcome
pi/lib/pi_daemon/sessions/store.py        SessionStore
pi/lib/pi_daemon/sessions/service.py      SessionService
pi/lib/pi_daemon/sessions/operations.py SessionOperations
pi/lib/pi_daemon/sessions/runner.py       SessionRunner
pi/lib/pi_daemon/sessions/relay.py        SessionRelay
pi/lib/pi_daemon/sessions/relay_session.py RelaySession
pi/lib/pi_daemon/sessions/pty_backend.py PosixPtyBackend
pi/lib/pi_daemon/sessions/pty_child.py    PtyChild
pi/lib/pi_daemon/sessions/pty_exec.py     PtyExec
pi/lib/pi_daemon/transcripts/operations.py TranscriptOperations
pi/lib/pi_daemon/transcripts/service.py    TranscriptService
pi/lib/pi_daemon/transport/control_action.py ControlAction
pi/lib/pi_daemon/migration/error.py       MigrationError
pi/lib/pi_daemon/migration/legacy_root.py LegacyRoot
pi/lib/pi_daemon/migration/translator.py  LegacyTranslator
pi/lib/pi_daemon/migration/plan.py        MigrationPlan
pi/lib/pi_daemon/migration/marker.py      MigrationMarker
pi/lib/pi_daemon/migration/migration.py   Migration
pi/lib/pi_daemon/migration/command.py     MigrateCommand
```

Each file owns one cohesive responsibility and one primary class. The
entry point only bootstraps: it requires `PI_DAEMON_STATE_DIR` and
`PI_DAEMON_RUNTIME_DIR`, composes `DaemonApplication` and runs it, and it
takes no arguments. `pi-daemon-migrate` bootstraps the same way and runs
`MigrateCommand`; neither falls back to the live roots.

`DaemonApplication` starts the instance lock, the tasks domain, the
sessions domain, the transcripts domain and the control endpoint in that
order, and stops them in reverse: close admission, stop accepting, settle sessions, cancel and
join children, persist both stores, remove the owned descriptor, release
the lock.

## The control contract

The listener speaks the published protocol, reimplemented field by
field against the installed clients:

```
client → {"cmd": "hello", "token": "<from descriptor>"}
server → {"ok": true, "srv": "pi-daemon", "proto": 1,
          "caps": ["transcript.v1"], "index": 1}
client → {"cmd": "<command>", ...}
server → {"ok": true, ...} | {"ok": false, "error": "<name>"}
```

The greeting carries exactly those keys: a malformed frame or a hello
that is not `cmd: hello` with the descriptor's token is answered
`bad-handshake`. The error names are the published ones:
`bad-handshake`, `bad-request`, `no-ticket`, `running`, `too-busy`,
`spawn-failed`, and every failure reply is built by `error_reply` in
`wire.py`, the single owner of the shape. The greeting advertises only
capability this build serves: `transcript.v1` since the transcript
commands answer, and `index` stays at the published `1`.

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

The transcript reads answer the published fields. `transcript-open`
returns the conversation's metadata plus `transcriptId` (a handle) and
`index` (the `{"state": ...}` the refresh produced); every other verb
accepts that handle, else a `file`, else a `session` name whose announced
conversation is resolved through the session store. `transcript-stat`
drops `file`. The rejections are `no-such-transcript`, `bad-transcript`,
`no-index`, `bad-cursor` and `bad-request`, and a reply echoes the
request's `id`. The daemon never serves a path outside the conversation
store, and the sweep skips every conversation a live session backs: its
announced file and any `--session` argument, for as long as its record is
live. The `session` form also resolves a name whose record is no longer
live, where the legacy daemon answered only for a hosted session; that is
additive, and the file it names is still checked against the store.

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
gc-reap-ack | daemon-purge | extensions-reload | daemon-restart |
shutdown
```

* `list` answers the published session view: `name`, `dir`, `pid`,
  `attached`, `clients`, `file`, `state`, `argv`; the client counts are
  read from the relay, never from the stored copy, which can lag a
  viewer that went away.
* `which` names the session a conversation is hosted by; `whichall`
  answers `names`, one `name`/`state` entry per live holder.
* `start` answers `created` (a live name is replayed, never replaced);
  the child environment is `os.environ` plus the stored `env`, plus
  `PI_DAEMON_SESSION`, plus the transient `env_once` overlay, and last
  of all - never overlaid - `PI_DAEMON_STATE_DIR`, which the daemon sets
  to the state root it resolved, so a hosted session's extensions read
  the reload and reap signals of the daemon that hosts them and a
  relative root cannot drift with the child's working directory.
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
* `daemon-purge` stops idle sessions with no viewer; `extensions-reload`
  asks hosted conversations to reload in place; `shutdown` answers
  before the application stops and carries the same `mode` the run ends
  with.

`extensions-reload` is one round with no memory, and rounds never
overlap: the service holds a lock for the whole round, so two callers
can neither overwrite each other's request nor read each other's
acknowledgement. It targets the live sessions that announced a
conversation and carry no `--no-session` (`skipped` says
`not-reloadable` otherwise), and it leaves a session whose turn or
blocking daemon wait is open alone (`not-idle`), because pi would deny
the reload there; a busy session is skipped and nothing retries it, so
the caller runs the command again. Nothing is started, respawned or
revived, so a broken update cannot become a spawn storm.

The request is a round's own file under the state root,
`<state root>/extensions-reload/<name>/<round>.json`, written whole
under a round name nothing can reuse and carrying the run it is for (the
run id and generation the daemon puts in the child's environment), so a
successor that took the session name never answers its predecessor's
request. The extension's watcher queues pi's own
`/daemon-reload --signal <round>` without removing the file; that
command's handler removes exactly the round's file and only then runs
pi's own reload. The queued command is terminal either way, so a round
the daemon already took back stays inert instead of falling through to
the manual restart, and a removal that fails for any reason other than
the file being gone is not an acknowledgement. A session that has not
acknowledged within the grace - an older extension without the watcher,
a watcher event that never arrived, or a scheduler that never ran the
queued command - gets `/reload` typed into its PTY instead. That
fallback is guarded twice: the round claims only its own file, so a
request consumed at the deadline or a later round's request is never
deleted, and it types by the run identity it selected (id and
generation), never by name, so a successor that took the name never
receives the earlier round's bytes. The typed bytes are queued for the
PTY, not proven rendered, and the fallback assumes the TUI is at a
prompt: idle is the service's only evidence, so a half-typed editor is
the operator's risk. One round's file is unique to it, so no round can
ever answer or delete another's request, and a startup purges the
directory, half-written temporaries included. The claim protocol and
that purge assume a state root is owned by one daemon (its instance lock
lives in the runtime root): two daemons sharing one state root would
race each other's claims and purge each other's requests.

The reply carries `reloaded` (a session whose request was taken, or
whose PTY accepted the typed command), `skipped` (`id` and `reason`:
`not-reloadable`, `not-idle`, `write-failed`, or `unknown` for a name
the round was asked for that is not live - `sessions` is an
intersection with the live set, and `[]` selects none), and `deferred`,
which says only that some selected session was left alone because it was
not idle; nothing is scheduled.

Deferred to the next increment, and answered `bad-request` today:
`carry`, `handover`, `daemon-restart`. A restart is a handover to a
successor process, so promising it before a successor exists would
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
instance. Every stop writes its mode and cause to stderr, which is the
unit's journal, so `journalctl --user -u pi-daemon` names the signal or
the client that asked for a stop no supervisor requested.

## Configuration

Every operational value is a `ServiceConfig` field, resolved once from
environment (`PI_DAEMON_<FIELD>`) over the settings file over the
default, and invalid values fall through to the next source. A numeric
field refuses booleans and non-finite numbers, and zero is a value, not
"unset". A flag takes only the two boolean spellings - a JSON boolean, or
`true`/`false` or `1`/`0` in an environment variable or the settings file
- and any other spelling falls through, so `PI_DAEMON_X=false` is false
and `PI_DAEMON_X=0` is not a truthy string.

There are no legacy names, no `PI_PTYD_*` fallbacks and no pi-teams
aliases: the new service only reads its own names. Legacy configuration is
not translated automatically: a `piDaemon` key - a ticket TTL, an
extension watch or an idle window - has no counterpart here, and
`migrate` translates none of it. Automatic extension-root watching is
not provided either: the settings surface no longer offers `extWatch*`,
those legacy keys and the `PI_PTYD_EXT_WATCH*` variables are ignored,
and a reload is explicit - `/reload` in a session, or
`pi-rc extensions-reload` for eligible idle hosted sessions, with a busy
session skipped rather than owed. The conversation index is configured by
`session_store_root`, `transcript_index`, `transcript_index_interval`,
`transcript_index_max_bytes` and `transcript_index_max_line`. An empty
`session_store_root` means the `sessions` directory of the agent home in
the environment this application was given (`PI_CODING_AGENT_DIR`), never
the process's own.

A comms setting whose zero would stop mail rather than disable a
feature is refused, so it falls through to its next source. The comms
sizes also have to agree with each other: a message TTL may not exceed
the maximum TTL, a payload plus the reply margin must fit one frame, and
the reply margin must be at least the agent-id limit plus 256 bytes.

## Domain boundaries

| Domain | Owns | Never |
|---|---|---|
| tasks | tickets, IDs, output logs, child execution | session, comms or peer state |
| transcripts | the conversation index, read handles, the sweep | session records, the relay |
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

1. Ship the new package beside the installed one (`pi/lib/pi_daemon/`,
   driven by its bootstrap `pi/daemon/pi-daemon`), with
   tests in `tests/run.py`. Nothing installed changes.
2. Bring up the bootstrap entry with explicit roots and verify the
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
5. Translate state once, after step 4 and before step 6: the
   `pi-daemon-migrate` command reads the legacy root and writes the new
   layout, leaving the legacy files untouched for one release. Its
   contract is below. The verb ships with the candidate generation and is
   tested there; only its call from the installer waits for step 6,
   because until the swap the root it reads keeps changing.
6. Switch the entry point only after the clients and the installer
   land: `pi/daemon/pi-daemon` becomes the thin bootstrap, replacing
   the legacy monolith, and the staging entry is deleted in the same
   commit. No standalone broker exists in this
   tree. No adapter is written between old and new code, so the switch
   is the last commit that can be rolled back without a client change.
   Done: the bootstrap imports `pi_daemon` from `lib/` beside it and
   turns off bytecode writing, because the library it runs lives in a
   release nothing may write to.
7. Update the installer for the private tree (`lib/`, `bin/`,
   `daemon/`, `extensions/`), the unit files and the docs. Done: the
   installer provisions `pi-comms` and `pi_rpc_client.py` beside `pi-rc`,
   records manifest format 2 (with `scripts/install.sh` among its sources,
   so an installer-only advance re-provisions), and manual mode is gone -
   the package is the only install and a bare run writes nothing.
   Done with step 6: each install builds one generation under
   `~/.local/lib/pi-daemon/releases/<id>/` and publishes it by replacing
   manifest format 3 (active generation, retained ones, every source
   digest, per-tree digests); `~/.local/bin` holds only dispatchers that
   read the manifest, so an upgrade is a pointer swap and a running
   daemon keeps its release. The installer asks itself (`--sources`) for
   the inventory the provisioning stamp compares against, so an added or
   removed module re-provisions as well as an edited one. The migration
   runs from the candidate generation before any public file changes and
   refuses (76) while a daemon serves the old state or endpoint, and a
   held install lock exits 75. A generation no manifest names is swept
   under the lock; a retained one is dropped once no daemon serves, and
   only the newest retained one survives then. While a daemon serves, no
   retained generation is removed, because any of them may be the one it
   runs, so the directory is bounded across an install with the unit
   stopped and a crash leaves no orphan release.

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
| | `gc-reap/<name>.json` (a reap request) | none: the new side asks nothing of a session |
| | the `piDaemon` keys of the agent home's `settings.json` | not translated; configure new-service fields explicitly |
| | | `<state>/settings.json`, an optional `daemon` section |
| | | `PI_DAEMON_<FIELD>`, which overrides a field |
| runtime | `$XDG_RUNTIME_DIR/pi-pty-host.sock` | `PI_DAEMON_RUNTIME_DIR` |

`pi-daemon-migrate` is the installed verb, never `pi-rc` (a client that
must speak to both servers and must never write server state) and never a
one-shot script (which would duplicate the stores' layout). It reads the
legacy root read-only and writes the new one:

    pi-daemon-migrate --dry-run        # print the plan as JSON, write nothing
    pi-daemon-migrate                  # translate, then record a marker

`migration.json` in the new state root holds the sha256 of every legacy
file it read. A rerun whose hashes still match does nothing; a rerun whose
hashes moved refuses unless `--force` is given, because a translated store
is not a merge; a run while the new instance lock is held refuses
outright, though the plan still reports the intent it refused for, so a
caller can tell a translated store from one that needs a translation.
The legacy root is never written, which is what makes rollback
independent of the new code.

A run that reaches a decision prints the plan as JSON, and `--dry-run`
differs only in that it writes nothing, so it fails exactly where the real
run would; an unreadable input prints only its message. The exit status
names the outcome: 0 for a translation or an already-current root, 1 for
an unreadable input, 3 for a held instance lock, 4 for sources that moved
since the marker, and 5 for a state root that holds untranslated state.
A refusal writes nothing. A translation writes the ticket and session
stores through their owners and records the marker last, with the digests
of the files it actually read; a file that moved in between exits 4
without a marker, and the next run then refuses as untranslated state.

The order follows from the generation being built before anything public
changes: the installer builds the candidate release, runs
`pi-daemon-migrate --dry-run` and then `pi-daemon-migrate` from inside it
with the roots the unit will declare, and only afterwards replaces the
launchers and the unit, enables it, and starts it only where no daemon
holds the runtime root: a start beside one would restart onto the lock,
so a lock over an already-translated root leaves the unit enabled and
not started, and the install warns once. When the
lock is held and the plan's intent is `current`, no store needs writing,
so the installer publishes the generation and skips the translation
altogether; a lock over an untranslated or moved legacy root still stops
it. A failed
translation starts nothing, because a started service would take the
instance lock and the next translation would refuse on it. The installer
runs the translation rather than `ExecStartPre`: with
`Restart=on-failure` and `StartLimitIntervalSec=0` a failing pre-start
command would restart every two seconds forever.

The one host the installer cannot serve alone is the one whose daemon
hosts the session asking it to run: `systemctl --user stop` ends that
session, because the unit's default `KillMode=control-group` reaches
every process in its cgroup and a `setsid` or `nohup` child cannot leave
it. There the install runs under `systemd-run --user`, which gives it a
transient unit of its own, with the environment a ticket cannot supply:
`PATH` must reach the real `pi` (`~/.pi/agent/bin/pi`, not the wrapper
directory and not the shim), and `PI_CODING_AGENT_DIR`,
`PI_DAEMON_STATE_DIR`, `PI_DAEMON_RUNTIME_DIR` and `TMPDIR` are named
explicitly. An operator-written script for it logs outside the
deployment clone, which the next `pi install` resets and cleans, and
decides on the installer's status:
75 is retried while a session's background provision holds the lock, 76
means a daemon came back and the legacy unit is still in place to start,
and a failed migration installs nothing.

Who starts a daemon follows from the unit's own declaration: `pi-rc
daemon-start` starts the unit when it is loaded and declares this
runtime root, and spawns a detached daemon only where no such unit
exists. The `pi` wrapper calls it instead of deciding for itself, so the
choice is made once and cannot disagree with the unit that owns the
root. Where a daemon already holds the lock, neither starts anything
(see Takeover).

## Takeover

A daemon can serve the root the unit declares without being the unit's
own. A detached daemon started while the unit was down, by an activation
that did not defer to it, keeps the instance lock and keeps serving its
release; the installer leaves it alone, enables the unit and does not
start it, and says so once, because a start beside a held lock only
restarts onto that lock. Moving the root to the unit is an operator step,
and it runs from a transient unit so the stop below cannot end the
session issuing it (see Cutover for the `systemd-run --user` invocation
and the environment it needs).

1. Publish the release and let it prove the lock exit. `pi install
git:github.com:ederevx/pi-daemon@<tag>` starts nothing here: the unit is
re-rendered and reloaded, its next attempt runs the new release, exits
3 on the held lock and stays failed, so `SubState` reads `failed` and
`NRestarts` stops growing instead of the unit restarting every two
seconds.
2. Record the sessions to re-attach afterwards: `pi-rc ls` gives each
one's name, directory and file.
3. Stop the old daemon and wait for it to release the lock. It settles
its sessions, persists them and only then exits, so the wait is the
lock's own answer, not a fixed sleep:

```bash
RT=${PI_DAEMON_RUNTIME_DIR:-/run/user/$(id -u)/pi-daemon}
RC=~/.local/bin/pi-rc
MANIFEST=${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/.pi-daemon/manifest.json
LIB=$(python3 -I -c 'import json, sys
print(json.load(open(sys.argv[1]))["active_root"] + "/lib")' "$MANIFEST")
"$RC" --endpoint "$RT/control.json" daemon-stop
for _ in $(seq 1 60); do
	python3 -I -c 'import sys
sys.path.insert(0, sys.argv[2])
from pi_daemon.lifecycle.instance_coordinator import InstanceCoordinator
sys.exit(1 if InstanceCoordinator(sys.argv[1]).held() else 0)' \
		"$RT/instance.lock" "$LIB" && break
	sleep 0.5
done
systemctl --user start pi-daemon.service
```

Anything that tries to start a daemon in that gap goes to the unit,
because activation defers to it; `-I` keeps the probe from importing
anything out of the working directory. The start returns once the unit
is up, not once the daemon serves; confirm `MainPID` is non-zero and
equal to the `pid` in `$RT/control.json`.

4. Prove supervision before any session is re-attached. `KillMode`
is `control-group`, so a hosted child dies with the main process, and
the restart that follows is the unit's:

```bash
MAIN0=$(systemctl --user show -p MainPID --value pi-daemon.service)
R0=$(systemctl --user show -p NRestarts --value pi-daemon.service)
kill -KILL "$MAIN0"
# a SIGKILL leaves a stale descriptor; the new instance overwrites it
for _ in $(seq 1 30); do
	MAIN=$(systemctl --user show -p MainPID --value pi-daemon.service)
	PID=$(python3 -I -c 'import json, sys
print(json.load(open(sys.argv[1])).get("pid"))' "$RT/control.json")
	[[ -n $MAIN && $MAIN != 0 && $MAIN != "$MAIN0" && $MAIN == "$PID" ]] && break
	sleep 1
done
test "$(systemctl --user show -p NRestarts --value pi-daemon.service)" \
	-eq $((R0 + 1))
for pid in $(pgrep -f '/daemon/pi-daemon$|\.local/bin/pi-daemon$'); do
	grep -q '/pi-daemon.service$' "/proc/$pid/cgroup" || echo "unowned: $pid"
done
"$RC" --endpoint "$RT/control.json" ls
```

5. Re-attach each recorded session with `pi-rc start <name> <dir>`; an
explicit name reopens its record rather than making a twin, and `pi-rc
ls` shows the result.
6. Soak: ten minutes later, re-read `MainPID` and `NRestarts`, the
`pid` in `control.json` and the cgroup of the daemon's processes, and
confirm they still agree.

`daemon-stop` stays a plain client stop. Routing it through
`systemctl stop` would recurse from the unit's own `ExecStop`, and
`INVOCATION_ID` cannot tell that apart from a hosted session, because
hosted sessions inherit the daemon's environment.

## Rollback

Rollback never depends on the new code, because the legacy root was never
written:

1. `systemctl --user stop pi-daemon.service`, from outside the unit's
   cgroup when a hosted session is asking (see Cutover: `systemd-run`).
2. `pi install git:github.com/ederevx/pi-daemon@<the tag the running
   install came from>`: its postinstall restores the previous
   `pi-daemon` launcher and unit. The target is the tag `settings.json`
   named before the cutover, not the previous tag by number: a host can
   run a tag the release list has moved past, and rolling back below it
   would drop whatever that tag carried. Record it before the cutover,
   because the entry then names the new one.
3. `rm -f ~/.local/bin/pi-comms ~/.local/bin/pi-daemon-migrate
   ~/.local/bin/pi-daemon-migrate.cmd ~/.local/bin/pi_rpc_client.py`: the
   previous release's manifest does not own them, so its `uninstall.sh`
   would leave them behind.
4. `rm -rf ~/.local/lib/pi-daemon/`: the previous install kept no
   generation there and stops maintaining the directory, so the rollback
   removes what this install published.
5. `systemctl --user start pi-daemon.service`, unless step 2 already did:
   a start is the installer's where no daemon holds the runtime root, so
   this step only matters where systemd is absent and that call did
   nothing.

The new state root stays in place as evidence. A rollback moves the
legacy state again, because the legacy daemon keeps writing it, so the
next `pi-daemon-migrate` refuses on the changed hashes; `--force` is what
retranslates it, and a forced run replaces the ticket and session stores
while leaving `comms/` (the node identity, the mailbox, the peers) and
`settings.json` alone. Because a rerun is decided by the recorded hashes,
the marker never re-adopts a translated root silently. The instance lock
and the runtime descriptor are per-root, so the two implementations never
share a runtime directory.

## Open work

* The deferred session operations (`carry`, `handover`,
  `daemon-restart`). Transcripts are served: the seven
  `transcript-*` verbs, the `transcript.v1` capability and the sweep.
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
* The unit's two roots and the installer's call to the translation
  shipped with step 6; what remains there is the maintenance cutover on a
  host whose legacy daemon is running, which the installer refuses (76)
  instead of attempting.
* Discovery activation: the descriptor publishes `pi-rc daemon-start`
  and the client reports a missing service, but nothing yet starts one;
  the CLI's binding is explicit, so a second entry is never dialed.
* The install lock is broken after ten minutes without checking whether
  its run is still alive. A run that takes a broken lock treats the live
  run's `install-transaction.json` as an interrupted install: it restores
  that run's backups over the launchers and unit it just wrote, and may
  delete the generation it is publishing. Both runs then write the
  manifest and both call systemctl, the live run's exit trap releases the
  lock the second one now holds, and two runs from one clone can nest one
  staging tree inside the other's generation until the next install
  rebuilds it. Record the owner's pid in the lock and break it only when
  that pid is gone (portable to Git Bash), or take a kernel-released lock
  (`flock`) where one exists.
