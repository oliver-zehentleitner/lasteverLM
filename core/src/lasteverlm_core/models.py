"""The API models of the family's contract v1 (parent: docs/architecture.md).

One definition for the rows three services share and the frontend
renders. Ids are `UUID`, timestamps `datetime`; Pydantic serializes both
to strings on the wire, so a service that handled them as strings so far
only casts at its own boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .settings import env_optional

# The user every caller of DEMO_TOKEN resolves to. Seeded by the account
# service's migration; fixed so all services agree on whose rows these are.
DEMO_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
DEMO_USER_EMAIL = "demo@localhost"

SourceKind = Literal["pdf", "text", "url", "image", "audio", "generated"]
SourceStatus = Literal["processing", "ready", "failed"]
OutputKind = Literal["report", "quiz", "table", "mindmap", "infographic", "audio"]
OutputStatus = Literal["waiting", "processing", "ready", "failed"]
# Access to the admin area: a viewer reads it, an admin also changes it.
# Stored in account's `users.role`; `users.is_admin` is `role = 'admin'`.
Role = Literal["user", "viewer", "admin"]
ROLES: tuple[Role, ...] = ("user", "viewer", "admin")
UsageKind = Literal["llm", "embedding", "tts", "stt"]
UsageService = Literal["ingest", "rag"]
MessageRole = Literal["user", "assistant"]


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
    """One chat message; an assistant message carries its citations."""

    id: UUID
    role: MessageRole
    content: str
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime


class Answer(BaseModel):
    """What the ask endpoint returns: the stored message's id, text and citations."""

    message_id: UUID
    answer: str
    citations: list[Citation] = Field(default_factory=list)


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
    """

    status: Literal["ok"] = "ok"
    version: str = UNKNOWN_BUILD
    commit: str = UNKNOWN_BUILD

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Health:
        return cls(
            version=env_optional("APP_VERSION", env) or UNKNOWN_BUILD,
            commit=env_optional("APP_COMMIT", env) or UNKNOWN_BUILD,
        )


class UsageRecord(BaseModel):
    """One row of the shared `usage` table, priced at write time.

    Usage rows are the only debit; the account service sums them against
    the ledger. Nothing else writes a debit.
    """

    user_id: UUID
    notebook_id: UUID | None
    service: UsageService
    kind: UsageKind
    model: str
    # `integer` columns: a value past INT4_MAX fails here, named, not in the insert
    tokens_in: int = Field(ge=0, le=INT4_MAX)
    tokens_out: int = Field(ge=0, le=INT4_MAX)
    cost_cents: int = Field(ge=0, le=INT4_MAX)
