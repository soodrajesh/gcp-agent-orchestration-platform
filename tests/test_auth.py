import pytest
from google.oauth2 import id_token
from starlette.requests import Request

from agentorch.auth import GoogleIdTokenAuth, Unauthenticated


def req(authorization: str | None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "headers": headers})


async def test_audience_is_enforced_when_verifying(monkeypatch):
    seen = {}

    def fake_verify(token, request, audience=None):
        seen.update(token=token, audience=audience)
        return {"email": "op@x.iam.gserviceaccount.com", "email_verified": True}

    monkeypatch.setattr(id_token, "verify_oauth2_token", fake_verify)
    caller = await GoogleIdTokenAuth("https://svc.run.app")(req("Bearer abc"))
    assert caller.email == "op@x.iam.gserviceaccount.com"
    assert seen == {"token": "abc", "audience": "https://svc.run.app"}


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer"])
async def test_missing_or_malformed_header_is_rejected(header):
    with pytest.raises(Unauthenticated):
        await GoogleIdTokenAuth("https://svc.run.app")(req(header))


async def test_invalid_token_is_rejected(monkeypatch):
    def boom(*a, **k):
        raise ValueError("Token has wrong audience")

    monkeypatch.setattr(id_token, "verify_oauth2_token", boom)
    with pytest.raises(Unauthenticated, match="wrong audience"):
        await GoogleIdTokenAuth("https://svc.run.app")(req("Bearer abc"))


async def test_token_without_verified_email_is_rejected(monkeypatch):
    monkeypatch.setattr(
        id_token, "verify_oauth2_token", lambda *a, **k: {"email": "a@b.c", "email_verified": False}
    )
    with pytest.raises(Unauthenticated, match="verified email"):
        await GoogleIdTokenAuth("https://svc.run.app")(req("Bearer abc"))
