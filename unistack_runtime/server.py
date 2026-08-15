"""
Focused graph-runtime — the ONLY component that imports the graph + SDK.

Its whole job is to start an activity and resume it on a human decision. State is
durable (checkpointer), so start and resume can be different requests / processes.
Everything read-only (listing pending approvals, fetching pause history) is NOT here —
it comes from the `hitl_resolutions` Mongo collection (see unistack-api).

Build it with `create_app(sdk, graph, auth=...)` or run it with `unistack serve …`.

**Auth is mandatory and cannot be omitted** — `auth` is a required keyword argument, so an
unauthenticated runtime is not constructible. Starting an activity and approving a pause are
SEPARATE scopes: an identity that can run an agent must not be able to sign off its own
guardrail breaches. The approver is derived from the verified token, never from the request
body, so the audit record cannot be forged by a caller.
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from unistack_runtime.entity import resolve as resolve_entity_key
from unistack_auth import (
    SCOPE_RESOLVE,
    SCOPE_START,
    AuthConfig,
    AuthError,
    Principal,
    make_verifier,
    require_scope,
)
from unistack.core import Resolver


class StartRequest(BaseModel):
    initial_state: dict
    run_id: str | None = None


class ResolveRequest(BaseModel):
    # extra="forbid" so a stale caller still sending `resolved_by` gets a loud 422 rather
    # than a silent 200 with its attribution quietly replaced — a security-relevant no-op is
    # the worst of the available failure modes.
    model_config = ConfigDict(extra="forbid")

    decision: str                      # "approve" | "reject"

    @model_validator(mode="before")
    @classmethod
    def _reject_body_identity(cls, data):
        if isinstance(data, dict) and "resolved_by" in data:
            raise ValueError(
                "resolved_by is no longer accepted — the approver is derived from the "
                "verified token. Remove it from the request body."
            )
        return data


def _result(r) -> dict:
    return {"activity_id": r.activity_id, "status": r.status, "node": r.node, "message": r.message}


def _resolver(principal: Principal) -> Resolver:
    """Verified claims → the audit identity persisted with the resolution."""
    return Resolver(label=principal.label, subject=principal.subject,
                    issuer=principal.issuer, auth_mode=principal.auth_mode)


def _requires(verifier, scope: str):
    """
    A parameter-level dependency (not `dependencies=[...]` on the decorator, which discards
    the return value) so the verified principal actually reaches the handler.

    NOTE: deliberately not `fastapi.security.HTTPBearer` — with auto_error=True it returns
    403 for a MISSING header, inverting the 401/403 distinction this module establishes.
    """
    def dependency(authorization: Annotated[str | None, Header()] = None) -> Principal:
        try:
            return require_scope(verifier.verify(authorization), scope)
        except AuthError as exc:
            raise HTTPException(
                exc.status, exc.detail,
                headers={"WWW-Authenticate": exc.www_authenticate}
                if exc.www_authenticate else None,
            ) from exc
    return dependency


def create_app(sdk, graph, *, auth: AuthConfig,
               entity_key_template: str | None = None) -> FastAPI:
    """A thin FastAPI hosting one compiled graph: start + resolve, nothing else."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # Flush buffered spans on shutdown. This MUST be a lifespan hook, not a
        # `finally:` around uvicorn.run(): on SIGTERM uvicorn exits the process without
        # returning, so the finally never executes and every span still sitting in the
        # BatchSpanProcessor is silently lost — on every deploy, restart and scale-down.
        sdk.close()

    app = FastAPI(title="UniStack graph-runtime", version="0.3.0", lifespan=lifespan)

    verifier = make_verifier(auth)          # holds the JWKS client — built once, not per request
    require_start = _requires(verifier, SCOPE_START)
    require_resolve = _requires(verifier, SCOPE_RESOLVE)

    @app.get("/health")
    def health():
        return {"status": "ok", "workflow": sdk._workflow}

    @app.post("/activities", status_code=201)
    def start_activity(body: StartRequest,
                       principal: Annotated[Principal, Depends(require_start)]):
        # The starter's verified identity goes onto the activity record, where a later
        # --deny-self-approval check can compare it against whoever resolves the pause.
        # `entity_key` says WHAT this run is about, rendered from the agent's own declaration
        # against this request's state. It has to be captured here, at start: telemetry is
        # fail-open, so deriving it from a trace later would make a rate's denominator depend on
        # whether an export succeeded, and it cannot be backfilled once checkpoints are deleted.
        return _result(sdk.start(graph, body.initial_state, body.run_id,
                                 started_by=_resolver(principal),
                                 entity_key=resolve_entity_key(
                                     entity_key_template, body.initial_state, sdk._workflow)))

    @app.post("/activities/{activity_id}/resolve")
    def resolve_activity(activity_id: str, body: ResolveRequest,
                         principal: Annotated[Principal, Depends(require_resolve)]):
        if body.decision not in ("approve", "reject"):
            raise HTTPException(422, "decision must be 'approve' or 'reject'")
        decision = "approved" if body.decision == "approve" else "rejected"
        r = sdk.resume(graph, activity_id, decision, resolved_by=_resolver(principal))
        if r.status == "not_found":
            raise HTTPException(404, r.message or f"no such activity: '{activity_id}'")
        return _result(r)

    return app


__all__ = ["create_app", "AuthConfig"]
