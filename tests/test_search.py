"""Combined-filter search scenarios.

The ``scenario_*`` functions take a plain TestClient and are executed against
both backends: here with the in-memory repository, and from
``test_pg_integration.py`` with the PostgreSQL repository, so identical
semantics are enforced by construction.
"""
from __future__ import annotations

from conftest import SAMPLES

# Defective (unknown charset on the text part) AND carrying an attachment.
DEFECTIVE_WITH_ATTACHMENT = b"""Message-ID: <def-att@example.com>
From: defatt@example.com
To: Archive Desk <archive@example.com>
Subject: defective with attachment
Date: Wed, 30 Sep 2026 09:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="bogus-charset-99"

payload with unknown charset
--BOUND
Content-Type: application/octet-stream; name=data.bin
Content-Disposition: attachment; filename=data.bin

BINBYTES
--BOUND--
"""

# No Date header at all: must never be matched by a date range.
NO_DATE = b"""Message-ID: <nodate@example.com>
From: nodate@example.com
Subject: undated quarterly report
Content-Type: text/plain

no date header at all
"""


def _post(c, name, data, **params):
    return c.post("/ingest", files={"file": (name, data, "message/rfc822")}, params=params)


def _batch_eml(i: int) -> bytes:
    return (
        f"Message-ID: <batch-{i}@example.com>\n"
        f"From: batch@example.com\n"
        f"Subject: batch-report {i}\n"
        f"Date: Thu, 01 Oct 2026 0{i}:00:00 +0000\n"
        f"Content-Type: text/plain\n\n"
        f"batch body {i}\n"
    ).encode()


# -- scenarios (run against memory here, against PostgreSQL in test_pg_integration) --

def scenario_from_disambiguates_same_subject(c):
    """Same subject, different senders: the from filter returns only the target."""
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())
    _post(c, "05_same_subject_other.eml", (SAMPLES / "05_same_subject_other.eml").read_bytes())

    both = c.get("/search", params={"q": "Quarterly"}).json()
    assert both["count"] == 2

    r = c.get("/search", params={"q": "Quarterly", "from": "q@example.com"}).json()
    assert r["count"] == 1
    assert r["results"][0]["from_json"][0]["address"] == "q@example.com"
    # the applied conditions are echoed back, normalized
    assert r["filters"] == {"q": "Quarterly", "from": "q@example.com"}

    other = c.get("/search", params={"q": "Quarterly", "from": "x@example.com"}).json()
    assert other["count"] == 1
    assert other["results"][0]["from_json"][0]["address"] == "x@example.com"

    # normalization: case and surrounding whitespace are irrelevant
    noisy = c.get("/search", params={"q": "Quarterly", "from": "  Q@Example.COM "}).json()
    assert noisy["count"] == 1
    assert noisy["filters"]["from"] == "q@example.com"


def scenario_undated_never_matches_date_range(c):
    """A missing Date is never fabricated into a range hit."""
    _post(c, "nodate.eml", NO_DATE)
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())

    # sanity: the undated mail is findable by keyword
    all_hits = c.get("/search", params={"q": "quarterly"}).json()
    assert all_hits["count"] == 2
    # dated first (date DESC, NULLS LAST), undated last
    assert all_hits["results"][0]["subject"] == "Quarterly report"
    assert all_hits["results"][1]["subject"] == "undated quarterly report"

    # a range covering every conceivable date still excludes the undated mail
    ranged = c.get(
        "/search",
        params={"q": "quarterly", "date_from": "2020-01-01", "date_to": "2030-01-01"},
    ).json()
    assert ranged["count"] == 1
    assert ranged["results"][0]["subject"] == "Quarterly report"

    # same for a range-only search
    only_range = c.get(
        "/search", params={"date_from": "2020-01-01", "date_to": "2030-01-01"}
    ).json()
    assert only_range["count"] == 1
    assert all(m["date"] is not None for m in only_range["results"])


def scenario_attachment_defective_crosscheck(c):
    """has_attachment + status results can be verified one-by-one via details."""
    _post(c, "def-att.eml", DEFECTIVE_WITH_ATTACHMENT)  # defective + attachment
    _post(c, "01_multibyte.eml", (SAMPLES / "01_multibyte.eml").read_bytes())  # ok + attachments
    _post(c, "06_corrupt_boundary.eml", (SAMPLES / "06_corrupt_boundary.eml").read_bytes())  # defective, no att
    _post(c, "07_bad_cte.eml", (SAMPLES / "07_bad_cte.eml").read_bytes())  # ok + attachment

    r = c.get("/search", params={"has_attachment": True, "status": "defective"}).json()
    assert r["count"] == 1
    assert r["filters"] == {"has_attachment": True, "status": "defective"}

    # every hit must be verifiable against the detail endpoints
    for hit in r["results"]:
        assert hit["has_attachment"] is True
        assert hit["status"] == "defective"
        detail = c.get(f"/messages/{hit['id']}").json()
        assert detail["attachments"], "detail must list the attachment"
        assert detail["defects"], "detail must list the defects behind 'defective'"
        ingest = c.get(f"/ingests/{detail['ingest_id']}").json()
        assert ingest["status"] == "defective"

    ok_with_att = c.get("/search", params={"has_attachment": True, "status": "ok"}).json()
    assert ok_with_att["count"] == 2
    defective_no_att = c.get(
        "/search", params={"has_attachment": False, "status": "defective"}
    ).json()
    assert defective_no_att["count"] == 1
    assert defective_no_att["results"][0]["subject"] == "Broken multipart boundary"


def scenario_total_count_across_pages(c):
    """count is the total number of hits, independent of limit/offset."""
    for i in range(3):
        _post(c, f"batch{i}.eml", _batch_eml(i))

    page1 = c.get("/search", params={"q": "batch-report", "limit": 2}).json()
    assert page1["count"] == 3
    assert page1["limit"] == 2 and page1["offset"] == 0
    assert len(page1["results"]) == 2

    page2 = c.get("/search", params={"q": "batch-report", "limit": 2, "offset": 2}).json()
    assert page2["count"] == 3
    assert len(page2["results"]) == 1

    ids = [m["id"] for m in page1["results"] + page2["results"]]
    assert len(set(ids)) == 3, "pages must partition the hits without overlap"
    # deterministic order: date DESC
    dates = [m["date"] for m in page1["results"] + page2["results"]]
    assert dates == sorted(dates, reverse=True)


def scenario_like_metacharacters_are_literal(c):
    """% and _ in the keyword are literal characters, not SQL wildcards."""
    _post(c, "u1.eml", b"Message-ID: <u1@example.com>\nFrom: u1@example.com\n"
                       b"Subject: coverage_report final\nContent-Type: text/plain\n\nx\n")
    _post(c, "u2.eml", b"Message-ID: <u2@example.com>\nFrom: u2@example.com\n"
                       b"Subject: coverage report final\nContent-Type: text/plain\n\nx\n")
    _post(c, "p1.eml", b"Message-ID: <p1@example.com>\nFrom: p1@example.com\n"
                       b"Subject: Progress 100% done\nContent-Type: text/plain\n\nx\n")
    _post(c, "p2.eml", b"Message-ID: <p2@example.com>\nFrom: p2@example.com\n"
                       b"Subject: Progress 1000 done\nContent-Type: text/plain\n\nx\n")

    r = c.get("/search", params={"q": "coverage_report"}).json()
    assert r["count"] == 1
    assert r["results"][0]["subject"] == "coverage_report final"

    r2 = c.get("/search", params={"q": "100%"}).json()
    assert r2["count"] == 1
    assert r2["results"][0]["subject"] == "Progress 100% done"


def scenario_to_filter_and_date_bounds(c):
    """Recipient filter hits To/Cc/Bcc; date-only upper bound spans the whole day."""
    _post(c, "def-att.eml", DEFECTIVE_WITH_ATTACHMENT)  # To: archive@example.com, 2026-09-30 09:00

    r = c.get("/search", params={"to": "archive@example.com"}).json()
    assert r["count"] == 1
    assert r["results"][0]["subject"] == "defective with attachment"

    # the sender address is not a recipient
    assert c.get("/search", params={"to": "defatt@example.com"}).json()["count"] == 0
    # ...but is found by the from filter
    assert c.get("/search", params={"from": "defatt@example.com"}).json()["count"] == 1

    # date-only bounds: the whole day is included
    day = c.get("/search", params={"date_from": "2026-09-30", "date_to": "2026-09-30"}).json()
    assert day["count"] == 1
    assert day["filters"]["date_from"].startswith("2026-09-30T00:00:00")
    assert day["filters"]["date_to"].startswith("2026-09-30T23:59:59")
    # the day before does not match
    assert c.get("/search", params={"date_to": "2026-09-29"}).json()["count"] == 0


# -- memory-backend tests ----------------------------------------------------

def test_from_filter_disambiguates_same_subject(client):
    c, _ = client
    scenario_from_disambiguates_same_subject(c)


def test_undated_never_matches_date_range(client):
    c, _ = client
    scenario_undated_never_matches_date_range(c)


def test_attachment_defective_crosscheck(client):
    c, _ = client
    scenario_attachment_defective_crosscheck(c)


def test_total_count_across_pages(client):
    c, _ = client
    scenario_total_count_across_pages(c)


def test_like_metacharacters_are_literal(client):
    c, _ = client
    scenario_like_metacharacters_are_literal(c)


def test_to_filter_and_date_bounds(client):
    c, _ = client
    scenario_to_filter_and_date_bounds(c)


def test_all_filters_combine(client):
    c, _ = client
    _post(c, "def-att.eml", DEFECTIVE_WITH_ATTACHMENT)
    _post(c, "01_multibyte.eml", (SAMPLES / "01_multibyte.eml").read_bytes())
    r = c.get(
        "/search",
        params={
            "q": "attachment",
            "from": "defatt@example.com",
            "to": "archive@example.com",
            "date_from": "2026-09-30T00:00:00+00:00",
            "date_to": "2026-10-01T00:00:00+00:00",
            "has_attachment": "true",
            "status": "defective",
        },
    ).json()
    assert r["count"] == 1
    assert set(r["filters"]) == {
        "q", "from", "to", "date_from", "date_to", "has_attachment", "status",
    }
    hit = r["results"][0]
    assert hit["subject"] == "defective with attachment"
    assert hit["has_attachment"] is True and hit["status"] == "defective"


def test_search_requires_at_least_one_criterion(client):
    c, _ = client
    r = c.get("/search")
    assert r.status_code == 422
    assert "at least one search criterion" in r.json()["detail"]


def test_search_rejects_invalid_values(client):
    c, _ = client
    r = c.get("/search", params={"status": "weird"})
    assert r.status_code == 422
    assert "status" in r.json()["detail"]
    assert c.get("/search", params={"date_from": "not-a-date"}).status_code == 422
    assert c.get(
        "/search", params={"date_from": "2026-10-01", "date_to": "2026-09-01"}
    ).status_code == 422
    assert c.get("/search", params={"q": ""}).status_code == 422
