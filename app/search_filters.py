"""Combined search filters for ``GET /search``.

A single full-text keyword is rarely enough to locate a batch of mail, so the
search endpoint accepts composable filters on top of the keyword: sender or
recipient address, date range, attachment presence and parse status.

This module parses/validates the raw query-string values exactly once, so the
in-memory and PostgreSQL repositories share identical semantics:

* addresses are compared in normalized form (``strip().lower()``) against the
  mailboxes parsed out of the headers — display names stay untouched in the
  stored detail;
* date bounds are inclusive; a date-only upper bound means end-of-day UTC and
  naive values are read as UTC. Messages **without** a Date never match a
  ranged filter — no date is ever fabricated for them;
* ``describe()`` echoes the normalized filters so a paged response explains
  exactly which conditions produced it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

VALID_STATUSES = ("ok", "defective", "failed")

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_address(value: str) -> str:
    """Canonical comparison form for a mailbox address parsed from a header."""
    return value.strip().lower()


def _parse_instant(raw: str, *, upper: bool) -> datetime:
    """Parse an ISO 8601 bound; date-only upper bounds include the whole day."""
    text = raw.strip()
    try:
        if _DATE_ONLY_RE.match(text):
            day = date.fromisoformat(text)
            edge = time.max if upper else time.min
            return datetime.combine(day, edge, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"invalid datetime {raw!r}: use ISO 8601, e.g. '2026-09-29' or "
            "'2026-09-29T06:00:00+00:00'"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class SearchFilters:
    """Validated, normalized search criteria (``None`` = filter not applied)."""

    q: str | None = None
    from_addr: str | None = None  # normalized sender mailbox
    to_addr: str | None = None  # normalized recipient mailbox (To/Cc/Bcc)
    date_from: datetime | None = None  # inclusive, tz-aware
    date_to: datetime | None = None  # inclusive, tz-aware
    has_attachment: bool | None = None
    status: str | None = None  # ok | defective | failed

    def describe(self) -> dict[str, Any]:
        """Explainable echo of the active filters, in normalized form."""
        out: dict[str, Any] = {}
        if self.q is not None:
            out["q"] = self.q
        if self.from_addr is not None:
            out["from"] = self.from_addr
        if self.to_addr is not None:
            out["to"] = self.to_addr
        if self.date_from is not None:
            out["date_from"] = self.date_from.isoformat()
        if self.date_to is not None:
            out["date_to"] = self.date_to.isoformat()
        if self.has_attachment is not None:
            out["has_attachment"] = self.has_attachment
        if self.status is not None:
            out["status"] = self.status
        return out


def parse_search_filters(
    *,
    q: str | None = None,
    from_addr: str | None = None,
    to_addr: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_attachment: bool | None = None,
    status: str | None = None,
) -> SearchFilters:
    """Validate and normalize raw query values. Raises ``ValueError`` on bad input."""
    if q is not None:
        q = q.strip()
        if not q:
            raise ValueError("q must not be blank")
    if from_addr is not None:
        from_addr = normalize_address(from_addr)
        if not from_addr:
            raise ValueError("from must not be blank")
    if to_addr is not None:
        to_addr = normalize_address(to_addr)
        if not to_addr:
            raise ValueError("to must not be blank")
    if status is not None:
        status = status.strip().lower()
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {', '.join(VALID_STATUSES)}")
    dt_from = _parse_instant(date_from, upper=False) if date_from is not None else None
    dt_to = _parse_instant(date_to, upper=True) if date_to is not None else None
    if dt_from is not None and dt_to is not None and dt_from > dt_to:
        raise ValueError("date_from must not be after date_to")

    filters = SearchFilters(
        q=q,
        from_addr=from_addr,
        to_addr=to_addr,
        date_from=dt_from,
        date_to=dt_to,
        has_attachment=has_attachment,
        status=status,
    )
    if not filters.describe():
        raise ValueError(
            "at least one search criterion is required "
            "(q, from, to, date_from, date_to, has_attachment, status)"
        )
    return filters
