from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from goa.auth import generate_api_key, hash_api_key
from goa.deps import AppContext, get_ctx, make_bearer_dependency
from goa.domain.models import Participant
from goa.errors import AUTH_401, VALIDATION_422, error_response


router = APIRouter()
require_participant = make_bearer_dependency()


class CreateParticipantBody(BaseModel):
    """§9.1 registration body. `access_policy` is **reserved** in v2 (§6.1):
    only `"public"` is accepted on create. v3 will turn enforcement on with a
    one-release deprecation window — until then, accepting `"private"` on the
    wire would create participants that look ACL-gated but aren't, so we
    reject it explicitly."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["agent", "service"]
    name: str = Field(min_length=1)
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    access_policy: Literal["public"] = "public"


class CreateParticipantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant: Participant
    api_key: str


class ListParticipantsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participants: list[Participant]


@router.post(
    "/participants",
    status_code=201,
    response_model=CreateParticipantResponse,
    summary="Register a participant",
    responses={**VALIDATION_422},
)
async def create_participant(
    body: CreateParticipantBody,
    ctx: AppContext = Depends(get_ctx),
) -> CreateParticipantResponse:
    """Bootstrap route — the only unauthenticated endpoint. Returns the API key
    once; it is never retrievable again."""

    api_key = generate_api_key()
    digest = hash_api_key(ctx.settings.server_pepper, api_key)
    participant = Participant(
        type=body.type,
        name=body.name,
        description=body.description,
        capabilities=list(body.capabilities),
        access_policy=body.access_policy,
        api_key_hash=digest,
    )
    await ctx.participant_store.create(participant)
    return CreateParticipantResponse(participant=participant, api_key=api_key)


@router.get(
    "/participants",
    response_model=ListParticipantsResponse,
    summary="Search participants",
    responses={**AUTH_401, **VALIDATION_422},
)
async def list_participants(
    capability: list[str] = Query(default_factory=list),
    q: str | None = None,
    type: Literal["agent", "service"] | None = None,
    _caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> ListParticipantsResponse:
    """§9.1 / §11 discovery. `?capability=` is repeatable and **AND-ed** —
    `?capability=summarize&capability=legal` returns participants carrying
    *both* tags. `q` does case-insensitive substring on `name`+`description`.
    `type` filters by participant type."""
    if q is not None and not q.strip():
        # Treat blank q as "no filter" rather than "match-all-empty".
        q = None
    results = await ctx.participant_store.search(capabilities=capability, q=q, type=type)
    return ListParticipantsResponse(participants=results)


@router.get(
    "/participants/{participant_id}",
    response_model=Participant,
    summary="Get a participant",
    responses={
        **AUTH_401,
        404: error_response("`not_found` — no participant with that id."),
        **VALIDATION_422,
    },
)
async def get_participant(
    participant_id: UUID,
    _caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> Participant:
    p = await ctx.participant_store.get(participant_id)
    if p is None:
        # Spec §12 reserves `participant_unknown` (422) for body-level refs;
        # for a single-resource fetch 404 is the right shape. The error handler
        # in goa.errors maps 404 → `{"error":{"code":"not_found",...}}`.
        raise HTTPException(status_code=404, detail="participant not found")
    return p
