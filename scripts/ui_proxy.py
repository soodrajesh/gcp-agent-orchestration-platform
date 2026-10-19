"""Local authenticated proxy for the run viewer: http://127.0.0.1:8080 -> Cloud Run, attaching an
impersonated operator token (`gcloud run services proxy` cannot: Cloud Run strips human tokens).

    ORCH_URL=... OPERATOR_SA=... .venv/bin/python scripts/ui_proxy.py
"""

import os

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agentorch.operator import OperatorToken, refuse_local_request

URL = os.environ["ORCH_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
TOKEN = OperatorToken(os.environ["OPERATOR_SA"], URL)
HTTP = httpx.AsyncClient(timeout=120)


async def forward(request: Request) -> Response:
    why = refuse_local_request(request.method, request.headers, PORT)
    if why:
        return Response(f"refused: {why}", status_code=403)
    headers = {"authorization": f"Bearer {TOKEN.get()}"}
    if ct := request.headers.get("content-type"):
        headers["content-type"] = ct
    body = await request.body()
    for attempt in (1, 2):  # one retry: idle keep-alive connections go stale after laptop sleep
        try:
            up = await HTTP.request(
                request.method,
                URL + request.url.path,
                params=request.query_params,
                content=body,
                headers=headers,
            )
            break
        except httpx.TransportError:
            if attempt == 2:
                raise
    return Response(up.content, up.status_code, media_type=up.headers.get("content-type"))


app = Starlette(routes=[Route("/{path:path}", forward, methods=["GET", "POST"])])
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
