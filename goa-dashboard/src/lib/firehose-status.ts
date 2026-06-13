// Tiny external store for the admin firehose connection state. The firehose
// is owned by Layout (single mount); the topbar and banners subscribe here to
// reflect connection health without opening their own SSE.

import { useSyncExternalStore } from "react";

export type FirehoseState = "connecting" | "open" | "reconnecting" | "closed";

interface Status {
  state: FirehoseState;
  // True after a stream.gap until the next successful REST reload clears it.
  stale: boolean;
}

let current: Status = { state: "connecting", stale: false };
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function setFirehoseState(state: FirehoseState) {
  if (current.state === state) return;
  current = { ...current, state };
  emit();
}

export function setFirehoseStale(stale: boolean) {
  if (current.stale === stale) return;
  current = { ...current, stale };
  emit();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useFirehoseStatus(): Status {
  return useSyncExternalStore(
    subscribe,
    () => current,
    () => current,
  );
}
