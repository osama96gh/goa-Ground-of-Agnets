// Admin-token-authed blob fetch helpers. Backed by goa-core/src/goa/api/admin.py
// (`/admin/blobs/{id}` and `/admin/blobs/{id}/meta`).

import { GoaError } from "./client";
import type { Attachment } from "../lib/types";
import { getAdminToken } from "../lib/storage";

function adminAuth(): string {
  const t = getAdminToken();
  if (!t) throw new Error("admin token missing — gate should have prevented this");
  return t;
}

export async function getBlobMeta(blobId: string): Promise<Attachment> {
  const response = await fetch(`/admin/blobs/${blobId}/meta`, {
    headers: { Authorization: `Bearer ${adminAuth()}` },
  });
  if (!response.ok) {
    const body = await safeJson(response);
    throw new GoaError(
      body?.error?.code ?? "error",
      body?.error?.message ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return (await response.json()) as Attachment;
}

export async function fetchBlobObjectUrl(blobId: string): Promise<string> {
  // Returns a blob: URL the browser can use as <img src> / <a href>. Caller
  // is responsible for revoking via URL.revokeObjectURL when done.
  const response = await fetch(`/admin/blobs/${blobId}`, {
    headers: { Authorization: `Bearer ${adminAuth()}` },
  });
  if (!response.ok) {
    const body = await safeJson(response);
    throw new GoaError(
      body?.error?.code ?? "error",
      body?.error?.message ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

export async function fetchBlobText(blobId: string): Promise<string> {
  const response = await fetch(`/admin/blobs/${blobId}`, {
    headers: { Authorization: `Bearer ${adminAuth()}` },
  });
  if (!response.ok) {
    const body = await safeJson(response);
    throw new GoaError(
      body?.error?.code ?? "error",
      body?.error?.message ?? `${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return await response.text();
}

async function safeJson(response: Response): Promise<{ error?: { code?: string; message?: string } } | null> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}
