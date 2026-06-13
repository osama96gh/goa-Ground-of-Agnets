// Maps GoaError codes / generic failures to human-friendly toast text.

import { GoaError } from "@/api/client";

const FRIENDLY: Record<string, string> = {
  task_not_found: "That task no longer exists.",
  blob_not_found: "That attachment is missing.",
  not_found: "Not found.",
  invalid_event_shape: "The request had an invalid value.",
  invalid_state: "That action isn't allowed in the task's current state.",
  forbidden_role: "You don't have permission for that action.",
  unauthorized: "Your admin session expired — sign in again.",
};

export function friendlyError(err: unknown): string {
  if (err instanceof GoaError) {
    // 401 is handled by the gate bounce; don't double-toast it.
    return FRIENDLY[err.code] ?? err.message ?? "Something went wrong.";
  }
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

// 401s trigger the gate bounce in App.tsx; suppress their toast.
export function isSuppressedError(err: unknown): boolean {
  return err instanceof GoaError && err.status === 401;
}
