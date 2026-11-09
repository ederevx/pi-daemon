/**
 * pi-daemon sessions entry point.
 *
 * Registers `sessions` and `session_stop`: the roster of hosted sessions
 * on this node and the one destructive verb over it. pi-daemon owns the
 * session store and its stopping path, so the tools carry no state of
 * their own - they name a target and render what the domain answered.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { ControlClient } from "./control/client.ts";
import { SessionRoster } from "./sessions/roster.ts";
import { SessionStop } from "./sessions/stop.ts";

export default function (pi: ExtensionAPI) {
	const client = new ControlClient();
	new SessionRoster(client).register(pi);
	new SessionStop(client).register(pi);
}
