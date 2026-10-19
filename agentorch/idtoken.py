"""ID tokens for calling other Cloud Run services (metadata server on Cloud Run), cached."""

from __future__ import annotations

import asyncio
import time

_cache: dict[str, tuple[str, float]] = {}


def _fetch(audience: str) -> str:
    import google.auth.transport.requests
    from google.oauth2 import id_token

    return id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


async def id_token_for(audience: str) -> str:
    token, fetched = _cache.get(audience, ("", 0.0))
    if not token or time.time() - fetched > 45 * 60:  # tokens live 1h
        token = await asyncio.to_thread(_fetch, audience)
        _cache[audience] = (token, time.time())
    return token
