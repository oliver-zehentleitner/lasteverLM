from decimal import Decimal

import httpx
import pytest

from lasteverlm_rag.ingest_client import HttpIngest, IngestError


def client(handler) -> HttpIngest:
    return HttpIngest("http://ingest/", httpx.Client(transport=httpx.MockTransport(handler)))


def test_calls_carry_the_users_bearer_and_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("/estimate"):
            return httpx.Response(
                200, json={"charge_cents": "0.40", "per_source_charge_cents": "5"}
            )
        if request.url.path.endswith("/library"):
            return httpx.Response(200, json=[{"id": "l1", "name": "a.pdf"}])
        return httpx.Response(202, json={"id": "s1", "status": "processing"})

    ingest = client(handler)
    assert ingest.add_url("Bearer t", "nb", "https://x.org")["id"] == "s1"
    ingest.add_library("Bearer t", "nb", ["l1"])
    assert ingest.library("Bearer t") == [{"id": "l1", "name": "a.pdf"}]
    assert ingest.url_charge_cents("Bearer t") == Decimal("5.40")
    assert seen == [
        ("POST", "/api/ingest/notebooks/nb/sources", "Bearer t"),
        ("POST", "/api/ingest/notebooks/nb/sources/bulk", "Bearer t"),
        ("GET", "/api/ingest/library", "Bearer t"),
        ("GET", "/api/ingest/estimate", "Bearer t"),
    ]


def test_a_refusal_or_an_outage_is_an_ingest_error():
    refused = client(lambda r: httpx.Response(400, json={"error": "url: bad scheme"}))
    with pytest.raises(IngestError, match="url: bad scheme") as caught:
        refused.add_url("Bearer t", "nb", "ftp://x")
    assert caught.value.status == 400

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(IngestError, match="not reachable") as caught:
        client(down).library("Bearer t")
    assert caught.value.status == 0


def test_empty_library_and_process_calls_do_not_make_http_calls():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    ingest = client(handler)
    assert ingest.add_library("Bearer t", "nb", []) == {"added": [], "already": []}
    assert ingest.process("Bearer t", []) == {"queued": [], "unchanged": []}
    assert seen == []
