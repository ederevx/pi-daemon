/**
 * pi-daemon — one ticket's incremental output cursor.
 *
 * Owns a byte offset into a ticket's output log, decodes complete UTF-8
 * as chunks arrive (a sequence split across chunks is held until whole),
 * and retains only a bounded tail. The daemon serves at most one chunk
 * per read, so a pull is bounded by its chunk budget; `behind` records
 * that the budget stopped it with output still unread.
 */

import { StringDecoder } from "node:string_decoder";
import { DEFAULT_MAX_BYTES } from "@earendil-works/pi-coding-agent";

/** The narrow read surface a stream needs: one ticket's bytes since a
 *  byte offset. Deliberately not TicketClient, so streams never import
 *  offload.ts. */
export interface OutputSource {
	outputFrom(id: string, offset: number): Promise<{ data: Buffer; next: number }>;
}

export class TicketStream {
	private offset = 0;
	private readonly decoder = new StringDecoder("utf8");
	private retained = "";
	/** True while the last pull stopped at its chunk budget. */
	behind = false;

	constructor(
		readonly id: string,
		private readonly maxBytes = DEFAULT_MAX_BYTES,
	) {}

	/** Reads up to maxChunks chunks of new output; returns whether any
	 *  bytes arrived. Callers bound the per-tick work with maxChunks. */
	async pull(source: OutputSource, maxChunks: number): Promise<boolean> {
		let pulled = false;
		for (let i = 0; i < maxChunks; i++) {
			const { data, next } = await source.outputFrom(this.id, this.offset);
			if (data.length === 0) {
				this.behind = false;
				return pulled;
			}
			this.offset = next;
			this.retained = this.keepTail(this.retained + this.decoder.write(data));
			pulled = true;
		}
		this.behind = true;
		return pulled;
	}

	/** The bounded decoded tail read so far. */
	tail(): string {
		return this.retained;
	}

	/** Keep the newest maxBytes bytes without splitting a character. */
	private keepTail(text: string): string {
		if (Buffer.byteLength(text, "utf8") <= this.maxBytes) return text;
		let size = 0;
		let start = text.length;
		while (start > 0) {
			const width = Buffer.byteLength(text[start - 1], "utf8");
			if (size + width > this.maxBytes) break;
			size += width;
			start -= 1;
		}
		return text.slice(start);
	}
}
