import type { ApiConnection } from "./client.ts";
import type { IngestEvent } from "./types.ts";

export interface SseMessage {
  id: string | null;
  event: string;
  data: string;
}

/**
 * Incremental SSE frame parser (WHATWG event-stream rules: `id:`, `event:`, `data:` fields,
 * `:` comment lines such as heartbeats, blank line dispatches).
 */
export class SseParser {
  private buffer = "";
  private id: string | null = null;
  private event = "";
  private data: string[] = [];

  push(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const out: SseMessage[] = [];
    let nl = this.buffer.search(/\r\n|\r|\n/);
    while (nl !== -1) {
      const line = this.buffer.slice(0, nl);
      const sepLen = this.buffer.startsWith("\r\n", nl) ? 2 : 1;
      this.buffer = this.buffer.slice(nl + sepLen);
      const msg = this.line(line);
      if (msg !== null) out.push(msg);
      nl = this.buffer.search(/\r\n|\r|\n/);
    }
    return out;
  }

  private line(line: string): SseMessage | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.event = "";
        return null;
      }
      const msg: SseMessage = { id: this.id, event: this.event || "message", data: this.data.join("\n") };
      this.event = "";
      this.data = [];
      return msg;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    else if (field === "event") this.event = value;
    else if (field === "id") this.id = value;
    return null;
  }
}

export interface IngestStreamHandlers {
  onEvent: (event: IngestEvent) => void;
  onStatus?: (status: "connecting" | "open" | "closed" | "error") => void;
}

/**
 * Follows `GET /ingest/events` with a fetch-based reader (the browser `EventSource` cannot send
 * the bearer token). Reconnects with `Last-Event-ID` after a drop. Returns a stop function.
 */
export function followIngestEvents(
  api: ApiConnection,
  caseId: string,
  handlers: IngestStreamHandlers,
  retryMs = 3000,
): () => void {
  const controller = new AbortController();
  let lastId: string | null = null;
  const stopped = (): boolean => controller.signal.aborted;

  const run = async (): Promise<void> => {
    while (!stopped()) {
      handlers.onStatus?.("connecting");
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (lastId !== null) headers["Last-Event-ID"] = lastId;
        const response = await api.fetchRaw(`/v1/cases/${encodeURIComponent(caseId)}/ingest/events`, {
          headers,
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok || response.body === null) {
          handlers.onStatus?.("error");
          if (response.status === 401 || response.status === 403 || response.status === 404) return;
        } else {
          handlers.onStatus?.("open");
          const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
          const parser = new SseParser();
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            for (const msg of parser.push(value)) {
              if (msg.id !== null) lastId = msg.id;
              try {
                handlers.onEvent(JSON.parse(msg.data) as IngestEvent);
              } catch {
                // A malformed frame is skipped; the stream itself stays open.
              }
            }
          }
          handlers.onStatus?.("closed");
        }
      } catch {
        if (stopped()) return;
        handlers.onStatus?.("error");
      }
      if (stopped()) return;
      await new Promise((resolve) => setTimeout(resolve, retryMs));
    }
  };
  void run();

  return () => {
    controller.abort();
  };
}
