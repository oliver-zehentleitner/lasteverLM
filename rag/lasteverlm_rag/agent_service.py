"""The agent chat: plan, confirm, execute.

A chat message goes to a planning call first (``agent.PLANNER_SYSTEM``,
no passages). Tool calls become a plan the user sees with its estimate;
anything else is answered as before. On confirm, the steps that need
ingest run inside that request with its own bearer (rag stores no
tokens), an output is created as any other (its job waits for the
notebook's processing sources), and an explanation waits until its output
is ready. The worker moves waiting steps on (``advance``), fails an output
still waiting for a source after ``PLAN_TIMEOUT_S`` without charging, and
closes the plan with a summary message. Every call of a plan books under
its action_id.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from lasteverlm_core import Answer, ToolCallingLLM

from .agent import (
    ACTION_TOOLS,
    NO_ACTION,
    PLAN_TIMEOUT_S,
    PLANNER_MAX_TOKENS,
    PLANNER_SYSTEM,
    Context,
    Step,
    build_steps,
    is_correction,
    output_label,
    pages_question,
    public_question,
    requested_audio_options,
    separate,
    sources_in,
    sources_question,
    tool_specs,
    total,
    verification_question,
)
from .citations import inline
from .ingest_client import Ingest, IngestError
from .models import Booking
from .options import LEVEL_OF_JUDGE, OutputOptions, resolve
from .provenance import now
from .reading import reading_lines, selected_uploads
from .service import (
    HISTORY_MESSAGES,
    ConversationNotFound,
    InvalidOptions,
    OutputService,
    RagService,
    check_sources,
    complete_tools_billed,
    require_cover,
    require_credit,
    require_notebook,
    twice,
    verification_level,
)
from .store import Store

log = logging.getLogger(__name__)

EXPLAIN_DEFAULT = "Explain this document: what it covers and its main points."
TIMEOUT_ERROR = "a source did not become ready within 30 minutes"
TERMINAL = ("done", "failed", "skipped")
# how far back a correction looks for the plan it corrects (a run adds its own messages)
CORRECTION_WINDOW = 20

EstimateStep = Callable[[Step, str], dict[str, float]]


class PlanNotFound(Exception):
    """No plan with that id in the caller's conversation."""


class PlanNotOpen(Exception):
    """The plan was confirmed or cancelled already."""


class PlanAsking(Exception):
    """The plan waits for the answer to its question; it cannot run yet."""


class StaleQuestion(Exception):
    """No open question with that id on the plan."""


def answered(question: dict[str, Any] | None) -> list[dict[str, Any]]:
    if question is None:
        return []
    done = [*question.get("history", []), *([question] if "answer" in question else [])]
    return [q for q in (public_question(q) for q in done) if q is not None]


def public(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan as answers, messages and the plan endpoint show it."""
    return {
        "plan_id": plan["id"],
        "status": plan["status"],
        "steps": plan["steps"],
        "estimate": {k: v for k, v in plan["estimate"].items() if k != "reading"},
        # ingest's own charges: pages a step adds, sources an output reads first
        "separate": separate(plan["steps"]) + plan["estimate"].get("reading", []),
        "question": public_question(plan.get("question")),
        # every answered question in order, each with its answer (the last one
        # also stays in "question" once answered, as before)
        "answered": answered(plan.get("question")),
        "notes": plan.get("notes") or [],
        "sources": plan.get("sources") or [],
    }


class AgentService:
    def __init__(
        self,
        store: Store,
        rag: RagService,
        outputs: OutputService,
        llm: Any,
        ingest: Ingest | None,
        estimate_step: EstimateStep,
        default_voice: str,
    ) -> None:
        self._store = store
        self._rag = rag
        self._outputs = outputs
        # without complete_tools (a text-only LLM) the chat answers as before
        self._llm: ToolCallingLLM | None = (
            cast(ToolCallingLLM, llm) if hasattr(llm, "complete_tools") else None
        )
        self._ingest = ingest
        self._estimate = estimate_step
        self._default_voice = default_voice

    # -- planning ----------------------------------------------------------

    def ask(
        self,
        user_id: str,
        notebook_id: str,
        question: str,
        conversation_id: str,
        bearer: str | None,
        output_id: str | None = None,
        source_ids: list[str] | None = None,
        url_refresh: list[str] | None = None,
    ) -> Answer:
        """A plan when the message asks for actions; the usual answer otherwise.
        A scoped question (about one output) is always answered."""
        booking = Booking.new(user_id, notebook_id, "chat_answer")
        if self._llm is not None and bearer and output_id is None:
            planned = self._plan(
                user_id, notebook_id, question, conversation_id, bearer, booking, source_ids
            )
            if planned is not None:
                return planned
        return self._rag.ask(
            user_id,
            notebook_id,
            question,
            conversation_id,
            output_id,
            source_ids,
            booking,
            bearer=bearer,
            url_refresh=url_refresh,
        )

    def _plan(
        self,
        user_id: str,
        notebook_id: str,
        question: str,
        conversation_id: str,
        bearer: str,
        booking: Booking,
        source_ids: list[str] | None = None,
    ) -> Answer | None:
        assert self._llm is not None
        require_notebook(self._store, user_id, notebook_id)
        selection = check_sources(self._store, notebook_id, source_ids)
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None or conversation.owner != user_id:
            raise ConversationNotFound(conversation_id)
        if conversation.notebook_id != notebook_id:
            raise ConversationNotFound(conversation_id)
        require_credit(self._store, user_id)
        question = question.strip()
        outputs = {
            r.id: r.title for r in self._store.list_outputs(notebook_id) if r.status == "ready"
        }
        recent = self._store.list_messages(conversation_id, last=HISTORY_MESSAGES)
        # the planner sees names and the user's own words, never a passage
        earlier = [m.content for m in recent if m.role == "user"]
        uploads = [
            s for s in selected_uploads(self._store, notebook_id, None) if s.status != "failed"
        ]
        names = {s.source_id: s.name for s in uploads}
        selected = {sid: n for sid, n in names.items() if not selection or sid in selection}
        # the sources the last answer cited, among the selected: "just discussed"
        last_answer = next(
            (m for m in reversed(recent) if m.role == "assistant" and m.citations), None
        )
        discussed = list(
            dict.fromkeys(
                str(c.source_id)
                for c in (last_answer.citations if last_answer else [])
                if str(c.source_id) in selected
            )
        )
        correction = is_correction(question)
        if correction:
            # a correction of the last plan: the same documents again, never an
            # answer from the sources
            previous = None
            for m in reversed(self._store.list_messages(conversation_id, last=CORRECTION_WINDOW)):
                if m.plan is not None or m.citations:
                    # the last plan, unless an answer from the sources came after it
                    previous = m.plan
                    break
            if previous is None:
                return self._no_plan_to_correct(notebook_id, conversation_id, question)
            calls = [
                (
                    "create_output",
                    {
                        "kind": step["args"]["kind"],
                        "options": {
                            k: v for k, v in step["args"]["options"].items() if k != "sources"
                        },
                    },
                )
                for step in previous["steps"]
                if step["valid"] and step["tool"] == "create_output"
            ]
            if not calls:
                return self._no_plan_to_correct(notebook_id, conversation_id, question)
            used = sorted(
                {i for st in previous["steps"] for i in st["args"].get("source_ids") or []}
            ) or sorted(selected)
            return self._propose(
                user_id,
                notebook_id,
                question,
                conversation_id,
                bearer,
                booking,
                calls,
                outputs,
                names,
                selected,
                selection,
                discussed,
                avoid=used,
                correction=True,
            )
        documents = self._store.notebook_documents(notebook_id)
        context = "\n".join(
            ["Earlier questions:"]
            + [f"- {inline(q)}" for q in earlier]
            + ["", "Documents in this notebook:"]
            + [f"- {inline(d.name)} ({d.output_kind or d.kind})" for d in documents]
            + ["", "Outputs (id: title):"]
            + [f"- {oid}: {inline(title)}" for oid, title in outputs.items()]
            + ["", "The user's message:", question]
        )
        llm = self._llm
        result = twice(
            "planning",
            lambda: complete_tools_billed(
                self._store,
                llm,
                booking,
                PLANNER_SYSTEM,
                [{"role": "user", "content": context}],
                tool_specs(),
                PLANNER_MAX_TOKENS,
            ),
        )
        calls = (
            [(c.name, copy.deepcopy(c.arguments)) for c in result.tool_calls]
            if result.tool_calls and result.text.strip() != NO_ACTION
            else []
        )
        if not any(name in ACTION_TOOLS for name, _ in calls):
            # reading or explaining alone is what an answer does: a question that
            # the planner met with read tools only is answered, unless a clear
            # creation request needs the deterministic audio fallback.
            options = requested_audio_options(question)
            if options is None:
                return None
            calls = [("create_output", {"kind": "audio", "options": options})]
        return self._propose(
            user_id,
            notebook_id,
            question,
            conversation_id,
            bearer,
            booking,
            calls,
            outputs,
            names,
            selected,
            selection,
            discussed,
        )

    def _propose(
        self,
        user_id: str,
        notebook_id: str,
        question: str,
        conversation_id: str,
        bearer: str,
        booking: Booking,
        calls: list[tuple[str, dict[str, Any]]],
        outputs: dict[str, str],
        names: dict[str, str],
        selected: dict[str, str],
        selection: list[str] | None,
        discussed: list[str],
        avoid: list[str] | None = None,
        correction: bool = False,
    ) -> Answer:
        """The plan for these tool calls: validated, estimated, its open
        choices asked. ``names`` are the notebook's sources, ``selected`` the
        ones the user selected, ``discussed`` those the last answer cited."""
        library = (
            self._library(bearer, notebook_id)
            if any(n == "add_from_library" for n, _ in calls)
            else {}
        )
        # a level the message names is taken as said; "default" is the user's own
        # level, or thorough when theirs is basic (they asked for a check)
        default_level = verification_level(self._store, user_id)
        judge_said: set[int] = set()
        # how the message says to treat the notebook's web pages, per output step
        pages_said = {
            n: str(args["web_pages"])
            for n, (name, args) in enumerate(calls, start=1)
            if name == "create_output" and args.get("web_pages") in ("cached", "fresh")
        }
        for n, (name, args) in enumerate(calls, start=1):
            options = args.get("options")
            if name == "create_output" and isinstance(options, dict):
                if "judge" in options:  # a planner of the older prompt
                    options["verification"] = LEVEL_OF_JUDGE.get(
                        str(options.pop("judge")), "default"
                    )
                if "verification" in options:
                    judge_said.add(n)
                    if options["verification"] == "default":
                        level = default_level if default_level != "basic" else "thorough"
                        options["verification"] = level
        # sources the message writes out, for a planner that left them out
        written = sources_in(question, names)
        for name, args in calls:
            if name == "create_output" and not args.get("sources") and written:
                args["sources"] = written
        named = {
            n
            for n, (name, args) in enumerate(calls, start=1)
            if name == "create_output" and args.get("sources")
        }
        steps = build_steps(
            calls, Context(question, library, outputs, self._default_voice, sources=names)
        )
        # a plan that adds a source makes its documents from all, the new one included
        adds = any(s.valid and s.tool in ("add_url", "add_from_library") for s in steps)
        if any(s.valid and s.tool == "create_output" for s in steps) and not adds and not selected:
            all_uploads = selected_uploads(self._store, notebook_id, selection)
            unreadable = any(s.status == "failed" for s in all_uploads)
            text = (
                "None of the selected sources can be read. Select another source first."
                if unreadable
                else "Select at least one source first, or add a source to this notebook."
            )
            self._store.insert_message(notebook_id, conversation_id, "user", question, None)
            message = self._store.insert_message(
                notebook_id, conversation_id, "assistant", text, []
            )
            return Answer(message_id=message.id, answer=text)
        for step in steps:
            if step.valid and step.tool == "create_output" and selection and not adds:
                step.args.setdefault("source_ids", list(selection))
        for step in steps:
            if not step.valid:
                continue
            if step.tool == "add_url":
                # linking the page costs the plan nothing; ingest charges its processing
                # under its own action, the price /prices shows for "add a web page"
                step.processing = {
                    "label": "Process the web page",
                    "estimate": self._estimate(step, bearer),
                }
            else:
                step.estimate = self._estimate(step, bearer)
        plan_id = str(uuid.uuid4())
        steps_json = [s.as_json() for s in steps]
        outputs_steps = [s.n for s in steps if s.valid and s.tool == "create_output"]
        uploads = selected_uploads(self._store, notebook_id, None)
        reads = _reads(steps_json)
        # the web pages the documents read: those of their sources only
        uploads = [u for u in uploads if reads is None or u.source_id in reads]
        # the notebook's web pages read before: their cached version is what an output reads
        pages = [
            {"library_id": s.library_id, "name": s.name, "read_at": s.read_at}
            for s in uploads
            if s.kind == "url" and s.status == "ready"
        ]
        page_ids = [str(p["library_id"]) for p in pages]
        for entry in steps_json:
            if pages and pages_said.get(entry["n"]) == "fresh":
                entry["args"]["url_refresh"] = page_ids
        estimate: dict[str, Any] = dict(total(steps))
        if outputs_steps:
            estimate["reading"] = self._reading(bearer, notebook_id, steps_json)
        # an open choice that changes the cost or the result is asked, never decided
        questions: list[dict[str, Any]] = []
        notes: list[str] = []
        open_sources = [n for n in outputs_steps if n not in named]
        if open_sources and not adds and len(selected) > 1:
            # which sources, when the message names none: asked after a correction,
            # or when the last answer was about only some of them
            just = discussed if 0 < len(discussed) < len(selected) else []
            sources_q = (
                sources_question(str(uuid.uuid4()), open_sources, selected, just, avoid)
                if correction or just
                else None
            )
            if sources_q is not None:
                for choice in sources_q["choices"]:
                    choice["estimate"] = _sum(steps_json)
                questions.append(sources_q)
            elif correction:
                notes.append("Name the document in your message to make it from that one only.")
        open_judge = [n for n in outputs_steps if n not in judge_said]
        if open_judge and default_level != "basic":
            level_q = verification_question(str(uuid.uuid4()), open_judge, default_level)
            for choice in level_q["choices"]:
                choice["estimate"] = _sum(self._apply(level_q["apply"], choice["id"], steps_json))
            questions.append(level_q)
        elif open_judge:
            notes.append(
                "Basic check only (words and negation): the new documents are not checked by "
                "a judge. Ask for one if you want it."
            )
        open_pages = [n for n in outputs_steps if n not in pages_said]
        if open_pages and pages:
            extra = self._refetch_cents(bearer, page_ids)
            pages_q = pages_question(str(uuid.uuid4()), open_pages, pages, datetime.now(UTC), extra)
            for choice in pages_q["choices"]:
                # the plan's own total stays; a refetch is ingest's charge (its label says how much)
                choice["estimate"] = _sum(steps_json)
            questions.append(pages_q)
        plan_question = questions[0] if questions else None
        if plan_question is not None:
            plan_question["next"] = questions[1:]
        sources = self._plan_sources(notebook_id, steps_json)
        plan = {
            "id": plan_id,
            "conversation_id": conversation_id,
            "notebook_id": notebook_id,
            "owner": user_id,
            "status": "asking" if plan_question else "proposed",
            "steps": steps_json,
            "estimate": estimate,
            "action_id": str(booking.action_id),
            "question": plan_question,
            "notes": notes,
            "sources": sources,
        }
        lines = "\n".join(
            f"{s.n}. {s.label}" + ("" if s.valid else f" (not possible: {s.invalid_reason})")
            for s in steps
        )
        if correction:
            lines = "The same again, from the sources you choose:\n\n" + lines
        if plan_question:
            text = (
                f"Here is my plan, with one question first.\n\n{lines}\n\n{plan_question['text']}"
            )
        else:
            text = "Here is my plan. Nothing runs until you confirm it.\n\n" + lines
            if notes:
                text += "\n\n" + "\n".join(notes)
        self._store.insert_message(notebook_id, conversation_id, "user", question, None)
        message = self._store.insert_message(
            notebook_id, conversation_id, "assistant", text, [], None, None, public(plan)
        )
        self._store.create_plan({**plan, "message_id": str(message.id)})
        return Answer(message_id=message.id, answer=text, plan=public(plan))

    def _plan_sources(self, notebook_id: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """What the plan's documents read ("Reads"): the sources their steps
        are made from, all of the notebook's when one is not narrowed, and
        the web pages the plan adds."""
        if not any(s["valid"] and s["tool"] == "create_output" for s in steps):
            return []
        reads = _reads(steps)
        added = [s["args"]["url"] for s in steps if s["valid"] and s["tool"] == "add_url"]
        return [
            {
                "name": s.name,
                "kind": s.kind,
                **({"read_at": s.read_at} if s.kind == "url" and s.read_at else {}),
            }
            for s in selected_uploads(self._store, notebook_id, None)
            if s.status != "failed" and (reads is None or s.source_id in reads)
        ] + [{"name": url, "kind": "url"} for url in added]

    def _no_plan_to_correct(self, notebook_id: str, conversation_id: str, question: str) -> Answer:
        """A correction with no plan to redo (none, or an answer came after
        it): asked back, never answered from the sources (no model call,
        nothing charged)."""
        text = (
            "Which document should I use? Name it in your message, for example "
            '"a mind map from report.pdf", or select it in the sources and ask again.'
        )
        self._store.insert_message(notebook_id, conversation_id, "user", question, None)
        message = self._store.insert_message(notebook_id, conversation_id, "assistant", text, [])
        return Answer(message_id=message.id, answer=text)

    def _library(self, bearer: str, notebook_id: str) -> dict[str, str]:
        """The user's library by id and name, without what the notebook has."""
        if self._ingest is None:
            return {}
        try:
            items = self._ingest.library(bearer)
        except IngestError as exc:
            log.warning("agent: the library could not be read: %s", exc)
            return {}
        return {
            str(i["id"]): str(i.get("name", ""))
            for i in items
            if notebook_id not in [str(n.get("id", n)) for n in i.get("notebooks") or []]
        }

    # -- confirm, cancel, read ------------------------------------------------

    def get(self, user_id: str, conversation_id: str, plan_id: str) -> dict[str, Any]:
        return public(self._owned(user_id, conversation_id, plan_id))

    def answer(
        self,
        user_id: str,
        conversation_id: str,
        plan_id: str,
        question_id: str,
        choice: str | None,
        text: str | None,
        bearer: str | None = None,
    ) -> dict[str, Any]:
        """The user's answer to the plan's question: applied to the steps, the
        estimate updated, the plan proposed. The answer is a turn of the
        conversation like any other message."""
        plan = self._owned(user_id, conversation_id, plan_id)
        open_question = plan.get("question")
        if plan["status"] != "asking" or not open_question:
            raise PlanNotOpen(plan_id)
        if open_question["question_id"] != question_id:
            raise StaleQuestion(question_id)
        labels = {c["id"]: c["label"] for c in open_question["choices"]}
        if choice is None and not (text and open_question.get("free_text")):
            raise InvalidOptions("answer: choose one of the choices")
        if choice is not None and choice not in labels:
            raise InvalidOptions(f"answer: unknown choice {choice!r}")
        if text and not open_question.get("free_text"):
            raise InvalidOptions("answer: this question takes a choice, not text")
        if choice is not None:
            plan["steps"] = self._apply(open_question["apply"], choice, plan["steps"])
            plan["sources"] = self._plan_sources(plan["notebook_id"], plan["steps"])
        estimate: dict[str, Any] = dict(_sum(plan["steps"]))
        if any(s["valid"] and s["tool"] == "create_output" for s in plan["steps"]):
            estimate["reading"] = self._reading(bearer, plan["notebook_id"], plan["steps"])
        plan["estimate"] = estimate
        open_question["answer"] = {
            **({"choice": choice} if choice else {}),
            **({"text": text} if text else {}),
            "at": now(),
        }
        self._store.insert_message(
            plan["notebook_id"], conversation_id, "user", labels.get(choice or "", text or ""), None
        )
        # the next question, if one waits; the answered one goes into the history
        queue = open_question.pop("next", [])
        history = open_question.pop("history", [])
        if queue:
            following = {**queue[0], "next": queue[1:], "history": [*history, open_question]}
            for entry in following["choices"]:
                entry["estimate"] = _sum(
                    self._apply(following["apply"], entry["id"], plan["steps"])
                )
            self._save(
                plan,
                status="asking",
                estimate=estimate,
                question=following,
                sources=plan.get("sources") or [],
            )
        else:
            self._save(
                plan,
                status="proposed",
                estimate=estimate,
                question={**open_question, "history": history},
                sources=plan.get("sources") or [],
            )
        return public(plan)

    def _reading(self, bearer: str | None, notebook_id: str, steps: list[dict[str, Any]]) -> Any:
        """Ingest's lines for what the outputs will read: unread sources, and
        the web pages a step fetches again."""
        if self._ingest is None or not bearer:
            return []
        refresh = sorted(
            {i for s in steps if s["valid"] for i in s["args"].get("url_refresh") or []}
        )
        reads = _reads(steps)
        return reading_lines(
            self._store,
            self._ingest,
            bearer,
            notebook_id,
            sorted(reads) if reads is not None else None,
            refresh,
        )

    def _refetch_cents(self, bearer: str, library_ids: list[str]) -> float:
        """What fetching these pages again may charge at most (ingest's price)."""
        if self._ingest is None or not library_ids:
            return 0.0
        items = self._ingest.estimate_sources(bearer, [], library_ids)
        return float(sum(float(i["charge_cents_max"]) for i in items))

    def _apply(
        self, apply: dict[str, Any], choice: str, steps: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The steps with a choice applied to the named steps: an output
        option set or removed (re-validated, relabelled, re-estimated), or a
        step argument set or removed (``url_refresh``)."""
        value = apply["values"][choice]
        changed = copy.deepcopy(steps)
        for step in changed:
            if step["n"] not in apply["steps"] or not step["valid"]:
                continue
            if apply["kind"] == "set_sources":
                step["args"]["source_ids"] = list(value["source_ids"])
                step["label"] = output_label(
                    step["args"]["kind"], step["args"]["options"], value["names"]
                )
                continue
            if apply["kind"] == "set_arg":
                step["args"].pop(apply["arg"], None)
                if value is not None:
                    step["args"][apply["arg"]] = value
                continue
            if apply["kind"] != "set_option":
                raise ValueError(f"unknown question kind {apply['kind']!r}")
            options = {k: v for k, v in step["args"]["options"].items()}
            options.pop(apply["option"], None)
            for other in apply.get("clears", []):
                options.pop(other, None)  # the level replaces a legacy judge
            if value is not None:
                options[apply["option"]] = value
            kind = step["args"]["kind"]
            options = resolve(kind, OutputOptions.model_validate(options), self._default_voice)
            step["args"] = {"kind": kind, "options": options}
            step["label"] = output_label(kind, options)
            step["estimate"] = self._estimate(
                Step(step["n"], step["tool"], step["args"], step["label"]), ""
            )
        return changed

    def cancel(self, user_id: str, conversation_id: str, plan_id: str) -> dict[str, Any]:
        plan = self._owned(user_id, conversation_id, plan_id)
        if plan["status"] == "asking":
            plan["status"] = "proposed"  # a plan may be cancelled while it asks
        if plan["status"] != "proposed":
            raise PlanNotOpen(plan_id)
        for step in plan["steps"]:
            step["state"] = "skipped"
        self._save(plan, status="cancelled", finished_at=datetime.now(UTC))
        return public(plan)

    def confirm(
        self, user_id: str, conversation_id: str, plan_id: str, bearer: str
    ) -> dict[str, Any]:
        """Run the plan: ingest steps now with this request's bearer, outputs
        created now (their jobs wait for sources), explanations when their
        output is ready (the worker). Credit is checked here, once: the balance
        must cover the plan's maximum, ingest's lines and what started work
        still holds; a confirmed plan then runs all its steps, even if the
        balance goes negative on the way."""
        plan = self._owned(user_id, conversation_id, plan_id)
        if plan["status"] == "asking":
            raise PlanAsking(plan_id)
        if plan["status"] != "proposed":
            raise PlanNotOpen(plan_id)
        # the plan's maximum and ingest's lines (pages it adds, sources read first)
        shown = public(plan)
        require_cover(
            self._store,
            user_id,
            float(shown["estimate"].get("max", 0)) + sum(line["max"] for line in shown["separate"]),
        )
        self._save(plan, status="running", confirmed_at=datetime.now(UTC))
        notebook_id = plan["notebook_id"]
        booking = Booking(user_id, notebook_id, "chat_answer", uuid.UUID(plan["action_id"]))
        for step in plan["steps"]:
            if not step["valid"]:
                step["state"] = "skipped"
                continue
            step["state"] = "running"
            try:
                self._run_now(plan, step, user_id, notebook_id, bearer, booking)
            except Exception as exc:  # a step's failure never stops the others
                step["state"], step["error"] = "failed", _public_error(exc)
            self._save(plan)
        self._store.insert_message(
            notebook_id,
            conversation_id,
            "assistant",
            "Running the plan. Its steps show their progress; I will report when it is done.",
            [],
        )
        self.advance_plan(plan)
        return public(plan)

    def _run_now(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        user_id: str,
        notebook_id: str,
        bearer: str,
        booking: Booking,
    ) -> None:
        tool, args = step["tool"], step["args"]
        if tool in ("add_url", "add_from_library"):
            if self._ingest is None:
                raise IngestError(0, "ingest is not configured")
            if tool == "add_url":
                source = self._ingest.add_url(bearer, notebook_id, args["url"])
                step["result"] = {"source_id": source.get("id"), "status": source.get("status")}
            else:
                added = self._ingest.add_library(bearer, notebook_id, args["library_ids"])
                step["result"] = added
            step["state"] = "done"
        elif tool == "create_output":
            output = self._outputs.create(
                user_id,
                notebook_id,
                args["kind"],
                OutputOptions.model_validate(
                    {k: v for k, v in args["options"].items() if k != "sources"}
                ),
                source_ids=args.get("source_ids"),
                booking_id=plan["action_id"],
                bearer=bearer,  # its sources not read yet are read now, as the user
                url_refresh=args.get("url_refresh"),
                check_credit=False,  # checked once, at confirm
            )
            step["result"] = {"output_id": str(output.id)}
            step["state"] = "waiting"
        elif tool == "explain_output":
            step["state"] = "waiting"  # runs once its output is ready
        elif tool == "list_sources":
            step["result"] = {
                "sources": [
                    {"name": s.name, "kind": s.kind}
                    for s in self._store.notebook_documents(notebook_id)
                ]
            }
            step["state"] = "done"
        elif tool == "list_outputs":
            step["result"] = {
                "outputs": [
                    {"output_id": r.id, "title": r.title, "kind": r.kind, "status": r.status}
                    for r in self._store.list_outputs(notebook_id)
                ]
            }
            step["state"] = "done"

    # -- the worker's part ----------------------------------------------------

    def advance(self) -> int:
        """Move every running plan on; returns how many finished."""
        finished = 0
        for plan in self._store.running_plans():
            try:
                if self.advance_plan(plan):
                    finished += 1
            except Exception:
                log.exception("agent: plan %s could not be advanced", plan["id"])
        return finished

    def advance_plan(self, plan: dict[str, Any]) -> bool:
        """Outputs that became ready or failed, explanations whose output is
        ready, the timeout; the summary when nothing waits any more."""
        steps = plan["steps"]
        confirmed = plan.get("confirmed_at") or datetime.now(UTC)
        timed_out = (datetime.now(UTC) - _aware(confirmed)).total_seconds() > PLAN_TIMEOUT_S
        changed = False
        for step in steps:
            if step["state"] != "waiting":
                continue
            if step["tool"] == "create_output":
                changed |= self._advance_output(step, timed_out)
            elif step["tool"] == "explain_output":
                changed |= self._advance_explain(plan, step)
        if changed:
            self._save(plan)
        if any(s["state"] not in TERMINAL for s in steps):
            return False
        done = [s for s in steps if s["state"] == "done"]
        self._save(plan, status="done" if done else "failed", finished_at=datetime.now(UTC))
        lines = [
            f"{'✓' if s['state'] == 'done' else '✗'} {s['n']}. {_done_line(s)}"
            + (f": {s['error']}" if s.get("error") else "")
            for s in steps
            if s["state"] != "skipped"
        ]
        # the final plan rides along: each created output's id and title, to link it
        self._store.insert_message(
            plan["notebook_id"],
            plan["conversation_id"],
            "assistant",
            "The plan is done.\n\n" + "\n".join(lines),
            [],
            None,
            None,
            public(plan),
        )
        return True

    def _advance_output(self, step: dict[str, Any], timed_out: bool) -> bool:
        output_id = step["result"]["output_id"]
        row = self._store.get_output(output_id)
        if row is None or row.deleted_at is not None:
            step["state"], step["error"] = "failed", "the output was deleted"
            return True
        if row.status == "ready":
            step["state"] = "done"
            step["result"] = {"output_id": output_id, "title": row.title}
            return True
        if row.status == "failed":
            step["state"], step["error"] = "failed", row.error or "the output failed"
            return True
        if row.status == "waiting" and timed_out:
            # nothing ran, so nothing is charged; the user sees why it stopped
            self._store.update_output(output_id, status="failed", error=TIMEOUT_ERROR)
            step["state"], step["error"] = "failed", TIMEOUT_ERROR
            return True
        return False

    def _advance_explain(self, plan: dict[str, Any], step: dict[str, Any]) -> bool:
        args = step["args"]
        if "step" in args:
            source = plan["steps"][args["step"] - 1]
            if source["state"] in ("failed", "skipped"):
                step["state"], step["error"] = "failed", f"step {args['step']} did not finish"
                return True
            if source["state"] != "done":
                return False
            output_id = source["result"]["output_id"]
        else:
            output_id = args["output_id"]
        user_id, notebook_id = plan["owner"], plan["notebook_id"]
        booking = Booking(user_id, notebook_id, "chat_answer", uuid.UUID(plan["action_id"]))
        try:
            answer = self._rag.ask(
                user_id,
                notebook_id,
                args.get("question") or EXPLAIN_DEFAULT,
                plan["conversation_id"],
                output_id,
                None,
                booking,
                check_credit=False,  # checked once, at confirm
            )
        except Exception as exc:
            step["state"], step["error"] = "failed", _public_error(exc)
            return True
        step["state"] = "done"
        step["result"] = {"output_id": output_id, "message_id": str(answer.message_id)}
        return True

    # -- helpers -------------------------------------------------------------

    def _owned(self, user_id: str, conversation_id: str, plan_id: str) -> dict[str, Any]:
        plan = self._store.get_plan(plan_id)
        if plan is None or plan["owner"] != user_id or plan["conversation_id"] != conversation_id:
            raise PlanNotFound(plan_id)
        return plan

    def _save(self, plan: dict[str, Any], **fields: Any) -> None:
        for key in ("status", "confirmed_at", "finished_at", "estimate", "question", "sources"):
            if key in fields:
                plan[key] = fields[key]
        self._store.save_plan(plan["id"], steps=plan["steps"], plan=public(plan), **fields)


def _reads(steps: list[dict[str, Any]]) -> set[str] | None:
    """The source ids the plan's documents are made from, or None when one
    of them reads every source."""
    reads: set[str] = set()
    for step in steps:
        if step["valid"] and step["tool"] == "create_output":
            if not step["args"].get("source_ids"):
                return None
            reads |= set(step["args"]["source_ids"])
    return reads


def _done_line(step: dict[str, Any]) -> str:
    """A finished step as the summary names it: a created output by its title."""
    title = (step.get("result") or {}).get("title")
    if step["tool"] == "create_output" and step["state"] == "done" and title:
        return f'Created the {step["args"]["kind"]} "{title}"'
    return str(step["label"])


def _sum(steps: list[dict[str, Any]]) -> dict[str, float]:
    """The plan's own total over its valid steps (ingest's lines stay apart)."""
    valid = [s for s in steps if s["valid"]]
    return {
        "min": round(sum(s["estimate"]["min"] for s in valid), 2),
        "max": round(sum(s["estimate"]["max"] for s in valid), 2),
    }


def _aware(value: Any) -> datetime:
    when = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _public_error(exc: Exception) -> str:
    if isinstance(exc, IngestError):
        return exc.public_message
    from .service import public_error

    return public_error(exc)
