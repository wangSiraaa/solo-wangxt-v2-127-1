"""End-to-end checks against the real PostgreSQL schema/SQL.

These run only when EMLARCH_RUN_PG_TESTS=1 and the test DSN is reachable.
They re-ingest representative samples and exercise SQL that has no in-memory
equivalent (JSONB aggregates, LATERAL identifier rollups, ILIKE joins).
"""
import pytest

from conftest import SAMPLES

pytestmark = pytest.mark.pg


def _post(c, name, **params):
    data = (SAMPLES / name).read_bytes()
    return c.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params=params,
    )


def test_health_reports_pg(pg_client):
    c, _ = pg_client
    assert c.get("/health").json()["backend"] == "postgresql"


def test_schema_persists_nested_multipart(pg_client):
    c, _ = pg_client
    r = _post(c, "01_multibyte.eml")
    assert r.status_code == 201, r.text
    pk = r.json()["message_pk"]
    msg = c.get(f"/messages/{pk}").json()
    assert msg["message_id"] == "multi-01@example.com"
    assert msg["tree_json"]["content_type"] == "multipart/mixed"
    # all four textual parts (incl. embedded message/rfc822 body)
    ctypes = sorted(b["content_type"] for b in msg["bodies"])
    assert ctypes.count("text/plain") == 3
    assert "text/html" in ctypes
    # binary attachments: inline gif + pdf
    atts = {(a["mime_path"], a["stored"]) for a in msg["attachments"]}
    assert ("1.3.2", True) in atts
    assert ("3", True) in atts
    # provenance: ingest id links raw digest to parsed result
    ing = c.get(f"/ingests/{msg['ingest_id']}").json()
    assert ing["raw_sha256"] == msg["raw_sha256"]


def test_failed_and_defective_rows_queryable(pg_client):
    c, _ = pg_client
    _post(c, "06_corrupt_boundary.eml")
    _post(c, "03_missing_id.eml")
    fails = c.get("/failures").json()
    assert len(fails) == 2
    # defect stages are populated for location
    assert all(d["stage"] for f in fails for d in f["defects"])


def test_threading_sql_roundtrip(pg_client):
    c, _ = pg_client
    for n in ["02_cycle_a.eml", "02_cycle_b.eml", "04_duplicate_id_a.eml",
              "04_duplicate_id_b.eml", "05_same_subject_root.eml",
              "05_same_subject_other.eml", "01_multibyte.eml"]:
        _post(c, n, recompute_threads=False)
    rebuilt = c.post("/threads/rebuild").json()
    assert rebuilt["threads"] >= 4
    assert any({"cycle-a@example.com", "cycle-b@example.com"} <= set(cyc)
               for cyc in rebuilt["cycles"])
    assert "dup-1@example.com" in rebuilt["duplicate_ids"]
    assert any(w["reason"] == "subject_match_only" for w in rebuilt["weak_suggestions"])

    # thread detail via LATERAL identifier rollup
    threads = c.get("/threads").json()
    cycle_thread = next(t for t in threads if t["message_count"] == 2)
    detail = c.get(f"/threads/{cycle_thread['thread_key']}").json()
    assert len(detail["messages"]) == 2
    # multibyte message is in parent-root thread, with refs rollups present
    multi = c.get("/search", params={"q": "multi-01"}).json()["results"][0]
    md = c.get(f"/messages/{multi['id']}").json()
    assert md["thread_key"]


def test_search_sql_joins(pg_client):
    c, _ = pg_client
    _post(c, "01_multibyte.eml")
    _post(c, "09_html_xss.eml")
    assert c.get("/search", params={"q": "GB18030"}).json()["count"] == 1
    assert c.get("/search", params={"q": "café"}).json()["count"] >= 1
    # header value search (From)
    assert c.get("/search", params={"q": "sigs@example.com"}).json()["count"] == 1
    assert c.get("/search", params={"q": "nothing-matches-zzz"}).json()["count"] == 0


def _eml(*, mid, sender="a@example.com", to=None, subject="hello",
         date="Mon, 29 Sep 2026 06:00:00 +0000", body="plain body text",
         attachment=False, bogus_charset=False):
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
    text_type = "text/plain" + ('; charset="bogus-charset-99"' if bogus_charset else "")
    parts = ["\r\n".join(headers), "", "--B",
             f"Content-Type: {text_type}", "", body, "--B",
             "Content-Type: application/octet-stream",
             "Content-Disposition: attachment; filename=data.bin", "",
             "ATTACHMENT-BYTES", "--B--", ""]
    return "\r\n".join(parts).encode()


def _post_bytes(c, name, data, **params):
    return c.post("/ingest", files={"file": (name, data, "message/rfc822")}, params=params)


def test_search_filters_sender_and_status_pg(pg_client):
    """Same-subject senders, has_attachment x status, totals and filter echo."""
    c, _ = pg_client
    _post(c, "05_same_subject_root.eml")
    _post(c, "05_same_subject_other.eml")
    _post(c, "08_traversal.eml")  # ok + attachment
    r = _post_bytes(c, "def_att.eml",
                    _eml(mid="def-att@example.com", attachment=True, bogus_charset=True))
    assert r.json()["status"] == "defective"

    both = c.get("/search", params={"q": "Quarterly"}).json()
    assert both["count"] == 2
    one = c.get("/search", params={"q": "Quarterly", "from": "Q <q@Example.COM>"}).json()
    assert one["count"] == 1
    assert [a["address"] for a in one["results"][0]["from_json"]] == ["q@example.com"]
    assert one["filters"] == {"q": "Quarterly", "from": "q@example.com"}

    res = c.get("/search", params={"has_attachment": "true", "status": "defective"}).json()
    assert res["count"] == 1
    for hit in res["results"]:
        detail = c.get(f"/messages/{hit['id']}").json()
        assert detail["attachments"]
        ing = c.get(f"/ingests/{detail['ingest_id']}").json()
        assert ing["status"] == "defective" and ing["defects"]

    ok_att = c.get("/search", params={"has_attachment": "true", "status": "ok"}).json()
    assert [m["message_id"] for m in ok_att["results"]] == ["trav-01@example.com"]


def test_search_filters_undated_excluded_pg(pg_client):
    c, _ = pg_client
    r_nodate = _post(c, "07_bad_cte.eml")  # no Date header at all
    _post(c, "05_same_subject_root.eml")
    pk = r_nodate.json()["message_pk"]
    assert c.get(f"/messages/{pk}").json()["date"] is None

    window = {"date_from": "2026-09-01T00:00:00Z", "date_to": "2026-10-05T00:00:00Z"}
    res = c.get("/search", params=window).json()
    assert res["count"] == 1
    assert res["results"][0]["message_id"] == "qr-1@example.com"
    one_sided = c.get("/search", params={"date_to": "2030-01-01T00:00:00Z"}).json()
    assert {m["message_id"] for m in one_sided["results"]} == {"qr-1@example.com"}


def test_search_filters_pagination_and_literal_wildcards_pg(pg_client):
    c, _ = pg_client
    for i in range(5):
        _post_bytes(c, f"p{i}.eml",
                    _eml(mid=f"p{i}@example.com", subject="batch item", body=f"commonkeyword {i}"))
    page = c.get("/search", params={"q": "commonkeyword", "limit": 2, "offset": 0}).json()
    assert page["count"] == 5 and len(page["results"]) == 2
    rest = c.get("/search", params={"q": "commonkeyword", "limit": 2, "offset": 4}).json()
    assert len(rest["results"]) == 1

    # LIKE metacharacters in the keyword stay literal (memory-backend parity)
    _post_bytes(c, "pct.eml", _eml(mid="pct@example.com", subject="Coverage 100%_done"))
    _post_bytes(c, "oth.eml", _eml(mid="oth@example.com", subject="Coverage 1000 done"))
    res = c.get("/search", params={"q": "100%_"}).json()
    assert [m["message_id"] for m in res["results"]] == ["pct@example.com"]


def test_idempotent_schema_init(pg_client):
    c, arch = pg_client
    # creating a second repository over the same DSN must not error on DDL
    arch.repo.init_schema()
    assert c.get("/health").status_code == 200
