"""Combined search filters: sender/recipient, date range, attachments,
parse status and keyword — all composable, with totals and an explainable
filter echo. Memory backend here; PostgreSQL parity lives in
test_pg_integration.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

from conftest import SAMPLES

from app.repository import SearchFilters, normalize_address


def _post(client, name, data, **params):
    return client.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params=params,
    )


def _eml(
    *,
    mid: str,
    sender: str = "a@example.com",
    to: str | None = None,
    subject: str = "hello",
    date: str | None = "Mon, 29 Sep 2026 06:00:00 +0000",
    body: str = "plain body text",
    attachment: bool = False,
    bogus_charset: bool = False,
) -> bytes:
    """Build a small EML with exactly the features a test needs."""
    headers = [f"Message-ID: <{mid}>", f"From: {sender}", f"Subject: {subject}"]
    if to is not None:
        headers.append(f"To: {to}")
    if date is not None:
        headers.append(f"Date: {date}")
    if not attachment:
        charset = 'charset="bogus-charset-99"' if bogus_charset else ""
        headers.append(f"Content-Type: text/plain; {charset}".rstrip("; "))
        return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode()
    headers.append('Content-Type: multipart/mixed; boundary="B"')
    parts = ["\r\n".join(headers), "", "--B"]
    text_type = "text/plain"
    if bogus_charset:
        text_type += '; charset="bogus-charset-99"'
    parts += [f"Content-Type: {text_type}", "", body, "--B"]
    parts += [
        "Content-Type: application/octet-stream",
        "Content-Disposition: attachment; filename=data.bin",
        "",
        "ATTACHMENT-BYTES",
        "--B--",
        "",
    ]
    return "\r\n".join(parts).encode()


# -- unit-level normalization -----------------------------------------------


def test_normalize_address_strips_display_name_and_case():
    assert normalize_address("Alice Example <Alice@Example.COM>") == "alice@example.com"
    assert normalize_address("  Bob@Example.org ") == "bob@example.org"
    assert normalize_address("") == ""
    assert normalize_address(None) == ""


def test_search_filters_normalization():
    f = SearchFilters(
        from_address=" Carol <Carol@Example.COM> ",
        date_from=datetime(2026, 9, 1),  # naive -> UTC
        q="   ",
    )
    assert f.from_address == "carol@example.com"
    assert f.date_from.tzinfo is timezone.utc
    assert f.q is None  # blank keyword degrades to "no keyword filter"
    echo = f.applied()
    assert echo["from"] == "carol@example.com"
    assert "q" not in echo


# -- sender / recipient filters ----------------------------------------------


def test_same_subject_only_target_sender_returned(client):
    c, _ = client
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())
    _post(c, "05_same_subject_other.eml", (SAMPLES / "05_same_subject_other.eml").read_bytes())

    # keyword alone hits both "Quarterly report" mails
    both = c.get("/search", params={"q": "Quarterly"}).json()
    assert both["count"] == 2

    # keyword + sender isolates exactly one of them
    res = c.get("/search", params={"q": "Quarterly", "from": "q@example.com"}).json()
    assert res["count"] == 1
    hit = res["results"][0]
    assert hit["subject"] == "Quarterly report"
    assert [a["address"] for a in hit["from_json"]] == ["q@example.com"]
    # explainable echo carries the normalized filter values
    assert res["filters"] == {"q": "Quarterly", "from": "q@example.com"}

    # address matching is case/display-name insensitive
    res2 = c.get("/search", params={"from": "X <X@Example.COM>"}).json()
    assert res2["count"] == 1
    assert [a["address"] for a in res2["results"][0]["from_json"]] == ["x@example.com"]

    # a sender that only appears in the other mail does not leak in
    res3 = c.get("/search", params={"q": "Quarterly", "from": "nobody@example.com"}).json()
    assert res3["count"] == 0


def test_recipient_filter_uses_parsed_to_header(client):
    c, _ = client
    _post(c, "01_multibyte.eml", (SAMPLES / "01_multibyte.eml").read_bytes())
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())

    res = c.get("/search", params={"to": "CN@example.com"}).json()
    assert res["count"] == 1
    assert res["results"][0]["message_id"] == "multi-01@example.com"
    assert res["filters"]["to"] == "cn@example.com"

    assert c.get("/search", params={"to": "absent@example.com"}).json()["count"] == 0


# -- date range ----------------------------------------------------------------


def test_undated_message_never_matches_date_range(client):
    c, _ = client
    r_nodate = _post(c, "07_bad_cte.eml", (SAMPLES / "07_bad_cte.eml").read_bytes())
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())

    # sanity: the undated message exists and its detail shows no fabricated date
    pk = r_nodate.json()["message_pk"]
    assert c.get(f"/messages/{pk}").json()["date"] is None

    window = {"date_from": "2026-09-01T00:00:00Z", "date_to": "2026-10-05T00:00:00Z"}
    res = c.get("/search", params=window).json()
    assert res["count"] == 1
    assert res["results"][0]["subject"] == "Quarterly report"
    assert res["filters"]["date_from"].startswith("2026-09-01")

    # a one-sided bound must not pull the undated message in either
    res2 = c.get("/search", params={"date_from": "2020-01-01T00:00:00Z"}).json()
    assert {m["message_id"] for m in res2["results"]} == {"qr-1@example.com"}
    res3 = c.get("/search", params={"date_to": "2030-01-01T00:00:00Z"}).json()
    assert {m["message_id"] for m in res3["results"]} == {"qr-1@example.com"}

    # ...but without any date filter the undated message is found normally
    res4 = c.get("/search", params={"from": "cte@example.com"}).json()
    assert res4["count"] == 1


def test_date_range_bounds_are_inclusive(client):
    c, _ = client
    _post(
        c,
        "d1.eml",
        _eml(mid="d1@example.com", date="Mon, 29 Sep 2026 06:00:00 +0000"),
    )
    res = c.get(
        "/search",
        params={
            "date_from": "2026-09-29T06:00:00+00:00",
            "date_to": "2026-09-29T06:00:00+00:00",
        },
    ).json()
    assert res["count"] == 1
    # one second earlier/later and it falls out
    assert c.get("/search", params={"date_from": "2026-09-29T06:00:01+00:00"}).json()["count"] == 0
    assert c.get("/search", params={"date_to": "2026-09-29T05:59:59+00:00"}).json()["count"] == 0


# -- attachments x parse status -----------------------------------------------


def test_attachment_and_defective_filters_crosschecked_with_details(client):
    c, _ = client
    defective_with_att = _post(
        c, "def_att.eml", _eml(mid="def-att@example.com", attachment=True, bogus_charset=True)
    ).json()
    assert defective_with_att["status"] == "defective"
    _post(c, "08_traversal.eml", (SAMPLES / "08_traversal.eml").read_bytes())  # ok + attachment
    _post(c, "06_corrupt_boundary.eml", (SAMPLES / "06_corrupt_boundary.eml").read_bytes())  # defective, no att
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())  # ok, no att

    res = c.get("/search", params={"has_attachment": "true", "status": "defective"}).json()
    assert res["count"] == 1
    assert res["filters"] == {"has_attachment": True, "status": "defective"}

    # every hit must hold up against its detail and ingest records
    for hit in res["results"]:
        detail = c.get(f"/messages/{hit['id']}").json()
        assert detail["attachments"], "has_attachment=true but no attachments in detail"
        ingest = c.get(f"/ingests/{detail['ingest_id']}").json()
        assert ingest["status"] == "defective"
        assert ingest["defects"], "status=defective but no defects recorded"

    # the other three quadrants
    ok_att = c.get("/search", params={"has_attachment": "true", "status": "ok"}).json()
    assert [m["message_id"] for m in ok_att["results"]] == ["trav-01@example.com"]
    def_noatt = c.get("/search", params={"has_attachment": "false", "status": "defective"}).json()
    assert [m["message_id"] for m in def_noatt["results"]] == ["corrupt-01@example.com"]
    ok_noatt = c.get("/search", params={"has_attachment": "false", "status": "ok"}).json()
    assert [m["message_id"] for m in ok_noatt["results"]] == ["qr-1@example.com"]


def test_status_failed_matches_no_messages(client):
    c, _ = client
    _post(c, "garbage.bin", b"\x00\xff\xfe not an email " * 50)
    res = c.get("/search", params={"status": "failed"}).json()
    # failed ingests have no message row; the failure is visible via /failures
    assert res["count"] == 0
    assert c.get("/failures").json()


def test_invalid_status_rejected(client):
    c, _ = client
    assert c.get("/search", params={"status": "bogus"}).status_code == 422


# -- pagination / totals / echo -------------------------------------------------


def test_pagination_reports_total_and_stable_pages(client):
    c, _ = client
    for i in range(5):
        _post(
            c,
            f"p{i}.eml",
            _eml(mid=f"p{i}@example.com", subject="batch item", body=f"commonkeyword {i}"),
        )
    page1 = c.get("/search", params={"q": "commonkeyword", "limit": 2, "offset": 0}).json()
    assert page1["count"] == 5  # total hits, not the page size
    assert page1["limit"] == 2 and page1["offset"] == 0
    assert len(page1["results"]) == 2
    page2 = c.get("/search", params={"q": "commonkeyword", "limit": 2, "offset": 2}).json()
    page3 = c.get("/search", params={"q": "commonkeyword", "limit": 2, "offset": 4}).json()
    ids = [m["id"] for p in (page1, page2, page3) for m in p["results"]]
    assert len(ids) == 5 and len(set(ids)) == 5  # no overlap, no loss


def test_no_filters_lists_everything_and_echoes_empty(client):
    c, _ = client
    _post(c, "05_same_subject_root.eml", (SAMPLES / "05_same_subject_root.eml").read_bytes())
    _post(c, "08_traversal.eml", (SAMPLES / "08_traversal.eml").read_bytes())
    res = c.get("/search").json()
    assert res["count"] == 2
    assert res["filters"] == {}
    assert res["query"] is None


def test_keyword_is_literal_not_like_pattern(client):
    c, _ = client
    _post(c, "pct.eml", _eml(mid="pct@example.com", subject="Coverage 100%_done"))
    _post(c, "other.eml", _eml(mid="other@example.com", subject="Coverage 1000 done"))
    # `%` and `_` must stay literal substring characters, not LIKE wildcards
    res = c.get("/search", params={"q": "100%_"}).json()
    assert res["count"] == 1
    assert res["results"][0]["message_id"] == "pct@example.com"
