"""The API models of the family's contract v1 (parent: docs/architecture.md).

One definition for the rows three services share and the frontend
renders. Ids are `UUID`, timestamps `datetime`; Pydantic serializes both
to strings on the wire, so a service that handled them as strings so far
only casts at its own boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, get_args
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from .settings import env_optional

# The user every caller of DEMO_TOKEN resolves to. Seeded by the account
# service's migration; fixed so all services agree on whose rows these are.
DEMO_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
DEMO_USER_EMAIL = "demo@localhost"

SourceKind = Literal["pdf", "text", "url", "image", "audio", "generated"]
# "waiting_for_credit" is never stored: ingest shows it for a queued
# ("processing") source whose owner has no credit, which its worker skips.
SourceStatus = Literal["processing", "waiting_for_credit", "ready", "failed"]
OutputKind = Literal[
    "report", "quiz", "table", "mindmap", "infographic", "audio", "decision", "chart"
]
OutputStatus = Literal["waiting", "processing", "ready", "failed"]
# Access to the admin area: a viewer reads it, an admin also changes it.
# Stored in account's `users.role`; `users.is_admin` is `role = 'admin'`.
Role = Literal["user", "viewer", "admin"]
ROLES: tuple[Role, ...] = ("user", "viewer", "admin")
# "fee" is an action's fixed fee: cost 0, charge the fee, one per action_id.
UsageKind = Literal["llm", "embedding", "tts", "stt", "decision", "fee"]
UsageService = Literal["ingest", "rag"]
# What a user did that a usage row bills: one list, the services pass one
# per booking. "unknown" marks rows booked before actions existed and is
# never accepted for a new booking.
USAGE_ACTIONS: frozenset[str] = frozenset(
    {
        "chat_answer",
        "chart_propose",
        "summary",
        "judge",
        "decision",
        "voice_sample",
        "demo_seed",
        *(f"output:{kind}" for kind in get_args(OutputKind)),
        *(f"source:{kind}" for kind in get_args(SourceKind) if kind != "generated"),
    }
)
UNKNOWN_ACTION = "unknown"
# How a price list counts an action: once per action_id ("action"), or per
# minute of the audio seconds its tts or stt rows hold in tokens_in
# ("seconds"). `unit` is a stable key the frontend translates.
UnitMeasure = Literal["action", "seconds"]


@dataclass(frozen=True)
class ActionUnit:
    """The unit a user is quoted for one action."""

    unit: str
    measure: UnitMeasure


_SECONDS_ACTIONS = frozenset({"output:audio", "source:audio"})
_UNIT_KEYS = {
    "chat_answer": "question",
    "chart_propose": "chart_proposal",
    "summary": "summary",
    "judge": "check",
    "decision": "decision",
    "voice_sample": "voice_sample",
    "demo_seed": "demo_seed",
}
USAGE_ACTION_UNITS: Mapping[str, ActionUnit] = MappingProxyType(
    {
        action: (
            ActionUnit("audio_minute", "seconds")
            if action in _SECONDS_ACTIONS
            else ActionUnit(
                _UNIT_KEYS.get(action) or ("output" if action.startswith("output:") else "source"),
                "action",
            )
        )
        for action in sorted(USAGE_ACTIONS)
    }
)


def is_usage_action(action: str) -> bool:
    """Whether a new usage row may be booked under `action`."""
    return action in USAGE_ACTIONS


MessageRole = Literal["user", "assistant"]
# What `/healthz` reports per dependency a service uses.
Dependency = Literal["postgres", "storage", "inference"]
DependencyState = Literal["ok", "down"]


class _Row(BaseModel):
    """Base for models that are read straight from database rows."""

    model_config = ConfigDict(from_attributes=True)


class User(_Row):
    """The account view every service and the frontend share."""

    id: UUID
    email: str
    is_admin: bool
    balance_cents: int
    created_at: datetime


class AdminUser(User):
    """A user as the admin overview lists it, with last login and total usage."""

    last_login_at: datetime | None = None
    usage_cents: int


class Notebook(_Row):
    """A notebook: the user's workspace that links library sources and holds chats."""

    id: UUID
    title: str
    created_at: datetime


class Source(_Row):
    """A source as linked into a notebook, with its processing status."""

    id: UUID
    notebook_id: UUID
    name: str
    kind: SourceKind
    status: SourceStatus
    error: str | None = None
    created_at: datetime


class Citation(BaseModel):
    """One `[n]` marker of an answer, mapped back to the chunk it cites.

    `source_kind` lets the frontend render a citation into a PDF
    differently from one into a generated report. `quote` is the verbatim
    chunk text, never trimmed. `similarity` is the chunk's cosine
    similarity to the question (3 decimals), `chunk_index` its 0-based
    position in its source (`chunks.idx`), `chunk_count` the number of
    chunks of that source. The three are optional because messages stored
    before they existed carry none and still validate.
    """

    n: int = Field(ge=1)
    source_id: UUID
    source_name: str
    source_kind: SourceKind
    chunk_id: UUID
    page: int | None = None
    quote: str
    similarity: float | None = None
    chunk_index: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=1)


class ChunkView(BaseModel):
    """One chunk with its place in the source: what a click on a citation
    opens (`GET /api/rag/chunks/{chunk_id}`). `text` is the full chunk."""

    chunk_id: UUID
    source_id: UUID
    source_name: str
    source_kind: SourceKind
    page: int | None = None
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    text: str


class Message(_Row):
    """One chat message; an assistant message carries its citations.

    `units` (one entry per answer paragraph, with its `[n]` refs and
    whether the cited chunks support it) and `check` (the method,
    threshold and counts) are the support check rag runs on an answer,
    shaped as on outputs' provenance; rag owns and documents both. They
    are `None` for an answer without citations and for older messages.
    """

    id: UUID
    role: MessageRole
    content: str
    citations: list[Citation] = Field(default_factory=list)
    units: list[dict[str, Any]] | None = None
    check: dict[str, Any] | None = None
    created_at: datetime


class Answer(BaseModel):
    """What the ask endpoint returns: the stored message's id, text and
    citations, and the support check as on `Message` (`units`, `check`)."""

    message_id: UUID
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    units: list[dict[str, Any]] | None = None
    check: dict[str, Any] | None = None


class Output(_Row):
    """A generated document. `options` holds the resolved generation options
    (language, voice, speed) as rag stores them; older outputs carry `{}`.

    Outputs belong to the user's library and survive their notebook:
    `notebook_id` is `None` once the notebook is deleted, and
    `origin_title` keeps the notebook's title from when the output was
    made (`""` where none was recorded). `deleted_at` and `purge_at` are
    set while the output is in the trash, `None` otherwise. The owner is
    never part of the model: it is always the caller. `provenance` is the
    generation log rag stores with the output (`outputs.provenance`); rag
    owns and documents its shape, `None` for outputs made before it.
    """

    id: UUID
    notebook_id: UUID | None
    origin_title: str = ""
    kind: OutputKind
    title: str
    content: dict[str, Any] | None = None
    status: OutputStatus
    error: str | None = None
    share_token: str | None = None
    source_id: UUID | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    deleted_at: datetime | None = None
    purge_at: datetime | None = None
    provenance: dict[str, Any] | None = None


class SharedOutput(BaseModel):
    """What `GET /api/rag/shared/{token}` returns: the document without ids.
    `provenance` is the output's, with the ids stripped by rag."""

    kind: OutputKind
    title: str
    content: dict[str, Any] | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    provenance: dict[str, Any] | None = None


UNKNOWN_BUILD = "unknown"
INT4_MAX = 2**31 - 1


class Health(BaseModel):
    """What every service answers on `GET /healthz`: liveness plus the build
    it runs, so the admin system view can collect the versions per service.

    `version` and `commit` come from `APP_VERSION` and `APP_COMMIT` (the
    ConfigMap sets both); a value that is unset or blank is `"unknown"`, so
    a probe never fails over a missing label.

    `dependencies` holds the state of what the service uses, only those
    (`DependencyProbe` in `lasteverlm_core.health`). `status` stays `"ok"`
    while the process answers: a dependency that is down is reported here,
    it never fails the liveness probe. A service that reports none (empty)
    serializes without the key, so its body is what it was before.
    """

    status: Literal["ok"] = "ok"
    version: str = UNKNOWN_BUILD
    commit: str = UNKNOWN_BUILD
    dependencies: dict[Dependency, DependencyState] = Field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dependencies: Mapping[Dependency, DependencyState] | None = None,
    ) -> Health:
        return cls(
            version=env_optional("APP_VERSION", env) or UNKNOWN_BUILD,
            commit=env_optional("APP_COMMIT", env) or UNKNOWN_BUILD,
            dependencies=dict(dependencies or {}),
        )

    @model_serializer(mode="wrap")
    def _omit_empty_dependencies(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and not data.get("dependencies"):
            data.pop("dependencies", None)
        return data


class UsageRecord(BaseModel):
    """One row of the shared `usage` table, priced at write time.

    Usage rows are the only debit; the account service sums their
    `charge_cents` against the ledger. Nothing else writes a debit.
    `cost_usd_cents` is the real cost as the platform prices it,
    `cost_cents` the same in euro cents at `usd_per_eur`, `charge_cents`
    what the user pays in euro cents; all exact and fixed when the row is
    booked.
    """

    user_id: UUID
    notebook_id: UUID | None
    service: UsageService
    action: str
    action_id: UUID
    kind: UsageKind
    model: str
    # `integer` columns: a value past INT4_MAX fails here, named, not in the insert
    tokens_in: int = Field(ge=0, le=INT4_MAX)
    tokens_out: int = Field(ge=0, le=INT4_MAX)
    cost_usd_cents: Decimal = Field(ge=0)
    usd_per_eur: Decimal | None = Field(default=None, gt=0)
    cost_cents: Decimal = Field(ge=0)
    charge_cents: Decimal = Field(ge=0)
