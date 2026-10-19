"""Caller identity for the orchestrator API.

Cloud Run IAM already rejects callers without `roles/run.invoker`. This is the second check: the
app reads *who* the caller is from the Google-signed ID token and applies its own allowlists
(operators vs. the Pub/Sub push service account). Signature, issuer, expiry and *audience* are
verified here. Only tokens minted for this service's URL verify: a human's own Google Sign-In
token cannot (Cloud Run's front end alters it), so humans impersonate the operator service
account to obtain one (see terraform/identities.tf)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request


class Unauthenticated(Exception):
    pass


@dataclass(frozen=True)
class Caller:
    email: str


class Authenticator(Protocol):
    async def __call__(self, request: Request) -> Caller: ...


class NoAuth:
    """Tests and local dev only. Never the default in service.py."""

    def __init__(self, email: str = "local@test") -> None:
        self._email = email

    async def __call__(self, request: Request) -> Caller:
        return Caller(self._email)


class GoogleIdTokenAuth:
    def __init__(self, audience: str) -> None:
        self._audience = audience

    async def __call__(self, request: Request) -> Caller:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise Unauthenticated("missing bearer token")
        try:
            claims = await asyncio.to_thread(
                id_token.verify_oauth2_token,
                header[7:],
                google.auth.transport.requests.Request(),
                self._audience,
            )
        except ValueError as exc:
            raise Unauthenticated(f"bad token: {exc}") from exc
        if not claims.get("email") or not claims.get("email_verified"):
            raise Unauthenticated("token has no verified email")
        return Caller(claims["email"])
