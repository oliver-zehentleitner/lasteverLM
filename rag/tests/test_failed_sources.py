"""A source that could not be read stops an output, loudly and uncharged; a
long run keeps its claim; an output waiting too long for its sources fails."""

import json
from datetime import UTC, datetime, timedelta

from lasteverlm_rag.service import WAITING_TIMEOUT_S

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
from .test_judging import judges, report, revision

OUTPUTS = f"/api/rag/notebooks/{NOTEBOOK}/outputs"
PRICES_PAGE = "https://lasteverlm.zehentleitner.co/prices"
NO_TEXT = "the page has no text without a browser"
REFUSAL = f"Source '{PRICES_PAGE}' could not be read. Check that it is readable and try again."


def oliver():
    """The starter notebook: two ready sources and a web page that failed."""
    store = FakeStore()
    pdf = store.add_source(NOTEBOOK, "handbook.pdf", summary="The handbook.")
    txt = store.add_source(NOTEBOOK, "faq.txt", summary="The FAQ.")
    page = store.add_source(NOTEBOOK, PRICES_PAGE, status="failed", kind="url")
    store.sources[page]["error"] = NO_TEXT
    return store, pdf, txt, page


def client_for(settings, store, *replies, **extra):
    ingest = FakeIngest(store=store)
    retriever = FakeRetriever([chunk(1, "The sky is blue on a clear day.")])
    return (
        make_client(settings, store, retriever, FakeLLM(*replies), ingest=ingest, **extra),
        ingest,
    )


def test_creating_an_output_with_a_failed_source_selected_is_refused(settings):
    store, pdf, txt, page = oliver()
    client, _ = client_for(settings, store)
    body = {"kind": "report", "options": {"judge": "high"}, "source_ids": [pdf, txt, page]}
    r = client.post(OUTPUTS, json=body, headers=AUTH)
    assert (r.status_code, r.json()) == (409, {"error": REFUSAL})
    # nothing selected means all: the failed page is part of it
    r = client.post(OUTPUTS, json={"kind": "report"}, headers=AUTH)
    assert r.status_code == 409
    assert store.outputs == {} and store.usage == []
    # without the page, it runs
    ok = client.post(OUTPUTS, json={"kind": "report", "source_ids": [pdf, txt]}, headers=AUTH)
    assert ok.status_code == 202


def test_a_source_failing_while_the_output_waits_fails_it_before_any_model_call(settings):
    store, pdf, txt, page = oliver()
    store.sources[page]["status"] = "processing"
    client, _ = client_for(settings, store, report("The sky is blue. [1]"))
    body = {"kind": "report", "source_ids": [pdf, txt, page]}
    created = client.post(OUTPUTS, json=body, headers=AUTH).json()
    assert created["status"] == "waiting"
    store.sources[page]["status"] = "failed"
    client.worker.run_once()
    out = client.get(f"/api/rag/outputs/{created['id']}", headers=AUTH).json()
    assert (out["status"], out["error"]) == ("failed", REFUSAL)
    assert store.usage == []  # failed before the first call: nothing charged


def test_a_failed_page_the_user_asks_to_fetch_again_is_retried(settings):
    store, pdf, txt, page = oliver()
    client, ingest = client_for(settings, store)
    body = {"kind": "report", "source_ids": [pdf, txt, page], "url_refresh": [page]}
    r = client.post(OUTPUTS, json=body, headers=AUTH)
    assert r.status_code == 202 and r.json()["status"] == "waiting"
    assert ingest.acted == [("refetch", AUTH["Authorization"], page)]


def test_the_estimate_says_which_source_blocks_and_why(settings):
    store, pdf, txt, page = oliver()
    client, _ = client_for(settings, store)
    body = {"kind": "report", "notebook_id": NOTEBOOK, "source_ids": [pdf, txt, page]}
    r = client.post("/api/rag/estimate", json=body, headers=AUTH).json()
    assert r["blocked"] == [{"library_id": page, "name": PRICES_PAGE, "reason": REFUSAL}]
    assert r["charge_cents_max"] > 0
    body["url_refresh"] = [page]
    assert client.post("/api/rag/estimate", json=body, headers=AUTH).json()["blocked"] == []


def test_an_answer_names_a_selected_source_that_could_not_be_read(settings):
    store, pdf, txt, page = oliver()
    client, _ = client_for(settings, store, "The sky is blue [1].")
    body = {"question": "?", "conversation_id": CONVERSATION, "source_ids": [pdf, page]}
    answer = client.post(f"/api/rag/notebooks/{NOTEBOOK}/ask", json=body, headers=AUTH).json()
    assert answer["answer"].endswith(REFUSAL)


def test_a_long_judged_run_refreshes_its_claim(settings):
    store = FakeStore()
    store.add_source(NOTEBOOK, "sky.pdf", summary="About the sky.")
    now = [0.0]

    class SlowJudge:
        """Every judge call takes 90 s of the fake clock."""

        def __init__(self, model):
            self.model = model
            self.inner = judges()["high"]

        def complete(self, system, user, max_tokens=None, *, thinking=None):
            now[0] += 90
            return self.inner.complete(system, user, max_tokens)

    slow = {"high": SlowJudge("test-judge-high")}
    client = make_client(
        settings,
        store,
        FakeRetriever([chunk(1, "The sky is blue on a clear day.")]),
        FakeLLM(
            report("The sky is blue. [1]", "The sky is wrong green. [1]"),
            revision({"index": 1, "action": "revise", "unit": {"text": "Still wrong. [1]"}}),
        ),
        judges=slow,
    )
    client.app.state.outputs._clock = lambda: now[0]  # type: ignore[attr-defined]
    r = client.post(OUTPUTS, json={"kind": "report", "options": {"judge": "high"}}, headers=AUTH)
    client.worker.run_once()
    out = client.get(f"/api/rag/outputs/{r.json()['id']}", headers=AUTH).json()
    assert out["status"] == "ready", out
    # four judge calls of 90 s: the claim was refreshed during the run; plain beats
    # (no new step) come at most once a minute, step changes come with their own
    assert store.touches and set(store.touches) == {r.json()["id"]}
    assert store.beats and len(store.beats) <= now[0] / 60 + 1


def test_an_output_waiting_too_long_for_its_sources_fails_uncharged(settings):
    store, pdf, txt, page = oliver()
    store.sources[page]["status"] = "processing"
    client, _ = client_for(settings, store)
    body = {"kind": "report", "source_ids": [pdf, page]}
    created = client.post(OUTPUTS, json=body, headers=AUTH).json()
    client.worker.run_once()
    assert store.outputs[created["id"]]["status"] == "waiting"

    old = datetime.now(UTC) - timedelta(seconds=WAITING_TIMEOUT_S + 60)
    store.outputs[created["id"]]["created_at"] = old.isoformat()
    client.worker.run_once()
    out = client.get(f"/api/rag/outputs/{created['id']}", headers=AUTH).json()
    assert (out["status"], out["error"]) == (
        "failed",
        f"Source '{PRICES_PAGE}' did not become ready within 30 minutes",
    )
    assert store.usage == []
    assert json.dumps(out)  # the row is kept, visible with its reason


def test_a_provider_failing_three_times_says_so_names_the_model_and_the_charge(settings):
    import openai
    from lasteverlm_core import ProviderError

    store = FakeStore()
    store.add_source(NOTEBOOK, "sky.pdf", summary="About the sky.")

    class TimingOut(FakeLLM):
        def complete(self, system, user, max_tokens=None, *, thinking=None):
            cause = openai.APITimeoutError(request=None)  # type: ignore[arg-type]
            raise ProviderError("completion request failed: timed out") from cause

    client = make_client(settings, store, FakeRetriever([]), TimingOut())
    output_id = client.post(OUTPUTS, json={"kind": "report"}, headers=AUTH).json()["id"]
    client.worker.run_once()
    assert store.outputs[output_id]["error"] == (
        "the model provider failed (test-llm: timeout); attempt 3 of 3 follows"
    )
    client.worker.run_once()
    out = client.get(f"/api/rag/outputs/{output_id}", headers=AUTH).json()
    assert (out["status"], out["error"]) == (
        "failed",
        "the model provider failed 3 times (test-llm: timeout); no further attempt; "
        "the calls made before the failure were charged",
    )


def test_every_failed_attempt_is_logged_with_model_and_error_class(settings, caplog):
    store = FakeStore()
    store.add_source(NOTEBOOK, "sky.pdf", summary="About the sky.")
    client = make_client(settings, store, FakeRetriever([]), FakeLLM(""))
    output_id = client.post(OUTPUTS, json={"kind": "report"}, headers=AUTH).json()["id"]
    with caplog.at_level("WARNING"):
        client.worker.run_once()
    lines = [r.getMessage() for r in caplog.records if output_id[:8] in r.getMessage()]
    assert lines[0] == (
        f"output {output_id[:8]} (report) attempt 1 of 3 failed, will retry: model test-llm, "
        "ProviderError (empty answer): report: the model returned no content"
    )
    assert "About the sky." not in " ".join(lines)  # never the prompt
