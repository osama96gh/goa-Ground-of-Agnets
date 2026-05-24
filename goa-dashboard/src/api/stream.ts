// SSE wrapper around the admin firehose at GET /admin/stream.
// Uses fetch-event-source so we can send the Authorization header (the
// browser's native EventSource cannot).

import { fetchEventSource } from "@microsoft/fetch-event-source";
import type { StreamEventFrame, StreamGapData } from "../lib/types";
import { getAdminToken } from "../lib/storage";

export interface FirehoseHandlers {
  onEvent: (frame: StreamEventFrame, lastEventId: string | null) => void;
  onGap: (gap: StreamGapData) => void;
  onError?: (err: unknown) => void;
}

export interface FirehoseHandle {
  close: () => void;
}

export function streamFirehose(handlers: FirehoseHandlers): FirehoseHandle {
  const ctrl = new AbortController();
  const token = getAdminToken();
  if (!token) {
    handlers.onError?.(new Error("admin token missing"));
    return { close: () => ctrl.abort() };
  }
  let lastEventId: string | null = null;

  // Fire-and-forget; fetchEventSource auto-reconnects until aborted.
  void fetchEventSource("/admin/stream", {
    signal: ctrl.signal,
    headers: {
      Authorization: `Bearer ${token}`,
      // fetchEventSource sets Last-Event-ID automatically based on previous
      // server-emitted ids, so explicit header here would just duplicate.
    },
    openWhenHidden: true,
    onmessage(msg) {
      lastEventId = msg.id || lastEventId;
      try {
        const data = msg.data ? JSON.parse(msg.data) : null;
        if (msg.event === "event") {
          handlers.onEvent(data as StreamEventFrame, lastEventId);
        } else if (msg.event === "stream.gap") {
          handlers.onGap(data as StreamGapData);
        }
        // ping is ignored
      } catch (err) {
        handlers.onError?.(err);
      }
    },
    onerror(err) {
      handlers.onError?.(err);
      // Re-throw to keep fetchEventSource's default exponential backoff;
      // returning would kill the connection.
      throw err;
    },
  });

  return {
    close: () => ctrl.abort(),
  };
}
