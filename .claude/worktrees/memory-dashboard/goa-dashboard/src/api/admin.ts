// Admin-token-authed read endpoints. Backed by goa-core/src/goa/api/admin.py.

import { request } from "./client";
import type {
  AdminCreateParticipantBody,
  AdminCreateParticipantResponse,
  AdminUpdateParticipantBody,
  Event,
  Participant,
  PendingPair,
  Task,
  TaskListItem,
} from "../lib/types";
import { getAdminToken } from "../lib/storage";

// Exported so sibling API modules (e.g. memory.ts) reuse the same
// token-missing guard rather than duplicating it.
export function adminAuth(): string {
  const t = getAdminToken();
  if (!t) throw new Error("admin token missing — gate should have prevented this");
  return t;
}

export async function listAdminTasks(params?: {
  has_pending?: boolean;
  parent_id?: string | null;
}): Promise<TaskListItem[]> {
  const query: Record<string, string> = {};
  if (params?.has_pending !== undefined) {
    query.has_pending = params.has_pending ? "true" : "false";
  }
  if (params?.parent_id !== undefined && params.parent_id !== null) {
    query.parent_id = params.parent_id;
  } else if (params?.parent_id === null) {
    query.parent_id = "null";
  }
  // Wire shape (Stages 2+3): each item is {task, pending_questions}.
  const decoded = await request<{ tasks: TaskListItem[] }>("/admin/tasks", {
    authToken: adminAuth(),
    query,
  });
  return decoded.tasks;
}

export async function getAdminTask(
  taskId: string,
): Promise<{ task: Task; pending_questions: PendingPair[]; events: Event[] }> {
  return request("/admin/tasks/" + taskId, { authToken: adminAuth() });
}

export async function listAdminParticipants(params?: {
  capability?: string[];
  q?: string;
  type?: "agent" | "service";
}): Promise<Participant[]> {
  const qs = new URLSearchParams();
  for (const cap of params?.capability ?? []) {
    qs.append("capability", cap);
  }
  if (params?.q) qs.set("q", params.q);
  if (params?.type) qs.set("type", params.type);
  const decoded = await request<{ participants: Participant[] }>(
    "/admin/participants",
    { authToken: adminAuth(), query: qs },
  );
  return decoded.participants;
}

export async function createAdminParticipant(
  body: AdminCreateParticipantBody,
): Promise<AdminCreateParticipantResponse> {
  return request<AdminCreateParticipantResponse>("/admin/participants", {
    method: "POST",
    authToken: adminAuth(),
    body,
  });
}

export async function updateAdminParticipant(
  id: string,
  body: AdminUpdateParticipantBody,
): Promise<Participant> {
  return request<Participant>(`/admin/participants/${id}`, {
    method: "PATCH",
    authToken: adminAuth(),
    body,
  });
}

export async function deleteAdminParticipant(id: string): Promise<void> {
  return request<void>(`/admin/participants/${id}`, {
    method: "DELETE",
    authToken: adminAuth(),
  });
}
