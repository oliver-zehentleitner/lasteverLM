"""The agent chat: a plan the user confirms, steps run as the user, every
call booked under the plan's action, and tool arguments only from the
user's own words and library."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from lasteverlm_core import ToolCall, ToolCompletion, Usage

from lasteverlm_rag.agent_service import TIMEOUT_ERROR

from .conftest import (
    AUTH,
    BASE_ENV,
    CONVERSATION,
    NOTEBOOK,
    FakeIngest,
    FakeLLM,
    FakeObjects,
    FakeRetriever,
    FakeStore,
    FakeTTS,
    chunk,
    make_client,
)

ASK = f"/api/rag/notebooks/{NOTEBOOK}/ask"
SKY = chunk(1, "The sky is blue on a clear day.")
URL = "https://example.org/sky"
QUIZ = json.dumps(
    {
        "title": "Sky quiz",
        "questions": [
            {"question": "Colour?", "options": ["blue", "red"], "answer": "blue", "refs": [1]}
        ]
        * 5,
    }
)


class ToolLLM(FakeLLM):
    """FakeLLM with tool calls: each planning call takes the next scripted
    list of (name, arguments); an empty list answers NO_ACTION."""

    def __init__(self, plans, *replies):
        super().__init__(*replies)
        self.plans = list(plans)
        self.planned: list[list[dict]] = []

    def complete_tools(self, system, messages, tools, max_tokens=None, *, thinking=None):
        self.planned.append(messages)
        calls = self.plans.pop(0) if self.plans else []
        usage = Usage("llm", "test-llm", 400, 30)
        if not calls:
            return ToolCompletion("NO_ACTION", [], usage, "stop")
        return ToolCompletion(
            "",
            [ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(calls)],
            usage,
            "tool_calls",
        )


def setup(settings, plans, *replies, ingest=None):
    store = FakeStore()
    store.add_source(NOTEBOOK, "sky.pdf", summary="About the sky.")
    llm = ToolLLM(plans, *replies)
    ingest = ingest or FakeIngest()
    client = make_client(settings, store, FakeRetriever([SKY]), llm, ingest=ingest)
    return store, llm, ingest, client


def ask(client, question):
    r = client.post(ASK, json={"question": question, "conversation_id": CONVERSATION}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def plan_path(plan_id):
    return f"/api/rag/conversations/{CONVERSATION}/plans/{plan_id}"


ACTIONS = [
    ("add_url", {"url": URL}),
    ("create_output", {"kind": "quiz", "options": {"question_count": 5}}),
    ("explain_output", {"output_id": "step:2", "question": "What does it test?"}),
]


def test_a_plain_question_is_answered_as_before_after_the_planning_call(settings):
    store, llm, _, client = setup(settings, [[]], "The sky is blue [1].")
    body = ask(client, "Why is the sky blue?")
    assert body["plan"] is None and body["answer"] == "The sky is blue [1]."
    # planning and answer: one action, the planner saw no passage
    assert len({i for _, i in store.bookings}) == 1 and len(store.bookings) == 3
    assert SKY.text not in json.dumps(llm.planned[0])
    assert "sky.pdf" in llm.planned[0][0]["content"]


def test_a_request_for_actions_returns_a_plan_and_runs_nothing(settings):
    store, _, ingest, client = setup(settings, [ACTIONS])
    body = ask(client, f"Add {URL} and make a quiz of 5 questions, then explain it.")
    plan = body["plan"]
    assert plan["status"] == "proposed"
    assert [(s["tool"], s["valid"], s["state"]) for s in plan["steps"]] == [
        ("add_url", True, "proposed"),
        ("create_output", True, "proposed"),
        ("explain_output", True, "proposed"),
    ]
    # the page's processing is ingest's own action: its own line, not in the plan's total
    assert plan["steps"][0]["estimate"] == {"min": 0.0, "max": 0.0}
    assert plan["steps"][0]["processing"] == {
        "label": "Process the web page",
        "estimate": {"min": 0.42, "max": 0.42},
    }
    assert plan["separate"] == [{"n": 1, "label": "Process the web page", "min": 0.42, "max": 0.42}]
    assert 0 < plan["steps"][1]["estimate"]["min"] <= plan["steps"][1]["estimate"]["max"]
    assert plan["estimate"]["max"] == round(
        plan["steps"][1]["estimate"]["max"] + plan["steps"][2]["estimate"]["max"], 2
    )
    assert plan["steps"][1]["args"]["options"]["question_count"] == 5
    assert "Nothing runs until you confirm it." in body["answer"]
    assert ingest.calls == [] and store.outputs == {}
    messages = client.get(f"/api/rag/conversations/{CONVERSATION}/messages", headers=AUTH).json()
    assert messages[-1]["plan"]["plan_id"] == plan["plan_id"]
    polled = client.get(plan_path(plan["plan_id"]), headers=AUTH).json()
    assert polled == plan


def test_tool_arguments_that_break_the_rules_make_invalid_steps(settings):
    calls = [
        ("add_url", {"url": "https://evil.example/x"}),  # not in the message
        ("create_output", {"kind": "quiz", "options": {"num_questions": 5}}),
        ("create_output", {"kind": "chart", "options": {}}),
        ("explain_output", {"output_id": "step:1"}),
        ("add_from_library", {"query": "ocean"}),
        ("list_sources", {}),
        ("list_outputs", {}),
    ]
    library = [
        {"id": "lib-1", "name": "Ocean currents.pdf", "notebooks": []},
        {"id": "lib-2", "name": "Ocean map.png", "notebooks": [{"id": NOTEBOOK}]},
    ]
    _, _, _, client = setup(settings, [calls], ingest=FakeIngest(library))
    steps = ask(client, "Add my ocean notes and make a quiz.")["plan"]["steps"]
    reasons = [s.get("invalid_reason") for s in steps]
    assert reasons[0] == "the address is not in your message"
    assert "num_questions" in reasons[1]
    assert reasons[2] == "chart cannot be created in a plan"
    assert reasons[3] == "step 1 creates no document"
    # only the library entry not yet in the notebook
    assert steps[4]["valid"] and steps[4]["args"]["library_ids"] == ["lib-1"]
    assert steps[5]["valid"] and reasons[6] == "a plan has at most 6 steps"


def test_a_confirmed_plan_runs_as_the_user_and_books_under_its_action(settings):
    store, _, ingest, client = setup(
        settings, [ACTIONS], QUIZ, "It tests the colour of the sky [1]."
    )
    plan = ask(client, f"Add {URL} and make a quiz of 5 questions, then explain it.")["plan"]
    planning = {i for _, i in store.bookings}
    r = client.post(f"{plan_path(plan['plan_id'])}/confirm", headers=AUTH)
    assert r.status_code == 200, r.text
    running = r.json()
    assert running["status"] == "running"
    # ingest got the user's own bearer; the output waits in the queue
    assert ingest.calls[0] == ("add_url", AUTH["Authorization"], URL)
    states = [s["state"] for s in running["steps"]]
    assert states == ["done", "waiting", "waiting"]
    output_id = running["steps"][1]["result"]["output_id"]
    assert store.outputs[output_id]["booking_id"] == plan_planning_id(planning)

    client.worker.run_once()
    done = client.get(plan_path(plan["plan_id"]), headers=AUTH).json()
    assert done["status"] == "done"
    assert [s["state"] for s in done["steps"]] == ["done", "done", "done"]
    assert done["steps"][1]["result"]["title"] == "Sky quiz"
    explained = done["steps"][2]["result"]["message_id"]
    messages = client.get(f"/api/rag/conversations/{CONVERSATION}/messages", headers=AUTH).json()
    assert any(m["id"] == explained for m in messages)
    assert messages[-1]["content"].startswith("The plan is done.")
    # every call of the plan, the planning call included, under one action_id
    assert {i for _, i in store.bookings} == planning
    assert {a for a, _ in store.bookings} == {"chat_answer", "output:quiz"}


def plan_planning_id(ids):
    (only,) = ids
    return str(only)


def test_a_plan_is_cancelled_or_confirmed_once_and_only_by_its_owner(settings):
    _, _, ingest, client = setup(settings, [ACTIONS, ACTIONS])
    first = ask(client, f"Add {URL} and a quiz.")["plan"]["plan_id"]
    r = client.post(f"{plan_path(first)}/cancel", headers=AUTH)
    assert r.json()["status"] == "cancelled"
    assert {s["state"] for s in r.json()["steps"]} == {"skipped"}
    for verb in ("confirm", "cancel"):
        r = client.post(f"{plan_path(first)}/{verb}", headers=AUTH)
        assert (r.status_code, r.json()) == (
            409,
            {"error": "the plan was confirmed or cancelled already"},
        )
    assert ingest.calls == []
    unknown = f"/api/rag/conversations/{CONVERSATION}/plans/{uuid.uuid4()}"
    assert client.get(unknown, headers=AUTH).status_code == 404
    assert client.post(f"{unknown}/confirm").status_code == 401


def test_a_step_that_fails_does_not_stop_the_others(settings):
    calls = [("add_url", {"url": URL}), ("list_sources", {})]
    _, _, _, client = setup(settings, [calls], ingest=FakeIngest(fail=True))
    plan = ask(client, f"Add {URL} and list my sources.")["plan"]
    done = client.post(f"{plan_path(plan['plan_id'])}/confirm", headers=AUTH).json()
    assert done["status"] == "done"
    first, second = done["steps"]
    assert (first["state"], first["error"]) == (
        "failed",
        "The source could not be processed. Check it and try again.",
    )
    assert second["state"] == "done" and second["result"]["sources"][0]["name"] == "sky.pdf"


def test_a_german_interview_audio_request_falls_back_to_a_priced_plan():
    from lasteverlm_rag.config import Settings

    prices = json.loads(BASE_ENV["MODEL_PRICES"])
    prices["test-tts"] = {"in": 1, "out": 0}
    audio_settings = Settings.from_env(
        {
            **BASE_ENV,
            "DEMO_TOKEN": "demo-secret",
            "MODEL_PRICES": json.dumps(prices),
            "TTS_MODEL": "test-tts",
            "S3_ENDPOINT": "https://objects.example",
            "S3_BUCKET": "audio",
            "S3_ACCESS_KEY": "key",
            "S3_SECRET_KEY": "secret",
        }
    )
    store = FakeStore()
    store.add_source(NOTEBOOK, "sky.pdf", summary="About the sky.")
    ingest = FakeIngest()
    client = make_client(
        audio_settings,
        store,
        FakeRetriever([SKY]),
        ToolLLM([[]]),
        tts=FakeTTS(),
        objects=FakeObjects(),
        ingest=ingest,
    )
    body = ask(client, "erzeugen ein Interview audio auf deutsch bitte")

    assert body["plan"]["status"] == "proposed"
    [step] = body["plan"]["steps"]
    assert step["tool"] == "create_output" and step["args"]["kind"] == "audio"
    assert step["args"]["options"]["audio_style"] == "conversation"
    assert step["args"]["options"]["language"] == "de"
    assert body["plan"]["estimate"]["max"] > 0
    assert "Nothing runs until you confirm it." in body["answer"]
    assert ingest.acted == []


def test_an_audio_request_without_readable_sources_asks_before_planning(settings):
    for unreadable in (False, True):
        store, _, ingest, client = setup(settings, [[]])
        store.sources.clear()
        if unreadable:
            source_id = store.add_source(NOTEBOOK, "broken.pdf", status="failed")
            store.sources[source_id]["error"] = "library_ids: List should have at least 1 item"

        body = ask(client, "erzeugen ein Interview audio auf deutsch bitte")

        assert body["plan"] is None
        if unreadable:
            assert "Select another source first" in body["answer"]
        else:
            assert "Select at least one source first" in body["answer"]
        assert "library_ids" not in body["answer"]
        assert store.plans == {} and store.outputs == {} and ingest.acted == []


def test_ingest_validation_errors_are_readable_in_api_responses(settings, monkeypatch):
    from lasteverlm_rag.ingest_client import IngestError

    store, _, ingest, client = setup(settings, [[]], "The sky is blue [1].")
    store.add_source(NOTEBOOK, "unread.pdf", status="added")

    def refuse(bearer, library_ids):
        raise IngestError(
            422, "library_ids: List should have at least 1 item after validation, not 0"
        )

    monkeypatch.setattr(ingest, "process", refuse)
    response = client.post(
        ASK,
        json={"question": "What is in the sources?", "conversation_id": CONVERSATION},
        headers=AUTH,
    )

    assert response.status_code == 502
    assert response.json() == {
        "error": "The source could not be processed. Check it and try again."
    }
    assert "library_ids" not in response.text


def test_an_output_still_waiting_for_a_source_fails_after_the_timeout_uncharged(settings):
    calls = [("create_output", {"kind": "quiz", "options": {"question_count": 5}})]
    store, _, _, client = setup(settings, [calls])
    store.add_source(NOTEBOOK, "slow.pdf", status="processing")
    plan = ask(client, "Make a quiz of 5 questions.")["plan"]
    running = client.post(f"{plan_path(plan['plan_id'])}/confirm", headers=AUTH).json()
    output_id = running["steps"][0]["result"]["output_id"]
    assert store.outputs[output_id]["status"] == "waiting"
    client.worker.run_once()
    assert store.plans[plan["plan_id"]]["status"] == "running"

    store.plans[plan["plan_id"]]["confirmed_at"] = datetime.now(UTC) - timedelta(minutes=31)
    usage = len(store.usage)
    client.worker.run_once()
    out = client.get(f"/api/rag/outputs/{output_id}", headers=AUTH).json()
    assert (out["status"], out["error"]) == ("failed", TIMEOUT_ERROR)
    final = client.get(plan_path(plan["plan_id"]), headers=AUTH).json()
    assert final["status"] == "failed" and final["steps"][0]["error"] == TIMEOUT_ERROR
    assert len(store.usage) == usage  # nothing ran, nothing was charged


SMOKE_QUESTION = "What is the capital of Zeltavia and on which river does it lie?"


def test_a_question_met_with_read_tools_only_is_answered_not_planned(settings):
    # v0.18.5: the smoke test's question came back as a one-step plan of an
    # explain_output that could not run; read tools alone never make a plan
    for calls in (
        [("explain_output", {"output_id": "step:1"})],
        [("list_sources", {}), ("list_outputs", {})],
        [("explain_output", {"output_id": "step:1"}), ("list_sources", {})],
    ):
        store, _, ingest, client = setup(settings, [calls], "Marrow Bay, on the Quill [1].")
        body = ask(client, SMOKE_QUESTION)
        assert body["plan"] is None, body
        assert body["answer"] == "Marrow Bay, on the Quill [1]." and body["citations"]
        assert store.plans == {} and ingest.calls == []


def test_one_action_tool_is_enough_for_a_plan(settings):
    calls = [("list_sources", {}), ("create_output", {"kind": "report"})]
    _, _, _, client = setup(settings, [calls])
    plan = ask(client, "Make a report.")["plan"]
    assert [s["tool"] for s in plan["steps"]] == ["list_sources", "create_output"]


def test_a_planned_output_reads_unread_sources_as_the_user(settings):
    calls = [("create_output", {"kind": "quiz", "options": {"question_count": 5}})]
    store, _, ingest, client = setup(settings, [calls])
    unread = store.add_source(NOTEBOOK, "grass.pdf", status="added")
    plan = ask(client, "Make a quiz of 5 questions.")["plan"]
    assert plan["separate"] == [
        {
            "library_id": unread,
            "label": "Read grass.pdf",
            "basis": "3 pages",
            "min": 1.5,
            "max": 1.5,
        }
    ]
    assert "reading" not in plan["estimate"]
    running = client.post(f"{plan_path(plan['plan_id'])}/confirm", headers=AUTH).json()
    assert ("process", AUTH["Authorization"], [unread]) in ingest.calls
    output_id = running["steps"][0]["result"]["output_id"]
    assert store.outputs[output_id]["status"] == "waiting"
