# The rebuild

This branch replaces pi-daemon with one service built from a single
unified framework. The old daemon and the pi-teams broker are not
carried over: no module, class, or function of theirs is reused. What
is kept is what is not theirs to change — the contracts a client sees,
the platform primitives, and the state a migration must not lose.

## The framework

One process, one state root, one control endpoint, three domains.

```
entry        pi/service/pi-daemon          argv -> Service().run()
framework    pi/service/daemon_service/
               app.py      Service        lifecycle, single instance, tick
               state.py    ServiceState   every path the service writes
               config.py   ServiceConfig  every tunable, resolved once
               wire.py     wire           request/response shapes
               endpoint.py Endpoint       loopback control endpoint
               router.py   Router         command table -> domain handler
domains      tasks/      Ticket, TicketStore, TicketRunner, TaskControl
             sessions/   (phase 2) hosted sessions, run state, revive
             comms/      (phase 3) agents, relay, mailbox, peers
```

Rules the code holds itself to:

- A domain owns its state and touches no other domain's. Cross-domain
  work is one call on the owning object, never a shared file or dict.
- The router is the only place that knows which owner serves a wire
  command; a domain never learns another domain's command names.
- The endpoint owns connections and authorization. A handler receives
  a decoded request and returns a response; it never sees a socket.
- Every path comes from `ServiceState`, every tunable from
  `ServiceConfig`. Neither a port, a path, a timeout, nor a default is
  written anywhere else.
- A record is written exactly once from its owner, atomically, through
  `ServiceState.write_json`.

## Wire

One newline-delimited JSON object per line, loopback TCP, a per-daemon
random token published in the runtime endpoint file (mode 0600).

The first request on a connection is `hello`:

```json
{"cmd": "hello", "token": "...", "version": 1}
```

answered with the service identity and the command list, so a client
discovers what the daemon serves instead of assuming it. Every later
request on that connection is served without repeating the token. A
wrong token, a non-hello first request, or an unknown version closes
the connection with `unauthorized` or `protocol-mismatch`.

## Domains

- **Tasks** own command tickets: a client submits a shell command for a
  session; the runner executes it detached from the client, the store
  keeps the record and its output, and `wait`, `output`, `list`,
  `cancel`, and `remove` work on it. A ticket outlives the session that
  submitted it.
- **Sessions** (phase 2) own hosted pi processes: spawn, attach, detach,
  resize, revive, run-state, and the conversation index. Nothing else
  starts or stops an agent process.
- **Comms** (phase 3) own agents: registration, relay, mailbox, waiting,
  and peer links. Comms reaches sessions only through the sessions
  domain's public spawn call, so the process owner stays single.

## Migration

The cutover contract, from the installed pi-daemon v2.9.66 plus
pi-teams v0.4.57 to this service, is unchanged in shape and stays the
reason the old tree is kept until its replacement is proven:

1. **Validate off-tree.** Install the branch into a scratch agent home
   (`PI_CODING_AGENT_DIR`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`,
   `PI_SERVICES_DIR` under one `~/tmp` directory), run the suites, then
   a live smoke of every domain. The live daemon is never replaced in
   this step.
2. **Tag the validated state**; no landing before it is verified.
3. **Back up state** by copying `pi-pty-host/` and `pi-teams/` to a
   dated directory and leaving both originals in place.
4. **Import comms state once**: `peers.json`, `mailbox/`, and `hosts/`
   become the comms state; registrations are connection-based and are
   not imported.
5. **Restart, don't migrate, the sessions.** The swap is an ordinary
   daemon restart: stop, reinstall the tagged release, start. Recorded
   sessions revive from their registry, and a session that was mid-turn
   is revived headless from its conversation like any restart.
6. **Retire pi-teams** only after a peer round trip passes against the
   unified service; drop the pin, restart pi sessions so no extension
   registers with two providers.
7. **Verify the invariants**: one comms descriptor, one relay listener,
   no pi-teams process, every hosted session listed, no ticket lost.

Rollback is a reinstall of the two tagged releases plus the dated state
copy; the new service adds files to the existing state root and never
rewrites an old one in place, which is what keeps the rollback a
restore of one directory.

## Compatibility kept

- The control endpoint path and the loopback endpoint-file shape.
- The `session-host` capability descriptor and its activation argv, so
  a still-installed pi-teams release keeps spawning through it.
- The conversation store, `run-state` records, and the state paths a
  ticket record lives at.
- Reaping stays a request the session answers itself, never a signal.

## Settings

The one namespace is `piDaemon`; what `piTeams` holds today becomes
`piDaemon.comms`, with every old environment name honored as a legacy
fallback for one release and the new name winning. The full mapping is
in the table below.

| Today (`piTeams`) | Rebuild (`piDaemon.comms`) | Env today | Env after |
| --- | --- | --- | --- |
| `host` | `comms.host` | `PI_TEAMS_HOST` | `PI_DAEMON_COMMS_HOST` |
| `stateDir` | removed: one state root | `TEAM_ROOT` | — |
| `binDir` | removed: `pi-rc` owns the CLI | `PI_TEAMS_BIN` | — |
| `gcIdleHours` | `comms.gcIdleHours` | `PI_TEAMS_GC_IDLE_HOURS` | `PI_DAEMON_COMMS_GC_IDLE_HOURS` |
| `busyGraceHours` | `comms.busyGraceHours` | `PI_TEAMS_BUSY_GRACE_HOURS` | `PI_DAEMON_COMMS_BUSY_GRACE_HOURS` |
| `restartGraceSeconds` | `comms.restartGraceSeconds` | `PI_TEAMS_RESTART_GRACE` | `PI_DAEMON_COMMS_RESTART_GRACE` |
| `peerGraceSeconds` | `comms.peerGraceSeconds` | `PI_TEAMS_PEER_GRACE` | `PI_DAEMON_COMMS_PEER_GRACE` |
| `spawnWindowMs` | `comms.spawnWindowMs` | `PI_TEAMS_SPAWN_WINDOW` | `PI_DAEMON_COMMS_SPAWN_WINDOW` |
| `waitSeconds` | `comms.waitSeconds` | `PI_TEAMS_WAIT` | `PI_DAEMON_COMMS_WAIT` |
| `stallSeconds` | `comms.stallSeconds` | `PI_TEAMS_STALL` | `PI_DAEMON_COMMS_STALL` |
| `remoteGuard` | `comms.remoteGuard` | `PI_TEAMS_REMOTE_GUARD` | `PI_DAEMON_COMMS_REMOTE_GUARD` |
| `ssh` | `comms.ssh` | `PI_TEAMS_SSH` | `PI_DAEMON_COMMS_SSH` |
| `remoteState` | removed: one state root | `PI_TEAMS_REMOTE_STATE` | — |
| `peerSetup` | `comms.peerSetup` | `PI_TEAMS_PEER_SETUP` | `PI_DAEMON_COMMS_PEER_SETUP` |
| `sessionsRoot` | `piDaemon.sessionsRoot` | `PI_TEAMS_SESSIONS_ROOT` | `PI_DAEMON_SESSIONS_ROOT` |
