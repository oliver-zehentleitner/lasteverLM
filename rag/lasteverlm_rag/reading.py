"""Sources are read when they are used.

A source linked into a notebook stays 'added' (not read) until something
needs it. Before an output is created or a question is answered, rag asks
ingest to read the selected sources not read yet (``process``) and to check
again the web pages the user wants fresh (``refetch``, per page). Both run
with the user's own bearer. Ingest marks them 'processing' until done, so an
output job waits for them as it waits for any processing source; an answer
waits a little and then answers from what is ready.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .ingest_client import Ingest
from .models import SourceState
from .store import Store

ANSWER_WAIT_S = 60.0
POLL_S = 2.0
NOT_READ = "added"
STILL_READING = ("added", "processing")


class InvalidRefresh(ValueError):
    """``url_refresh`` names something that is not a selected web page."""


@dataclass
class Prepared:
    """What was started: sources being read, web pages being checked."""

    reading: list[SourceState] = field(default_factory=list)
    refreshing: list[SourceState] = field(default_factory=list)

    @property
    def link_ids(self) -> list[str]:
        """Waited on by link: a changed page moves its link to a new library row."""
        return [s.source_id for s in self.reading + self.refreshing]


def selected_uploads(
    store: Store, notebook_id: str, selected: Sequence[str] | None
) -> list[SourceState]:
    return [
        s
        for s in store.source_states(notebook_id)
        if s.kind != "generated" and (not selected or s.source_id in selected)
    ]


FAILED = "failed"


def failed_sources(
    uploads: Sequence[SourceState], url_refresh: Sequence[str] = ()
) -> list[SourceState]:
    """The selected sources that could not be read; a web page the user asked
    to fetch again is being retried, not failed."""
    retried = set(url_refresh)
    return [s for s in uploads if s.status == FAILED and s.library_id not in retried]


def could_not_read(source: SourceState) -> str:
    """A user-facing failure that does not expose ingest's internal error."""
    return f"Source '{source.name}' could not be read. Check that it is readable and try again."


def check_refresh(uploads: Sequence[SourceState], url_refresh: Sequence[str]) -> list[SourceState]:
    """The web pages to check again, each a selected upload of kind url; a
    page not read yet is read fresh anyway and needs no check."""
    by_library = {s.library_id: s for s in uploads}
    pages = []
    for library_id in dict.fromkeys(url_refresh):
        page = by_library.get(library_id)
        if page is None or page.kind != "url":
            raise InvalidRefresh(f"url_refresh: {library_id} is not a selected web page")
        # not read yet: read fresh anyway; being read: fresh already (ingest refuses a check)
        if page.status not in STILL_READING:
            pages.append(page)
    return pages


def prepare(
    store: Store,
    ingest: Ingest | None,
    bearer: str | None,
    notebook_id: str,
    selected: Sequence[str] | None,
    url_refresh: Sequence[str] = (),
) -> Prepared:
    """Start reading the selected sources not read yet and checking the pages
    to refresh. Without a bearer (a step the worker runs) nothing is started:
    only what is ready is used."""
    uploads = selected_uploads(store, notebook_id, selected)
    pages = check_refresh(uploads, url_refresh)
    unread = [s for s in uploads if s.status == NOT_READ]
    if ingest is None or not bearer:
        return Prepared()
    if unread:
        ingest.process(bearer, [s.library_id for s in unread])
    for page in pages:
        ingest.refetch(bearer, page.source_id)
    return Prepared(unread, pages)


def wait_until_read(
    store: Store,
    notebook_id: str,
    prepared: Prepared,
    timeout: float = ANSWER_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Wait until the started sources are read, at most ``timeout``; the
    names of those still being read."""
    wanted = set(prepared.link_ids)
    if not wanted:
        return []
    deadline = clock() + timeout
    while True:
        pending = [
            s.name
            for s in store.source_states(notebook_id)
            if s.source_id in wanted and s.status in STILL_READING
        ]
        if not pending or clock() >= deadline:
            return pending
        sleep(POLL_S)


def reading_lines(
    store: Store,
    ingest: Ingest,
    bearer: str,
    notebook_id: str,
    selected: Sequence[str] | None,
    url_refresh: Sequence[str],
) -> list[dict[str, Any]]:
    """The estimate's separate lines: reading each selected source not read
    yet, and checking each page in ``url_refresh`` again (one web page each,
    nothing when it has not changed), at ingest's price, in euro cents."""
    uploads = selected_uploads(store, notebook_id, selected)
    pages = check_refresh(uploads, url_refresh)
    unread = [s for s in uploads if s.status == NOT_READ]
    if not unread and not pages:
        return []
    names = {s.library_id: s.name for s in uploads}
    items = ingest.estimate_sources(
        bearer, [s.library_id for s in unread], [p.library_id for p in pages]
    )
    refreshed = {p.library_id for p in pages}
    return [
        {
            "library_id": item["library_id"],
            "label": (
                f"Re-read {names.get(item['library_id'], '')} if it changed"
                if item["library_id"] in refreshed
                else f"Read {names.get(item['library_id'], '')}"
            ),
            "basis": item.get("basis", ""),
            "min": float(item["charge_cents_min"]),
            "max": float(item["charge_cents_max"]),
        }
        for item in items
    ]


def still_reading_note(names: Sequence[str]) -> str:
    return (
        "Still being read: "
        + ", ".join(names)
        + ". This answer uses the sources that are ready; ask again in a moment for the rest."
    )
