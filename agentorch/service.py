"""Orchestrator HTTP API (Cloud Run).

POST /runs                        operator   start a run (idempotent on run_id) and dispatch it
GET  /runs, /runs/{id}            operator   inspect
POST /runs/{id}/approvals/{step}  operator   approve / reject a gated step, then resume the run
POST /internal/advance            push SA    Pub/Sub push endpoint; its status code is the ack
GET  /                            -          run viewer (static page; data calls need a token)
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from importlib.resources import files

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .auth import Authenticator, Caller, Unauthenticated
from .dispatch import Dispatcher
from .engine import Engine, LeaseHeldError, StepRetryError
from .models import Budget
from .store import ConflictError, NotFoundError, RunStore

# Caller-supplied values are validated before they reach storage or a model.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")  # becomes a Firestore document id
MAX_INPUT_BYTES = 16_384  # an alert, not a document: bounds Firestore writes and model spend
MAX_BUDGET = Budget(
    max_tokens=1_000_000, max_cost_usd=5.0
)  # per-run ceiling, whatever the caller asks


class BadRequest(Exception):
    pass


async def read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequest("body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    return body


@dataclass(frozen=True)
class Settings:
    operators: frozenset[str]
    push_sa: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            operators=frozenset(e.strip() for e in os.environ["OPERATORS"].split(",") if e.strip()),
            push_sa=os.environ["PUSH_SA"],
        )


def create_app(
    engine: Engine,
    store: RunStore,
    dispatcher: Dispatcher,
    authn: Authenticator,
    settings: Settings,
    workflows: set[str],
) -> Starlette:
    async def operator(request: Request) -> Caller:
        caller = await authn(request)
        if caller.email not in settings.operators:
            raise PermissionError(caller.email)
        return caller

    def guarded(handler):
        async def wrapper(request: Request) -> Response:
            try:
                return await handler(request)
            except Unauthenticated as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)
            except PermissionError:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            except BadRequest as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except NotFoundError:
                return JSONResponse({"error": "not found"}, status_code=404)

        return wrapper

    async def index(request: Request) -> Response:
        return HTMLResponse(files("agentorch.ui").joinpath("index.html").read_text())

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def start_run(request: Request) -> Response:
        await operator(request)
        body = await read_json(request)
        wf = body.get("workflow")
        if wf not in workflows:
            return JSONResponse(
                {"error": f"unknown workflow; have {sorted(workflows)}"}, status_code=400
            )
        if (halted := await engine.guard.halt_reason()) is not None:
            return JSONResponse({"error": f"platform halted: {halted}"}, status_code=503)
        run_id, run_input = body.get("run_id"), body.get("input", {})
        if run_id is not None and not (isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)):
            raise BadRequest("run_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,62}")
        if not isinstance(run_input, dict):
            raise BadRequest("input must be a JSON object")
        if len(json.dumps(run_input)) > MAX_INPUT_BYTES:
            return JSONResponse(
                {"error": f"input exceeds {MAX_INPUT_BYTES} bytes"}, status_code=413
            )
        budget = None
        if body.get("budget") is not None:
            try:
                budget = Budget.model_validate(body["budget"])
            except ValueError as exc:
                raise BadRequest("budget must be {max_tokens:int, max_cost_usd:number}") from exc
            if not (
                0 < budget.max_tokens <= MAX_BUDGET.max_tokens
                and 0 < budget.max_cost_usd <= MAX_BUDGET.max_cost_usd
            ):
                raise BadRequest(
                    f"budget must be within (0, {MAX_BUDGET.max_tokens}] tokens and (0, ${MAX_BUDGET.max_cost_usd}]"
                )
        run = await engine.start(wf, run_input, run_id=run_id, budget=budget)
        if not run.status.terminal:
            await dispatcher.dispatch(run.id)
        return JSONResponse(json.loads(run.model_dump_json()), status_code=202)

    async def list_runs(request: Request) -> Response:
        await operator(request)
        try:
            limit = max(1, min(int(request.query_params.get("limit", 20)), 100))
        except ValueError as exc:
            raise BadRequest("limit must be an integer") from exc
        runs = await store.list_recent(limit)
        return JSONResponse([json.loads(r.model_dump_json()) for r in runs])

    async def get_run(request: Request) -> Response:
        await operator(request)
        run = await store.get(request.path_params["run_id"])
        return JSONResponse(json.loads(run.model_dump_json()))

    async def decide(request: Request) -> Response:
        caller = await operator(request)
        body = await read_json(request)
        if not isinstance(body.get("approved"), bool):
            return JSONResponse({"error": 'body must be {"approved": true|false}'}, status_code=400)
        run = await engine.decide(
            request.path_params["run_id"],
            request.path_params["step"],
            approved=body["approved"],
            by=caller.email,
        )
        await dispatcher.dispatch(run.id)
        return JSONResponse(json.loads(run.model_dump_json()))

    async def platform_state(request: Request) -> Response:
        await operator(request)
        return JSONResponse(await engine.guard.state())

    async def killswitch(request: Request) -> Response:
        caller = await operator(request)
        body = await read_json(request)
        if not isinstance(body.get("enabled"), bool):
            return JSONResponse({"error": 'body must be {"enabled": true|false}'}, status_code=400)
        await engine.guard.set_killswitch(body["enabled"], caller.email)
        return JSONResponse(await engine.guard.state())

    async def advance(request: Request) -> Response:
        caller = await authn(request)
        if caller.email != settings.push_sa:
            raise PermissionError(caller.email)
        try:
            envelope = await request.json()
            run_id = json.loads(base64.b64decode(envelope["message"]["data"]))["run_id"]
        except (KeyError, ValueError, TypeError):
            return Response(status_code=204)  # malformed message can never succeed: ack, don't loop
        try:
            await engine.advance(run_id)
        except NotFoundError:
            return Response(status_code=204)
        except (LeaseHeldError, ConflictError):
            return JSONResponse({"retry": "leased"}, status_code=409)  # nack -> redelivery
        except StepRetryError as exc:
            return JSONResponse({"retry": str(exc)}, status_code=503)  # nack -> redelivery
        return Response(status_code=204)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/health", healthz),
            Route("/runs", guarded(start_run), methods=["POST"]),
            Route("/runs", guarded(list_runs), methods=["GET"]),
            Route("/runs/{run_id}", guarded(get_run), methods=["GET"]),
            Route("/runs/{run_id}/approvals/{step}", guarded(decide), methods=["POST"]),
            Route("/admin/state", guarded(platform_state), methods=["GET"]),
            Route("/admin/killswitch", guarded(killswitch), methods=["POST"]),
            Route("/internal/advance", guarded(advance), methods=["POST"]),
        ]
    )


def build_from_env() -> Starlette:
    from google.cloud import firestore

    from .auth import GoogleIdTokenAuth
    from .cloudrun import GcpRunAdmin
    from .dispatch import PubSubDispatcher
    from .firestore_store import FirestoreRunStore
    from .workflows.triage import build_triage

    store = FirestoreRunStore(firestore.AsyncClient(project=os.environ["PROJECT_ID"]))
    triage = build_triage(
        os.environ["SPECIALIST_URL"],
        authenticate=True,
        model=os.environ.get("AGENT_MODEL", "gemini-2.5-flash"),
        admin=GcpRunAdmin(os.environ["PROJECT_ID"], os.environ["REGION"]),
        allowed=frozenset(s for s in os.environ["ROLLBACK_ALLOWED_SERVICES"].split(",") if s),
    )
    from .guard import FirestoreGuard

    guard = FirestoreGuard(store._client, float(os.environ.get("DAILY_BUDGET_USD", "1.0")))
    engine = Engine(store, {"triage": triage}, guard=guard)
    dispatcher = PubSubDispatcher(os.environ["PROJECT_ID"], os.environ["TOPIC"])
    app = create_app(
        engine,
        store,
        dispatcher,
        GoogleIdTokenAuth(os.environ["SERVICE_URL"]),
        Settings.from_env(),
        {"triage"},
    )
    from . import obs

    obs.init("ao-orchestrator", app)
    return app


def __getattr__(
    name,
):  # `uvicorn agentorch.service:app`; lazy so importing this module is side-effect free
    if name == "app":
        return build_from_env()
    raise AttributeError(name)
