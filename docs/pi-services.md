# pi-rpc/1: extension-to-extension services

One contract for an extension that needs another extension's data or
signals: a narrow profile of JSON-RPC 2.0
(<https://www.jsonrpc.org/specification>) carried either in-process over
`pi.events` or over a loopback socket, discovered through the service
directory.

Capability negotiation follows MCP's initialize
(<https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle>)
and LSP's capability exchange
(<https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/>):
a caller checks for a listed method rather than a version number, and a
provider may support a subset.

## 1 Messages

Messages are JSON-RPC 2.0 requests, responses, and notifications, with
one deviation: batches are not supported. A JSON array is rejected with
`-32600`.

- A request carries `id`, `method`, and optional `params`. The caller
  owns the id and its deadline.
- A response carries the request's `id` and exactly one of `result` or
  `error`.
- A notification carries `method` and optional `params`, never `id`, and
  is never answered.
- Methods beginning `rpc.` belong to this profile.

On a socket, each message is one compact JSON object on one
`\n`-terminated UTF-8 line. A line longer than 64 KiB closes the
connection unless `rpc.hello` raised the limit.

## 2 Discovery

A provider publishes a descriptor under the services root:

    <root>/<service>/<provider>.json

```json
{"service": "session-host", "version": 1,
 "protocol": "pi-pty-host/1", "provider": "pi-daemon", "pid": 3721013,
 "endpoint_file": "/run/user/1000/pi-pty-host.sock",
 "activation": {"kind": "exec", "argv": ["pi-rc", "daemon-start"]}}
```

`<service>` is a capability name and never a package name. The root is
`$PI_SERVICES_DIR`, else `$XDG_RUNTIME_DIR/pi-services` on POSIX, else
`<tempdir>/pi-services`; Windows never uses `XDG_RUNTIME_DIR`. A
descriptor is stale when its JSON is malformed, its pid is not alive, or
its endpoint is missing or publishes no dialable coordinates.

Rules for a client:

1. Enumerate every descriptor for the service and keep only those whose
   `protocol` it speaks.
2. Order the survivors by `version` and try the highest first.
3. Ignore unknown fields, methods, and topics everywhere.
4. When nothing usable answers, do the job itself (self-provision)
   instead of failing the task.

`pi-rpc/1` services publish `protocol: "pi-rpc/1"`. A descriptor for a
different capability keeps its own protocol name, as `session-host` does
with `pi-pty-host/1`.

The descriptor holds non-secret coordinates only. A token belongs in the
endpoint file it points at, mode 0600.

## 3 Socket profile

The provider listens on `127.0.0.1` and publishes host, port, and token
in its endpoint file.

- **`rpc.hello`** - the first request on every connection, params
  `{"token": ..., "version": 1}`, result
  `{"service": ..., "version": ..., "methods": [...], "topics": [...],
  "maxFrameBytes"?: ...}`. Any other first request, or a wrong token,
  answers `-32001` and the provider closes the connection.
- **`rpc.cancel`** - notification, params `{"id": <request id>}`. A
  provider that honours it answers the original request with `-32800`; a
  provider that does not simply ignores it, and the caller's own deadline
  decides.
- **`rpc.subscribe`** - request, params `{"topics": [...]}`, result
  `{"topics": <accepted>}`. Signals then arrive as notifications whose
  `method` is the topic and whose `params` are the payload.
- **`rpc.unsubscribe`** - request, params `{"topics": [...]}`.

Subscriptions live exactly as long as their connection: closing the
connection ends them, so no durable subscriber record needs reaping.

Every request has a caller-owned deadline. On expiry the caller sends
`rpc.cancel` when it can and reports `-32004`; a provider never invents a
timeout for its caller.

## 4 In-process profile

Inside one pi session, `pi.events` carries the same messages, so no code
dependency exists between packages.

- `pi-rpc/<service>` - requests, consumer to provider.
- `pi-rpc/<service>/reply` - responses. A consumer ignores ids that are
  not its own.
- `pi-rpc/<service>/signal` - notifications, `method` is the topic.

`pi.events` dispatch is synchronous (`dist/core/event-bus.js`), so a
provider answers `rpc.hello` inside its handler before any `await`. A
consumer that has no reply by the time `emit` returns has no provider in
this session and self-provisions immediately, with no timeout. Two
providers in one session resolve by load order: the first reply wins.

Probe when the capability is used, not in the extension factory, because
a provider may load later. Keep the unsubscribe function and call it on
shutdown rather than relying on pi's reload cleanup.

```ts
type Bus = { emit(c: string, d: unknown): void; on(c: string, h: (d: unknown) => void): () => void };
type Msg = { jsonrpc: "2.0"; id?: string; method?: string; params?: unknown; result?: unknown; error?: { code: number; message: string } };

export function serve(bus: Bus, service: string, version: number, methods: Record<string, (p: any) => unknown>): () => void {
	const reply = (m: Msg) => bus.emit(`pi-rpc/${service}/reply`, m);
	return bus.on(`pi-rpc/${service}`, (d) => {
		const m = d as Msg;
		if (m?.jsonrpc !== "2.0" || typeof m.id !== "string" || typeof m.method !== "string") return;
		if (m.method === "rpc.hello") return reply({ jsonrpc: "2.0", id: m.id, result: { service, version, methods: Object.keys(methods) } });
		const fn = methods[m.method];
		if (!fn) return reply({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: "method not found" } });
		Promise.resolve().then(() => fn(m.params)).then(
			(result) => reply({ jsonrpc: "2.0", id: m.id, result }),
			(e) => reply({ jsonrpc: "2.0", id: m.id, error: { code: -32603, message: String(e?.message ?? e) } }),
		);
	});
}

export function probe(bus: Bus, service: string): unknown {
	const id = crypto.randomUUID();
	let hello: unknown;
	const off = bus.on(`pi-rpc/${service}/reply`, (d) => {
		if ((d as Msg)?.id === id) hello ??= (d as Msg).result;
	});
	bus.emit(`pi-rpc/${service}`, { jsonrpc: "2.0", id, method: "rpc.hello" });
	off();
	return hello; // undefined: no provider in this session -> self-provision
}

export function call(bus: Bus, service: string, method: string, params?: unknown, ms = 30_000): Promise<unknown> {
	const id = crypto.randomUUID();
	return new Promise((resolve, reject) => {
		const timer = setTimeout(() => {
			off();
			reject({ code: -32004, message: "deadline exceeded" });
		}, ms);
		const off = bus.on(`pi-rpc/${service}/reply`, (d) => {
			const m = d as Msg;
			if (m?.id !== id) return;
			clearTimeout(timer);
			off();
			m.error ? reject(m.error) : resolve(m.result);
		});
		bus.emit(`pi-rpc/${service}`, { jsonrpc: "2.0", id, method, params });
	});
}
```

Cancellation in-process means the caller stops waiting: the provider's
work is not interrupted.

## 5 Errors

| Code | Meaning |
|---|---|
| `-32700` | parse error |
| `-32600` | invalid request, including a batch |
| `-32601` | method not found |
| `-32602` | invalid params |
| `-32603` | internal error |
| `-32001` | unauthorized, wrong or missing token |
| `-32002` | busy, retry later |
| `-32003` | unavailable, the provider cannot serve now |
| `-32004` | deadline exceeded |
| `-32800` | cancelled |

Domain outcomes are results, not errors: a service that can decline
answers `{"result": {"status": "declined"}}` rather than inventing a
code.

## 6 Compatibility

Requests are additive. Adding a field, method, or topic keeps the
version. Removing one, or changing what an existing one means, bumps the
descriptor's `version` and, when the wire shape changes, the protocol
name to `pi-rpc/<n>`.

A client on `-32601` falls back to older behaviour it knows; a client
that understands no survivor does the work itself. A provider ignores
unknown params fields instead of rejecting the request.

## 7 Lifetime

The provider publishes its descriptor when it starts and removes it with
its own pid at exit; its own sweep clears stale entries. A client may
run `activation.argv` once and re-read the directory, and never stops,
kills, or deletes a provider: only an explicit terminate or the owner's
idle policy ends a provider or session. A client drops only its own
bindings, and a provider keeps no consumer state across restarts.

## 8 Adoption

- **Adopted:** `session-host` discovery (`pi_services.py`, pi-teams
  `service_registry.py`) publishes and reads the descriptor above, with
  its control protocol kept at `pi-pty-host/1`.
- **Exempt, pi-teams broker `op` frames:** one broker serving its own
  clients and peers, not an extension-to-extension service. A package
  that needs team data gets a `pi-rpc/1` service published for it.
- **Exempt, pi-consult bridge:** a consultant's MCP bridge talking to one
  extension with a per-invocation token.
- **Exempt, agent-mem-struct hook calls:** a subprocess hook, not
  extension communication.
- **Pending:** pi-cache and pi-checklist take the in-process profile when
  one becomes a provider or consumer.

A socket-profile library is written when the first cross-session
`pi-rpc/1` service exists; until then the profile is a contract only.
