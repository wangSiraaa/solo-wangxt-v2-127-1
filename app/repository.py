"""Repository interfaces used by the service layer.

Two implementations exist: :mod:`app.pg_repository` (PostgreSQL) and
:mod:`app.memory_repository` (tests / ephemeral runs). The API only depends on
this interface, so business logic is testable without a database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Protocol, Sequence

from app.parser.models import ParsedMessage


def normalize_address(value: str | None) -> str:
    """Reduce an address filter to a bare, lower-cased addr-spec.

    Accepts either a plain address or a ``Display Name <addr>`` form. Stored
    addresses are normalized the same way at comparison time, so matching is
    case- and display-name-insensitive while the stored detail rows keep the
    original display name and raw header text.
    """
    if not value:
        return ""
    _, addr = parseaddr(value)
    return (addr or value).strip().lower()


@dataclass
class SearchFilters:
    """Combined search criteria; every criterion is optional and AND-ed.

    ``__post_init__`` normalizes addresses and datetimes so the memory and
    PostgreSQL repositories compare identical values and stay semantically
    aligned. Messages with no parsed ``Date`` never satisfy a date bound —
    missing dates are not fabricated.
    """

    q: str | None = None  # substring over subject / ids / header values / plain text
    from_address: str | None = None  # exact normalized addr-spec from the From header
    to_address: str | None = None  # exact normalized addr-spec from the To header
    date_from: datetime | None = None  # inclusive lower bound on the parsed Date
    date_to: datetime | None = None  # inclusive upper bound on the parsed Date
    has_attachment: bool | None = None  # message has at least one attachment part
    status: str | None = None  # ingest parse status: ok / defective / failed
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        if self.q is not None and not self.q.strip():
            self.q = None
        if self.from_address is not None:
            self.from_address = normalize_address(self.from_address) or None
        if self.to_address is not None:
            self.to_address = normalize_address(self.to_address) or None
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                setattr(self, name, value.replace(tzinfo=timezone.utc))

    def applied(self) -> dict[str, Any]:
        """Explainable echo of the active criteria (normalized values)."""
        out: dict[str, Any] = {}
        if self.q is not None:
            out["q"] = self.q
        if self.from_address is not None:
            out["from"] = self.from_address
        if self.to_address is not None:
            out["to"] = self.to_address
        if self.date_from is not None:
            out["date_from"] = self.date_from.isoformat()
        if self.date_to is not None:
            out["date_to"] = self.date_to.isoformat()
        if self.has_attachment is not None:
            out["has_attachment"] = self.has_attachment
        if self.status is not None:
            out["status"] = self.status
        return out


class Repository(Protocol):
    def init_schema(self) -> None: ...

    def save_ingest(
        self,
        parsed: ParsedMessage,
        *,
        raw_relpath: str | None,
        stored_attachments: Sequence[tuple[Any, str]],
        source_name: str | None,
        status: str,
        fatal_error: str | None,
    ) -> dict[str, Any]:
        """Persist one EML (headers, parts, attachments metadata, defects).

        Returns ``{"ingest_id": int, "message_id": int | None}``. Failed parses
        get an ingest row with message_id=None.
        """
        ...

    def rebuild_threads(self) -> dict[str, Any]:
        """Recompute all thread assignments from stored headers."""
        ...

    def get_message(self, message_pk: int) -> dict[str, Any] | None: ...
    def get_ingest(self, ingest_id: int) -> dict[str, Any] | None: ...
    def list_messages(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def search_messages(self, filters: SearchFilters) -> dict[str, Any]:
        """Combined search. Returns ``query``, ``filters`` (applied criteria),
        ``count`` (total hits, unpaginated), ``limit``, ``offset`` and the
        current page of summaries in ``results``."""
        ...
    def get_thread(self, thread_key: str) -> dict[str, Any] | None: ...
    def list_threads(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def list_failures(self, limit: int, offset: int) -> list[dict[str, Any]]: ...
    def get_attachment(self, attachment_id: int) -> dict[str, Any] | None: ...
    def get_attachment_by_message(self, message_pk: int, attachment_id: int) -> dict[str, Any] | None: ...
