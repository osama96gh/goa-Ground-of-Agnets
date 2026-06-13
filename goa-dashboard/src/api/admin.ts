// Admin-token-authed read endpoints. Backed by goa-core/src/goa/api/admin.py.

import { request } from "./client";
import type {
  AdminCreateParticipantBody,
  AdminCreateParticipantResponse,
  AdminStats,
  AdminUpdateParticipantBody,
  Event,
  Participant,
  PendingPair,
  Task,
  TaskListItem,
  TaskPage,
  TaskStatus,
} from "../lib/types";
import { getAdminToken } from "../lib/storage";

// Exported so sibling API modules (e.g. memory.ts) reuse the same
// token-missing guard rather than duplicating it.
export function adminAuth(): string {
  const t = getAdminToken();
  if (!t) throw new Error("admin token missing — gate should have prevented this");
  return t;
}

export interface ListAdminTasksParams {
  has_pending?: boolean;
  parent_id?: string | null;
  status?: TaskStatus;
  q?: string;
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string | null;
}

// Full keyset-paginated response — `{ tasks, next_cursor }`.
export async function listAdminTaskPage(
  params?: ListAdminTasksParams,
): Promise<TaskPage> {
  const query: Record<string, string> = {};
  if (params?.has_pending !== undefined) {
    query.has_pending = params.has_pending ? "true" : "false";
  }
  if (params?.parent_id !== undefined && params.parent_id !== null) {
    query.parent_id = params.parent_id;
  } else if (params?.parent_id === null) {
    query.parent_id = "null";
  }
  if (params?.status) query.status = params.status;
  if (params?.q?.trim()) query.q = params.q.trim();
  if (params?.since) query.since = params.since;
  if (params?.until) query.until = params.until;
  if (params?.limit !== undefined) query.limit = String(params.limit);
  if (params?.cursor) query.cursor = params.cursor;
  // Wire shape: { tasks: [{task, pending_questions}], next_cursor }.
  return request<TaskPage>("/admin/tasks", {
    authToken: adminAuth(),
    query,
  });
}

// Convenience wrapper returning just the items (used by the command palette
// and callers that don't paginate).
export async function listAdminTasks(
  params?: ListAdminTasksParams,
): Promise<TaskListItem[]> {
  const page = await listAdminTaskPage(params);
  return page.tasks;
}

export async function closeAdminTask(taskId: string): Promise<Task> {
  const decoded = await request<{ task: Task }>(
    `/admin/tasks/${taskId}/close`,
    { method: "POST", authToken: adminAuth() },
  );
  return decoded.task;
}

export async function getAdminStats(params?: {
  window?: string;
  recent_limit?: number;
}): Promise<AdminStats> {
  const query: Record<string, string> = {};
  if (params?.window) query.window = params.window;
  if (params?.recent_limit !== undefined) {
    query.recent_limit = String(params.recent_limit);
  }
  return request<AdminStats>("/admin/stats", {
    authToken: adminAuth(),
    query,
  });
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
