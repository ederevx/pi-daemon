/**
 * pi-daemon remote-work guard entry point.
 *
 * Registers the guard that keeps ssh on this host for file transfer only;
 * the policy lives in `guards/remote-work-guard.ts`.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { RemoteWorkGuard } from "./guards/remote-work-guard.ts";

export default function (pi: ExtensionAPI) {
	new RemoteWorkGuard().register(pi);
}
