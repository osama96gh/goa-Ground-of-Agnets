// Admin-token-authed, read-only memory reads. Backed by the admin route
// GET /admin/participants/{id}/memory in goa-core/src/goa/api/admin.py.
//
// Memory is owner-scoped and normally only the owning participant can read
// it; the deployment admin token bypasses that for reads (never writes), the
// same way /admin/tasks and /admin/blobs expose participant-scoped data to
// operators.

import { request } from "./client";
import { adminAuth } from "./admin";
import type { MemoryEntry } from "../lib/types";

export async function listAdminParticipantMemory(
  participantId: string,
  params?: { key?: string; prefix?: string; tag?: string[] },
): Promise<MemoryEntry[]> {
  const qs = new URLSearchParams();
  if (params?.key) qs.set("key", params.key);
  if (params?.prefix) qs.set("prefix", params.prefix);
  for (const t of params?.tag ?? []) qs.append("tag", t); // repeatable, AND-ed
  const decoded = await request<{ entries: MemoryEntry[] }>(
    `/admin/participants/${participantId}/memory`,
    { authToken: adminAuth(), query: qs },
  );
  return decoded.entries;
}
