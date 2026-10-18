"""Sources are read when they are used: an output or an answer reads the
selected sources not read yet, web pages are checked again on request."""

from lasteverlm_rag.models import SourceState
from lasteverlm_rag.reading import Prepared, could_not_read, wait_until_read

from .conftest import (
    AUTH,
    CONVERSATION,
    NOTEBOOK,
    FakeIngest,
    FakeLLM,
    FakeRetriever,
    FakeStore,
    chunk,
    make_client,
)

OUTPUTS = f"/api/rag/notebooks/{NOTEBOOK}/outputs"
ASK = f"/api/rag/notebooks/{NOTEBOOK}/ask"
REPORT = '{"title": "Sky", "markdown": "The sky is blue. [1]"}'


def notebook():
    store = FakeStore()
    ready = store.add_source(NOTEBOOK, "sky.pdf", summary="About the sky.")
    unread = store.add_source(NOTEBOOK, "grass.pdf", status="added")
    page = store.add_source(NOTEBOOK, "news", summary="A page.", kind="url")
    return store, ready, unread, page


def client_for(settings, store, *replies):
    ingest = FakeIngest(store=store)
    llm = FakeLLM(*replies)
    client = make_client(
        settings, store, FakeRetriever([chunk(1, "The sky is blue.")]), llm, ingest=ingest
    )
    return client, ingest


def test_an_output_reads_its_unread_sources_first_and_waits_for_them(settings):
    store, ready, unread, _ = notebook()
    client, ingest = client_for(settings, store, REPORT)
    r = client.post(OUTPUTS, json={"kind": "report"}, headers=AUTH)
    assert r.status_code == 202 and r.json()["status"] == "waiting"
    assert ingest.acted == [("process", AUTH["Authorization"], [unread])]
    assert client.worker.run_once() == 0  # still being read

    store.sources[unread].update(status="ready", summary="About grass.")
    assert client.worker.run_once() == 1


def test_only_the_selected_sources_are_read(settings):
    store, ready, unread, _ = notebook()
    client, ingest = client_for(settings, store)
    body = {"kind": "report", "source_ids": [ready]}
    assert client.post(OUTPUTS, json=body, headers=AUTH).json()["status"] == "processing"
    assert ingest.calls == []


def test_a_web_page_is_checked_again_only_when_asked(settings):
    store, ready, unread, page = notebook()
    client, ingest = client_for(settings, store)
    body = {"kind": "report", "source_ids": [ready, page], "url_refresh": [page]}
    r = client.post(OUTPUTS, json=body, headers=AUTH)
    assert r.json()["status"] == "waiting"
    assert ingest.acted == [("refetch", AUTH["Authorization"], page)]

    for bad in ([ready], [unread]):  # not a web page, or not selected
        body = {"kind": "report", "source_ids": [ready, page], "url_refresh": bad}
        r = client.post(OUTPUTS, json=body, headers=AUTH)
        assert r.status_code == 422 and "is not a selected web page" in r.json()["error"]


def test_an_answer_reads_what_it_needs_and_says_what_is_still_being_read(settings):
    store, ready, unread, _ = notebook()
    client, ingest = client_for(settings, store, "The sky is blue [1].")
    r = client.post(ASK, json={"question": "?", "conversation_id": CONVERSATION}, headers=AUTH)
    body = r.json()
    assert ingest.acted == [("process", AUTH["Authorization"], [unread])]
    assert body["answer"].startswith("The sky is blue [1].")
    assert "Still being read: grass.pdf." in body["answer"]
    assert [c["n"] for c in body["citations"]] == [1]


def test_waiting_for_sources_ends_when_they_are_read_or_the_time_is_up():
    store = FakeStore()
    source = store.add_source(NOTEBOOK, "a.pdf", status="processing")
    prepared = Prepared(reading=[SourceState(source, "pdf", "processing", source, "a.pdf")])
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 4:
            store.sources[source]["status"] = "ready"

    assert wait_until_read(store, NOTEBOOK, prepared, 60, sleep, lambda: now[0]) == []
    assert now[0] == 4.0

    store.sources[source]["status"] = "processing"
    now[0] = 0.0
    never = lambda seconds: now.__setitem__(0, now[0] + seconds)  # noqa: E731
    assert wait_until_read(store, NOTEBOOK, prepared, 10, never, lambda: now[0]) == ["a.pdf"]
    assert wait_until_read(store, NOTEBOOK, Prepared(), 10, never, lambda: now[0]) == []


def test_the_estimate_lists_reading_and_re_reading_as_lines_of_their_own(settings):
    store, ready, unread, page = notebook()
    client, _ = client_for(settings, store)
    plain = client.post("/api/rag/estimate", json={"kind": "report"}, headers=AUTH).json()
    assert plain["separate"] == []
    body = {"kind": "report", "notebook_id": NOTEBOOK, "url_refresh": [page]}
    r = client.post("/api/rag/estimate", json=body, headers=AUTH).json()
    assert r["separate"] == [
        {
            "library_id": unread,
            "label": "Read grass.pdf",
            "basis": "3 pages",
            "min": 1.5,
            "max": 1.5,
        },
        {
            "library_id": page,
            "label": "Re-read news if it changed",
            "basis": "web page",
            "min": 0.0,
            "max": 0.4,
        },
    ]
    assert r["charge_cents_max"] == plain["charge_cents_max"]  # not added into the total


def test_a_chart_reads_nothing(settings):
    store, ready, unread, _ = notebook()
    client, ingest = client_for(settings, store)
    client.post(OUTPUTS, json={"kind": "chart", "options": {"charts": []}}, headers=AUTH)
    assert ingest.calls == []


def test_an_unread_page_in_url_refresh_is_read_once(settings):
    store, ready, _, page = notebook()
    store.sources[page]["status"] = "added"
    client, ingest = client_for(settings, store)
    body = {"kind": "report", "source_ids": [ready, page], "url_refresh": [page]}
    client.post(OUTPUTS, json=body, headers=AUTH)
    assert ingest.acted == [("process", AUTH["Authorization"], [page])]


def test_waiting_follows_the_link_when_a_changed_page_gets_a_new_library_row():
    store = FakeStore()
    link = store.add_source(NOTEBOOK, "news", kind="url", status="processing")
    # the page was checked again: the link stays, the library id behind it is new
    prepared = Prepared(refreshing=[SourceState(link, "url", "ready", "old-library-id", "news")])
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    assert wait_until_read(store, NOTEBOOK, prepared, 4, sleep, lambda: now[0]) == ["news"]


def test_a_page_being_read_is_not_checked_again(settings):
    store, ready, _, page = notebook()
    store.sources[page]["status"] = "processing"
    client, ingest = client_for(settings, store)
    body = {"kind": "report", "source_ids": [ready, page], "url_refresh": [page]}
    assert client.post(OUTPUTS, json=body, headers=AUTH).status_code == 202
    assert ingest.calls == []


def test_ingest_refusing_to_read_is_a_502_and_nothing_is_created(settings):
    from lasteverlm_rag.ingest_client import IngestError

    store, _, unread, _ = notebook()
    client, ingest = client_for(settings, store)

    def refuse(bearer, library_ids):
        raise IngestError(404, f"unknown library ids: {library_ids[0]}")

    ingest.process = refuse
    r = client.post(OUTPUTS, json={"kind": "report"}, headers=AUTH)
    assert (r.status_code, r.json()) == (
        502,
        {"error": "The source could not be processed. Check it and try again."},
    )
    assert store.outputs == {}


def test_a_source_read_error_is_not_exposed_to_the_user():
    source = SourceState(
        "source-id", "pdf", "failed", "library-id", "private.pdf", "internal validation details"
    )

    message = could_not_read(source)

    assert (
        message
        == "Source 'private.pdf' could not be read. Check that it is readable and try again."
    )
    assert "internal validation details" not in message


def test_an_unreadable_source_api_error_does_not_expose_stored_details(settings):
    store, _, unread, _ = notebook()
    store.sources[unread].update(
        status="failed",
        error="library_ids: List should have at least 1 item after validation, not 0",
    )
    client, ingest = client_for(settings, store)

    response = client.post(OUTPUTS, json={"kind": "report"}, headers=AUTH)

    assert response.status_code == 409
    assert "could not be read" in response.json()["error"]
    assert "library_ids" not in response.text
    assert ingest.acted == []
