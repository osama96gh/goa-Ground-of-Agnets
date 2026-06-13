"""`/memory` — agent-private, cross-task key→document memory.

Every entry is owned by the authenticated caller (`owner_id = caller.id`);
a participant can neither read nor write another's memory, so this never
crosses the task-boundary seal (§7). The store is plain owner-scoped CRUD,
so the router talks to `ctx.memory_store` directly — no service layer and
no SSE fan-out (memory is private state, not an event).

Endpoints (one path, one list-shaped read so empty and missing read the
same — mirrors `GET /participants`):

- `POST   /memory`            upsert `(owner, key)`; 201 on create / 200 on overwrite
- `GET    /memory?key=K`      exact fetch (0 or 1 entries)
- `GET    /memory?prefix=P&tag=a&tag=b`  list by key-prefix and/or AND-ed tags
- `DELETE /memory?key=K | ?prefix=P`     delete one / forget-by-prefix
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from goa.deps import AppContext, get_ctx, make_bearer_dependency
from goa.domain.models import MemoryEntry, Participant, UpsertMemoryBody
from goa.errors import AUTH_401, VALIDATION_422, error_response


router = APIRouter()
require_participant = make_bearer_dependency()


class ListMemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[MemoryEntry]


class DeleteMemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: int


@router.post(
    "/memory",
    response_model=MemoryEntry,
    summary="Store or overwrite a memory entry",
    responses={
        201: {"model": MemoryEntry, "description": "A new entry was created."},
        200: {
            "model": MemoryEntry,
            "description": (
                "An existing entry with the same key was overwritten "
                "(its `id` and `created_at` are preserved)."
            ),
        },
        **AUTH_401,
        409: error_response(
            "`memory_quota_exceeded` — the per-participant entry limit is "
            "reached. Only a brand-new key is rejected; overwriting an "
            "existing key is always allowed."
        ),
        413: error_response(
            "`memory_entry_too_large` — the JSON-encoded value exceeds the "
            "configured per-entry size limit."
        ),
        **VALIDATION_422,
    },
)
async def upsert_memory(
    body: UpsertMemoryBody,
    response: Response,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> MemoryEntry:
    """Upsert a memory entry owned by the caller. Overwrites `value`/`tags`
    for an existing key (preserving `created_at`). `201` on create, `200`
    on overwrite — the same `POST /tasks/upsert` convention. Raises
    `memory_entry_too_large` (413) / `memory_quota_exceeded` (409) per the
    configured caps."""
    entry = MemoryEntry(
        owner_id=caller.id,
        key=body.key,
        value=body.value,
        tags=list(body.tags),
    )
    stored, created = await ctx.memory_store.put_memory(
        entry,
        max_entry_bytes=ctx.settings.memory_max_entry_bytes,
        max_entries=ctx.settings.memory_max_entries_per_owner,
    )
    response.status_code = 201 if created else 200
    return stored


@router.get(
    "/memory",
    response_model=ListMemoryResponse,
    summary="List or recall memory",
    responses={**AUTH_401, **VALIDATION_422},
)
async def list_memory(
    key: str | None = None,
    prefix: str | None = None,
    tag: list[str] = Query(default_factory=list),
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> ListMemoryResponse:
    """List the caller's own memory. `key` is an exact lookup (0 or 1
    entries). Otherwise filter by `prefix` (exact key prefix, not a LIKE
    pattern) and/or repeatable `tag` (AND-ed). With no filters, returns all
    of the caller's entries, ordered by key."""
    if key is not None:
        entry = await ctx.memory_store.get_memory(caller.id, key)
        return ListMemoryResponse(entries=[entry] if entry is not None else [])
    if prefix is not None and not prefix.strip():
        # Blank prefix is "no filter", not "match everything by empty prefix".
        prefix = None
    entries = await ctx.memory_store.list_memory(
        caller.id, key_prefix=prefix, tags=tag or None,
    )
    return ListMemoryResponse(entries=entries)


@router.delete(
    "/memory",
    response_model=DeleteMemoryResponse,
    summary="Delete or forget memory",
    responses={
        200: {"model": DeleteMemoryResponse, "description": "Number of entries removed."},
        400: error_response(
            "`invalid_request` — neither `key` nor `prefix` was provided "
            "(a full-owner wipe is intentionally not expressible here)."
        ),
        **AUTH_401,
        **VALIDATION_422,
    },
)
async def delete_memory(
    key: str | None = None,
    prefix: str | None = None,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> DeleteMemoryResponse:
    """Delete one entry by exact `key`, or forget every entry under
    `prefix`. Exactly one is required — `400` if neither is given. A
    full-owner wipe is intentionally not expressible here (delete the
    participant for that). Returns the number of entries removed."""
    if key is not None:
        deleted = await ctx.memory_store.delete_memory(caller.id, key)
    elif prefix is not None and prefix.strip():
        # Use the raw prefix (matching the GET filter); a whitespace-only
        # prefix is treated as "absent" → 400, never a wipe-everything.
        deleted = await ctx.memory_store.purge_memory(caller.id, key_prefix=prefix)
    else:
        raise HTTPException(status_code=400, detail="key or prefix is required")
    return DeleteMemoryResponse(deleted=deleted)
