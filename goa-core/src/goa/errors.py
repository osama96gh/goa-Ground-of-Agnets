from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class GoaError(Exception):
    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class Unauthorized(GoaError):
    def __init__(self, message: str = "missing or invalid bearer token") -> None:
        super().__init__("unauthorized", message, 401)


class Forbidden(GoaError):
    """Generic 403 (spec §12 `forbidden`). Specific cases use the dedicated
    subclasses (`NotAParticipant`, `ForbiddenRole`, etc.)."""

    def __init__(self, message: str = "forbidden") -> None:
        super().__init__("forbidden", message, 403)


class NotAParticipant(GoaError):
    def __init__(self, message: str = "not a participant in this task") -> None:
        super().__init__("not_a_participant", message, 403)


class ForbiddenRole(GoaError):
    def __init__(self, message: str = "this event type is restricted to the task initiator") -> None:
        super().__init__("forbidden_role", message, 403)


class ParentTaskNotVisible(GoaError):
    """Per spec §12. Used both when the parent task does not exist and when
    the caller is not a participant of it — same code, never leak existence."""

    def __init__(self, message: str = "parent task not found or not a participant") -> None:
        super().__init__("parent_task_not_visible", message, 403)


class TaskNotFound(GoaError):
    def __init__(self, message: str = "task not found") -> None:
        super().__init__("task_not_found", message, 404)


class InvalidState(GoaError):
    """Raised by event-append paths when the task is closed (§8 explicit
    task close). Reserved for any future lifecycle preconditions that
    fail an otherwise well-formed request."""

    def __init__(self, message: str = "action incompatible with current task state") -> None:
        super().__init__("invalid_state", message, 409)


class ExternalRefInUse(GoaError):
    """Per spec §12. Raised when a different open task already maps
    `(initiator_id, external_ref)`. Direct `POST /tasks` with a colliding
    `external_ref` returns this; `POST /tasks/upsert` returns the existing
    task instead."""

    def __init__(self, message: str = "external_ref already mapped to another open task") -> None:
        super().__init__("external_ref_in_use", message, 409)


class ParticipantUnknown(GoaError):
    def __init__(self, message: str = "participant id does not resolve to a registered participant") -> None:
        super().__init__("participant_unknown", message, 422)


class NotATarget(GoaError):
    def __init__(self, message: str = "answer references a question that does not target the sender") -> None:
        super().__init__("not_a_target", message, 422)


class InvalidEventShape(GoaError):
    def __init__(self, message: str = "event payload does not match the discriminated-union schema for its event_type") -> None:
        super().__init__("invalid_event_shape", message, 422)


class BlobNotFound(GoaError):
    def __init__(self, message: str = "blob not found") -> None:
        super().__init__("blob_not_found", message, 404)


class BlobForbidden(GoaError):
    """Per spec §12. Caller is neither the uploader nor a participant on any
    task whose event log references the blob."""

    def __init__(self, message: str = "blob not visible to caller") -> None:
        super().__init__("blob_forbidden", message, 403)


class BlobTooLarge(GoaError):
    def __init__(self, message: str = "upload exceeds the configured blob size limit") -> None:
        super().__init__("blob_too_large", message, 413)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GoaError)
    async def _handle_goa_error(_: Request, exc: GoaError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default returns {"detail": [...]} which violates §12.
        # Surface a single human-readable summary; the structured per-field
        # detail is dropped (clients can re-derive from the message).
        errs = exc.errors()
        first = errs[0] if errs else {"loc": (), "msg": "invalid request"}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "invalid request")
        message = f"{loc}: {msg}" if loc else msg
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "message": message}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Catch-all for routes raising HTTPException directly (e.g. 405/404
        # from the router). Map to the §12 envelope.
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "invalid_state",
            413: "blob_too_large",
            422: "invalid_request",
        }.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else "error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
        )
