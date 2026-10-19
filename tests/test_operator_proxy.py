import pytest

from agentorch.operator import refuse_local_request

OK = {"host": "127.0.0.1:8080"}


def test_plain_local_reads_and_json_writes_are_allowed():
    assert refuse_local_request("GET", OK, 8080) is None
    assert refuse_local_request("POST", {**OK, "content-type": "application/json"}, 8080) is None
    assert (
        refuse_local_request(
            "POST",
            {**OK, "origin": "http://127.0.0.1:8080", "content-type": "application/json"},
            8080,
        )
        is None
    )
    assert refuse_local_request("GET", {"host": "localhost:8080"}, 8080) is None


@pytest.mark.parametrize(
    "host", ["evil.example:8080", "127.0.0.1.evil.example:8080", "127.0.0.1:9999", ""]
)
def test_dns_rebinding_foreign_host_header_is_refused(host):
    assert refuse_local_request("GET", {"host": host}, 8080) == "unexpected Host header"


def test_cross_origin_browser_requests_are_refused():
    h = {**OK, "origin": "https://evil.example", "content-type": "application/json"}
    assert refuse_local_request("POST", h, 8080) == "cross-origin request"
    assert (
        refuse_local_request("GET", {**OK, "sec-fetch-site": "cross-site"}, 8080)
        == "cross-site request"
    )


def test_simple_cross_site_post_without_json_content_type_is_refused():
    """The CSRF that needs no preflight: <form method=post enctype=text/plain> to the local proxy."""
    assert (
        refuse_local_request("POST", {**OK, "content-type": "text/plain"}, 8080)
        == "writes must be application/json"
    )
    assert refuse_local_request("POST", OK, 8080) == "writes must be application/json"
