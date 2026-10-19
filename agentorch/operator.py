"""Operator-side client: mint an audience-bound token by impersonating the operator service
account, and call the orchestrator with it. Used by scripts/ao.py, scripts/ui_proxy.py, test.sh."""

from __future__ import annotations

import time

import google.auth
import httpx
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request


class OperatorToken:
    """Caches an ID token for `audience`, refreshing a few minutes before expiry."""

    def __init__(self, service_account: str, audience: str) -> None:
        source, _ = google.auth.default()
        target = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=service_account,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        self._creds = impersonated_credentials.IDTokenCredentials(
            target, target_audience=audience, include_email=True
        )
        self._fetched = 0.0

    def get(self) -> str:
        if time.time() - self._fetched > 45 * 60:  # ID tokens live 1h
            self._creds.refresh(Request())
            self._fetched = time.time()
        return self._creds.token


class OrchestratorClient:
    def __init__(self, url: str, service_account: str, timeout: float = 60.0) -> None:
        self._url = url.rstrip("/")
        self._tok = OperatorToken(service_account, self._url)
        self._http = httpx.Client(timeout=timeout)

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {**kw.pop("headers", {}), "authorization": f"Bearer {self._tok.get()}"}
        return self._http.request(method, self._url + path, headers=headers, **kw)

    def start(self, workflow: str, input: dict, run_id: str | None = None) -> dict:
        r = self.request(
            "POST", "/runs", json={"workflow": workflow, "input": input, "run_id": run_id}
        )
        r.raise_for_status()
        return r.json()

    def get(self, run_id: str) -> dict:
        r = self.request("GET", f"/runs/{run_id}")
        r.raise_for_status()
        return r.json()

    def decide(self, run_id: str, step: str, approved: bool) -> dict:
        r = self.request("POST", f"/runs/{run_id}/approvals/{step}", json={"approved": approved})
        r.raise_for_status()
        return r.json()

    def wait(self, run_id: str, until: set[str], timeout: float = 180.0, poll: float = 3.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            run = self.get(run_id)
            if run["status"] in until:
                return run
            time.sleep(poll)
        raise TimeoutError(
            f"run {run_id} did not reach {until} in {timeout}s; last={run['status']}"
        )


def refuse_local_request(method: str, headers, port: int) -> str | None:
    """Gate for the localhost UI proxy, which holds the operator's token. Returns why a request must
    be refused, or None. Blocks (1) DNS rebinding: a hostile page resolving its own name to
    127.0.0.1 sends a foreign Host header; (2) cross-site requests, which browsers mark with Origin /
    Sec-Fetch-Site; (3) "simple" cross-site POSTs, by requiring a JSON content type on writes, which
    forces a CORS preflight this proxy never grants."""
    h = {k.lower(): v for k, v in headers.items()}
    if h.get("host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return "unexpected Host header"
    if h.get("origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        return "cross-origin request"
    if h.get("sec-fetch-site") not in (None, "same-origin", "none"):
        return "cross-site request"
    if method.upper() not in ("GET", "HEAD") and not h.get("content-type", "").startswith(
        "application/json"
    ):
        return "writes must be application/json"
    return None
