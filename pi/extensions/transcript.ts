/**
 * pi-daemon transcript retrieval entry point.
 *
 * Registers `transcript_read`, the read-only view onto the daemon's
 * positional transcript index, so the model can page a conversation
 * instead of pulling the whole session file into context.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { ControlClient } from "./transcript/client.ts";
import { TranscriptTool } from "./transcript/tool.ts";

export default function (pi: ExtensionAPI) {
	new TranscriptTool(new ControlClient()).register(pi);
}
