"""End-to-end tests for the water usage service."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("WATER_DATA_DIR", tmp)
    monkeypatch.setenv("WATER_DB_PATH", os.path.join(tmp, "test.db"))
    # Import after env vars are set; rebuild app with isolated DB.
    import importlib
    import app.config as config
    import app.database as database
    import app.main as main
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def _files(c, kind, content, name="x.csv"):
    return c.post(
        f"/api/uploads/{kind}",
        files={"file": (name, content.encode("utf-8"), "text/csv")},
    )


REPL = (
    "account,old_meter,new_meter,switched_at\n"
    "A1,OLD1,NEW1,2026-01-04T12:00:00Z\n"
    "A2,OLD2,NEW2,2026-01-04T00:00:00Z\n"
)

READINGS = (
    "account,meter,read_at,reading_cum\n"
    "A1,OLD1,2026-01-01T00:00:00Z,100\n"
    "A1,OLD1,2026-01-02T00:00:00Z,110\n"
    "A1,OLD1,2026-01-03T00:00:00Z,125.5\n"
    "A1,NEW1,2026-01-05T00:00:00Z,0\n"
    "A1,NEW1,2026-01-06T00:00:00Z,7\n"
    "A1,NEW1,2026-01-07T00:00:00Z,6\n"          # reversed
    "A2,OLD2,2026-01-01T08:00:00Z,200\n"
    "A2,OLD2,2026-01-02T08:00:00Z,208\n"
    "A2,NEW2,2026-01-04T12:00:00Z,0\n"
    "A2,NEW2,2026-01-05T12:00:00Z,15\n"
)


def _by_key(rows):
    return {(r["account"], r["meter"], r["interval_end"]): r for r in rows}


def _gen(client):
    resp = client.post("/api/generations")
    assert resp.status_code == 200, resp.text
    return resp.json()["detail"]["generation_id"]


def _rows(client, gid):
    return client.get(f"/api/generations/{gid}/rows?limit=10000").json()["rows"]


def test_health_and_empty_state(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/api/generations").json()["generations"] == []
    assert client.get("/api/generations/latest/download").status_code == 404


def test_full_flow_pairing_day_attribution_and_review(client):
    assert _files(client, "replacements", REPL).status_code == 200
    assert _files(client, "readings", READINGS).status_code == 200
    gid = _gen(client)
    rows = _rows(client, gid)
    k = _by_key(rows)

    # Normal pairs: difference attributed to the UTC day of the later reading.
    r = k[("A1", "OLD1", "2026-01-02T00:00:00Z")]
    assert r["status"] == "OK" and r["consumption_m3"] == 10.0
    assert r["attributed_day"] == "2026-01-02"
    assert r["source_reading_ids"]  # source ids populated

    r = k[("A1", "OLD1", "2026-01-03T00:00:00Z")]
    assert r["status"] == "OK" and r["consumption_m3"] == 15.5
    assert r["attributed_day"] == "2026-01-03"

    # First reading of each meter is unpaired -> REVIEW.
    assert k[("A1", "OLD1", "2026-01-01T00:00:00Z")]["status"] == "REVIEW"
    assert k[("A1", "OLD1", "2026-01-01T00:00:00Z")]["review_reason"] == "unpaired_reading"
    assert k[("A1", "NEW1", "2026-01-05T00:00:00Z")]["review_reason"] == "unpaired_reading"

    # Reversed reading flagged, no consumption.
    rev = k[("A1", "NEW1", "2026-01-07T00:00:00Z")]
    assert rev["status"] == "REVIEW"
    assert rev["review_reason"] == "reversed_reading"
    assert rev["consumption_m3"] is None

    # The pair before the reversed one is still fine.
    assert k[("A1", "NEW1", "2026-01-06T00:00:00Z")]["consumption_m3"] == 7.0

    # A2: old-meter second pair OK; new meter first unpaired, second OK.
    assert k[("A2", "OLD2", "2026-01-02T08:00:00Z")]["consumption_m3"] == 8.0
    assert k[("A2", "NEW2", "2026-01-05T12:00:00Z")]["consumption_m3"] == 15.0


def test_same_timestamp_same_value_counts_once(client):
    _files(client, "replacements", REPL)
    base = (
        "account,meter,read_at,reading_cum\n"
        "A1,OLD1,2026-01-01T00:00:00Z,100\n"
        "A1,OLD1,2026-01-02T00:00:00Z,110\n"
    )
    r1 = _files(client, "readings", base)
    assert r1.json()["detail"]["inserted"] == 2
    # Re-upload identical content: all duplicates, zero new rows.
    r2 = _files(client, "readings", base)
    body = r2.json()["detail"]
    assert body["inserted"] == 0 and body["duplicates_skipped"] == 2

    gid = _gen(client)
    rows = _rows(client, gid)
    assert len([r for r in rows if r["meter"] == "OLD1"]) == 2  # no double counting


def test_same_timestamp_different_value_whole_batch_rejected(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n")
    # Conflicting value against committed data -> 422, nothing inserted.
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "A1,OLD1,2026-01-01T00:00:00Z,999\n"
                  "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    assert resp.status_code == 422
    batches = client.get("/api/batches").json()["batches"]
    rejected = [b for b in batches if b["status"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["reject_reason"]
    # Exactly one audit row per rejection attempt (no duplicate on rollback).
    assert sum(1 for b in batches if b["batch_type"] == "reading"
               and b["status"] == "rejected") == 1
    # The second row of the rejected batch must not exist.
    gid = _gen(client)
    rows = _rows(client, gid)
    assert all(r["interval_end"] != "2026-01-02T00:00:00Z" for r in rows)


def test_conflict_inside_same_batch_rejected(client):
    _files(client, "replacements", REPL)
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "A1,OLD1,2026-01-01T00:00:00Z,100\n"
                  "A1,OLD1,2026-01-01T00:00:00Z,101\n")
    assert resp.status_code == 422
    batches = client.get("/api/batches").json()["batches"]
    assert all(b["status"] != "committed" for b in batches if b["batch_type"] == "reading")


def test_crossing_switchover_pair_flagged(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A3,O3,N3,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A3,O3,2026-01-09T00:00:00Z,10\n"
           "A3,O3,2026-01-11T00:00:00Z,30\n"          # after switch on old meter
           "A3,N3,2026-01-09T00:00:00Z,0\n"           # before switch on new meter
           "A3,N3,2026-01-11T00:00:00Z,5\n")
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    assert k[("A3", "O3", "2026-01-11T00:00:00Z")]["review_reason"] == "outside_meter_service_window"
    assert k[("A3", "N3", "2026-01-09T00:00:00Z")]["review_reason"] == "outside_meter_service_window"
    # The surviving in-window readings are unpaired, never cross-subtracted.
    assert k[("A3", "O3", "2026-01-09T00:00:00Z")]["review_reason"] == "unpaired_reading"
    assert k[("A3", "N3", "2026-01-11T00:00:00Z")]["review_reason"] == "unpaired_reading"


def test_interval_exactly_spanning_switch_is_flagged(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A4,O4,N4,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A4,O4,2026-01-09T00:00:00Z,10\n"
           "A4,O4,2026-01-10T00:00:00Z,12\n"          # exactly at switch: in window
           "A4,O4,2026-01-10T12:00:00Z,40\n")         # outside window -> pair blocked
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    # Pair ending exactly at the switch timestamp is allowed (does not cross).
    assert k[("A4", "O4", "2026-01-10T00:00:00Z")]["consumption_m3"] == 2.0
    assert k[("A4", "O4", "2026-01-10T12:00:00Z")]["review_reason"] == "outside_meter_service_window"


def test_late_reading_expires_previous_generation(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)
    gens = client.get("/api/generations").json()["generations"]
    assert gens[0]["id"] == g1 and gens[0]["status"] == "active"

    # Late reading arrives -> previous generation must be expired.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2025-12-31T00:00:00Z,90\n")
    gens = client.get("/api/generations").json()["generations"]
    assert next(g for g in gens if g["id"] == g1)["status"] == "expired"

    g2 = _gen(client)
    rows = _rows(client, g2)
    k = _by_key(rows)
    # New earliest reading becomes the unpaired one; old first reading now pairs.
    assert k[("A1", "OLD1", "2025-12-31T00:00:00Z")]["review_reason"] == "unpaired_reading"
    assert k[("A1", "OLD1", "2026-01-01T00:00:00Z")]["consumption_m3"] == 10.0
    assert k[("A1", "OLD1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0
    # Old generation remains downloadable.
    assert client.get(f"/api/generations/{g1}/download").status_code == 200


def test_replacement_correction_expires_and_changes_window(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A5,O5,N5,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A5,O5,2026-01-08T00:00:00Z,10\n"
           "A5,O5,2026-01-09T00:00:00Z,13\n")
    g1 = _gen(client)
    assert _rows(client, g1)[1]["consumption_m3"] == 3.0

    # Correction: switch actually happened earlier; Jan 9 reading now out of window.
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A5,O5,N5,2026-01-08T12:00:00Z\n")
    gens = client.get("/api/generations").json()["generations"]
    assert next(g for g in gens if g["id"] == g1)["status"] == "expired"
    g2 = _gen(client)
    k = _by_key(_rows(client, g2))
    assert k[("A5", "O5", "2026-01-09T00:00:00Z")]["review_reason"] == "outside_meter_service_window"


def test_generation_without_readings_fails_but_keeps_old_result(client):
    _files(client, "replacements", REPL)
    # No readings at all -> business failure.
    resp = client.post("/api/generations")
    assert resp.status_code == 409

    # Now create a valid active generation.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)
    assert client.get("/api/generations").json()["generations"][0]["status"] == "active"

    # Simulate a failing generation (no committed readings reachable): force error
    # by breaking compute via monkeypatched loader on the app module.
    import app.main as main
    original = main.generate_generation

    def boom(conn):
        raise main.GenerationError("模拟生成失败")

    main.generate_generation = boom
    resp = client.post("/api/generations")
    main.generate_generation = original
    assert resp.status_code == 409

    gens = client.get("/api/generations").json()["generations"]
    old = next(g for g in gens if g["id"] == g1)
    assert old["status"] == "expired"      # marked expired, still downloadable
    assert "失败" in old["status_detail"]
    dl = client.get(f"/api/generations/{g1}/download")
    assert dl.status_code == 200 and "text/csv" in dl.headers["content-type"]
    assert dl.headers["X-Generation-Status"] == "expired"


def test_download_csv_contents(client):
    _files(client, "replacements", REPL)
    _files(client, "readings", READINGS)
    gid = _gen(client)
    dl = client.get(f"/api/generations/{gid}/download")
    text = dl.text
    header = text.splitlines()[0]
    assert "source_reading_ids" in header
    assert "attributed_utc_day" in header
    assert "review_reason" in header
    # Every result line references the generation and has an OK/REVIEW status.
    for line in text.splitlines()[1:]:
        assert line.split(",")[0] == str(gid)
    assert "REVIEW" in text and "reversed_reading" in text


def test_bad_csv_headers_and_rows_rejected(client):
    resp = _files(client, "readings", "a,b,c\n1,2,3\n")
    assert resp.status_code == 422 and "表头" in resp.json()["detail"]
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\nA1,OLD1,not-a-time,10\n")
    assert resp.status_code == 422 and "时刻" in resp.json()["detail"]
    resp = _files(client, "replacements",
                  "account,old_meter,new_meter,switched_at\n"
                  "A1,X,X,2026-01-01T00:00:00Z\n")
    assert resp.status_code == 422


def test_rejected_replacement_keeps_previous_snapshot(client):
    good = (
        "account,old_meter,new_meter,switched_at\n"
        "A5,O5,N5,2026-01-10T00:00:00Z\n")
    assert _files(client, "replacements", good).status_code == 200
    # Invalid sheet (duplicate account) must be rejected without touching data.
    bad = (
        "account,old_meter,new_meter,switched_at\n"
        "A5,X1,Y1,2026-01-01T00:00:00Z\n"
        "A5,X2,Y2,2026-01-02T00:00:00Z\n")
    assert _files(client, "replacements", bad).status_code == 422
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A5,O5,2026-01-08T00:00:00Z,10\n"
           "A5,O5,2026-01-09T00:00:00Z,13\n")
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    # Original switch (Jan 10) still in force: pair is valid.
    assert k[("A5", "O5", "2026-01-09T00:00:00Z")]["consumption_m3"] == 3.0


def test_duplicate_account_in_replacement_sheet_rejected(client):
    resp = _files(client, "replacements",
                  "account,old_meter,new_meter,switched_at\n"
                  "A1,O1,N1,2026-01-01T00:00:00Z\n"
                  "A1,O2,N2,2026-01-02T00:00:00Z\n")
    assert resp.status_code == 422 and "每户一条" in resp.json()["detail"]


class _Row(dict):
    """Minimal sqlite3.Row stand-in supporting key access."""
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_cross_switch_branch_direct_unit():
    """Unit tests for the per-meter pairing rules around the switch instant."""
    from app.compute import _process_meter_group

    def group(*spec):
        return [_Row(id=i, account="A7", meter="O7", read_at=ts, reading_cum=val)
                for i, (ts, val) in enumerate(spec, start=1)]

    def repl(switch):
        return {"old_meter": "O7", "new_meter": "N7", "switched_at": switch}

    # 1) Switch strictly inside an old-meter pair: the later reading is past
    #    the window, so the window guard blocks the pair (production path).
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10), ("2026-01-12T00:00:00Z", 30)),
        repl("2026-01-10T12:00:00Z"), "O7")
    assert [r.review_reason for r in out] == [
        "unpaired_reading", "outside_meter_service_window"]

    # 2) Pair ending exactly at the switch boundary is allowed (no strict cross).
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10), ("2026-01-10T12:00:00Z", 30)),
        repl("2026-01-10T12:00:00Z"), "O7")
    assert out[0].review_reason == "unpaired_reading"
    assert out[1].status == "OK" and out[1].consumption_m3 == 20.0

    # 3) The cross-switch predicate itself is a strict open-interval test.
    t1, switch, t2 = ("2026-01-10T00:00:00Z",
                      "2026-01-10T12:00:00Z",
                      "2026-01-11T00:00:00Z")
    assert t1 < switch < t2           # would trigger crosses_switchover
    assert not (t1 < t2 < switch)     # readings both before switch: no cross

    # 4) Window boundary followed by an out-of-window reading resets the chain.
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10),
              ("2026-01-10T00:00:00Z", 12),
              ("2026-01-10T00:00:01Z", 30)),
        repl("2026-01-10T00:00:00Z"), "O7")
    assert out[0].review_reason == "unpaired_reading"
    assert out[1].status == "OK" and out[1].consumption_m3 == 2.0
    assert out[2].review_reason == "outside_meter_service_window"

    # 5) Unknown meter for the account: every row held, no replacement record.
    out = _process_meter_group(
        group(("2026-01-09T00:00:00Z", 10)),
        repl("2026-01-10T00:00:00Z"), "MYSTERY")
    assert out[0].review_reason == "no_replacement_record"


def test_zero_consumption_equal_readings_are_ok(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A8,O8,N8,2026-02-01T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A8,O8,2026-01-01T00:00:00Z,50\n"
           "A8,O8,2026-01-02T00:00:00Z,50\n"     # equal -> zero usage, still OK
           "A8,O8,2026-01-03T00:00:00Z,51\n")
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    assert k[("A8", "O8", "2026-01-02T00:00:00Z")]["consumption_m3"] == 0.0
    assert k[("A8", "O8", "2026-01-02T00:00:00Z")]["status"] == "OK"
    assert k[("A8", "O8", "2026-01-03T00:00:00Z")]["consumption_m3"] == 1.0


def test_naive_timestamp_treated_as_utc(client):
    _files(client, "replacements", REPL)
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "A1,OLD1,2026-01-01 00:00:00,100\n"
                  "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    assert resp.status_code == 200
    gid = _gen(client)
    k = _by_key(_rows(client, gid))
    assert k[("A1", "OLD1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0


def test_meter_without_replacement_record_is_review(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A9,UNKNOWN,2026-01-01T00:00:00Z,5\n"
           "A9,UNKNOWN,2026-01-02T00:00:00Z,9\n")
    gid = _gen(client)
    rows = [r for r in _rows(client, gid) if r["account"] == "A9"]
    assert all(r["status"] == "REVIEW" for r in rows)
    assert all(r["review_reason"] == "no_replacement_record" for r in rows)


# ---------------------------------------------------------------------------
# Source ledger, withdrawal and interleaving (the source-traceability feature)
# ---------------------------------------------------------------------------

REPL_W = (
    "account,old_meter,new_meter,switched_at\n"
    "W1,WM1,WN1,2026-01-10T00:00:00Z\n"
)


def _batch_ids(client):
    return [b["id"] for b in client.get("/api/batches").json()["batches"]]


def _reading_batch_id(client, index=0):
    batches = [b for b in client.get("/api/batches").json()["batches"]
               if b["batch_type"] == "reading" and b["status"] == "committed"]
    return batches[index]["id"]


def _status_of(client, gid):
    return next(g for g in client.get("/api/generations").json()["generations"]
                if g["id"] == gid)["status"]


def test_same_value_retransmission_keeps_per_row_source(client):
    _files(client, "replacements", REPL_W)
    csv1 = (
        "account,meter,read_at,reading_cum\n"
        "W1,WM1,2026-01-01T00:00:00Z,100\n"
        "W1,WM1,2026-01-02T00:00:00Z,110\n"
    )
    # Same content again, plus an in-batch duplicate row as well.
    r1 = _files(client, "readings", csv1)
    assert r1.json()["detail"]["inserted"] == 2
    # Same content again in a second batch (no in-batch duplicates here);
    # per-row retransmission ledger is tested separately below.
    r2 = _files(client, "readings", csv1)
    detail = r2.json()["detail"]
    # No new effective readings, but every CSV row is recorded as a source.
    assert detail["inserted"] == 0
    assert detail["sources_recorded"] == 2
    assert detail["duplicates_skipped"] == 2
    assert detail["effective_changed"] is False

    # In-batch same-value duplicate rows are also kept as separate sources.
    r3 = _files(client, "readings",
                "account,meter,read_at,reading_cum\n"
                "W1,WM1,2026-01-01T00:00:00Z,100\n"
                "W1,WM1,2026-01-01T00:00:00Z,100\n")
    assert r3.json()["detail"]["sources_recorded"] == 2

    gid = _gen(client)
    assert len([r for r in _rows(client, gid) if r["meter"] == "WM1"]) == 2

    b2 = r2.json()["detail"]["batch_id"]
    sources = client.get(f"/api/batches/{b2}/sources").json()["sources"]
    assert len(sources) == 2                      # one ledger row per CSV row
    assert all(s["active"] for s in sources)
    # After batch 1 and the identical batch 2, each distinct reading has two
    # attesting sources (one per batch).
    counts = {}
    for bid in (r1.json()["detail"]["batch_id"], b2):
        body = client.get(f"/api/batches/{bid}/sources").json()
        for s in body.get("sources", []):
            counts[s["reading_id"]] = counts.get(s["reading_id"], 0) + 1
    assert set(counts.values()) == {2}
    # In-batch duplicates land as two source rows on the same reading id.
    b3_sources = client.get(
        f"/api/batches/{r3.json()['detail']['batch_id']}/sources").json()["sources"]
    assert len(b3_sources) == 2
    assert len({s["reading_id"] for s in b3_sources}) == 1


def test_withdraw_shared_batch_keeps_other_sources_active(client):
    _files(client, "replacements", REPL_W)
    csv1 = (
        "account,meter,read_at,reading_cum\n"
        "W1,WM1,2026-01-01T00:00:00Z,100\n"
        "W1,WM1,2026-01-02T00:00:00Z,110\n"
    )
    csv2 = (
        "account,meter,read_at,reading_cum\n"
        "W1,WM1,2026-01-02T00:00:00Z,110\n"
        "W1,WM1,2026-01-03T00:00:00Z,125\n"
    )
    _files(client, "readings", csv1)
    _files(client, "readings", csv2)
    gid = _gen(client)
    assert _status_of(client, gid) == "active"

    batches = client.get("/api/batches").json()["batches"]
    reading_batches = [b for b in batches if b["batch_type"] == "reading"]
    b1, b2 = reading_batches[1]["id"], reading_batches[0]["id"]  # b2 is newest

    # Withdrawing b2: Jan-2 reading survives via b1; only Jan-3 loses its last source.
    resp = client.post(f"/api/batches/{b2}/withdraw")
    detail = resp.json()["detail"]
    assert resp.status_code == 200
    assert detail["readings_deactivated"] == 1
    assert detail["sources_withdrawn"] == 2

    b2_after = next(b for b in client.get("/api/batches").json()["batches"]
                    if b["id"] == b2)
    assert b2_after["status"] == "withdrawn" and b2_after["withdrawn_at"]
    assert b2_after["active_source_count"] == 0

    # Active generation must be expired because an effective reading changed.
    assert _status_of(client, gid) == "expired"
    # Old generation stays fully downloadable and traceable.
    old_dl = client.get(f"/api/generations/{gid}/download")
    assert old_dl.status_code == 200
    assert old_dl.headers["X-Generation-Status"] == "expired"

    g2 = _gen(client)
    k = _by_key(_rows(client, g2))
    # Jan-2 pair still computed from b1's surviving reading; Jan-3 row is gone.
    assert k[("W1", "WM1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0
    assert all(r["interval_end"] != "2026-01-03T00:00:00Z"
               for r in _rows(client, g2))
    # The new generation's batch manifest no longer lists the withdrawn batch.
    gen2 = next(g for g in client.get("/api/generations").json()["generations"]
                if g["id"] == g2)
    assert str(b2) not in gen2["reading_batch_ids"].split(",")
    assert str(b1) in gen2["reading_batch_ids"].split(",")

    # Withdraw b1 as well: its readings now lose their last sources.
    resp = client.post(f"/api/batches/{b1}/withdraw")
    assert resp.json()["detail"]["readings_deactivated"] == 2


def test_withdraw_last_source_drops_reading_but_keeps_history(client):
    _files(client, "replacements", REPL_W)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "W1,WM1,2026-01-01T00:00:00Z,100\n"
           "W1,WM1,2026-01-02T00:00:00Z,110\n")
    gid = _gen(client)
    bid = _reading_batch_id(client)
    assert client.post(f"/api/batches/{bid}/withdraw").status_code == 200

    # No effective readings remain: regeneration fails and nothing new is published.
    resp = client.post("/api/generations")
    assert resp.status_code == 409
    gens = client.get("/api/generations").json()["generations"]
    assert all(g["id"] != gid or g["status"] == "expired" for g in gens)
    assert not any(g["status"] == "active" for g in gens)

    # Raw readings and source rows remain in the database for traceability.
    from app.database import get_db_path
    import sqlite3
    db = sqlite3.connect(str(get_db_path()))
    db.row_factory = sqlite3.Row
    assert db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM reading_sources").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM readings WHERE active=1").fetchone()[0] == 0
    db.close()


def test_reupload_after_withdrawal_reactivates_and_changes_generation(client):
    _files(client, "replacements", REPL_W)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "W1,WM1,2026-01-01T00:00:00Z,100\n"
           "W1,WM1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)
    bid = _reading_batch_id(client)
    client.post(f"/api/batches/{bid}/withdraw")
    assert _status_of(client, g1) == "expired"

    # Re-upload the same readings: they re-enter the effective set.
    resp = _files(client, "readings",
                  "account,meter,read_at,reading_cum\n"
                  "W1,WM1,2026-01-01T00:00:00Z,100\n"
                  "W1,WM1,2026-01-02T00:00:00Z,110\n")
    detail = resp.json()["detail"]
    assert detail["reactivated"] == 2 and detail["inserted"] == 0
    g2 = _gen(client)
    k = _by_key(_rows(client, g2))
    assert k[("W1", "WM1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0

    # A different value for the same key is still rejected outright.
    conflict = _files(client, "readings",
                      "account,meter,read_at,reading_cum\n"
                      "W1,WM1,2026-01-02T00:00:00Z,999\n")
    assert conflict.status_code == 422


def test_no_effective_change_keeps_generation_active(client):
    _files(client, "replacements", REPL_W)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "W1,WM1,2026-01-01T00:00:00Z,100\n"
           "W1,WM1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)

    # Same-value retransmission changes nothing effective -> stays active.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "W1,WM1,2026-01-01T00:00:00Z,100\n")
    assert _status_of(client, g1) == "active"

    # Withdraw the retransmission-only batch: other source remains -> stays active.
    newest = _reading_batch_id(client, 0)
    resp = client.post(f"/api/batches/{newest}/withdraw")
    assert resp.json()["detail"]["effective_changed"] is False
    assert _status_of(client, g1) == "active"


def test_withdraw_guard_cases(client):
    _files(client, "replacements", REPL_W)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "W1,WM1,2026-01-01T00:00:00Z,100\n")
    bid = _reading_batch_id(client)
    # Unknown batch.
    assert client.post("/api/batches/9999/withdraw").status_code == 404
    # Replacement batch cannot be withdrawn.
    repl_id = next(b["id"] for b in client.get("/api/batches").json()["batches"]
                   if b["batch_type"] == "replacement")
    assert client.post(f"/api/batches/{repl_id}/withdraw").status_code == 409
    # Double withdrawal is rejected.
    assert client.post(f"/api/batches/{bid}/withdraw").status_code == 200
    again = client.post(f"/api/batches/{bid}/withdraw")
    assert again.status_code == 409 and "撤回" in again.json()["detail"]


def test_legacy_batch_marked_not_withdrawable_but_still_supplies_data(client):
    """A database built by the previous schema must keep serving data, with
    its batches explicitly marked non-withdrawable."""
    import importlib
    import sqlite3

    import app.config as config
    from app.database import get_db_path

    db_path = str(get_db_path())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Wipe the freshly initialized schema and hand-craft the *legacy* schema
    # (no withdrawable column, no reading_sources, no readings.active).
    conn.executescript(
        """
        DROP TABLE IF EXISTS result_rows;
        DROP TABLE IF EXISTS generations;
        DROP TABLE IF EXISTS reading_sources;
        DROP TABLE IF EXISTS readings;
        DROP TABLE IF EXISTS replacements;
        DROP TABLE IF EXISTS upload_batches;
        CREATE TABLE upload_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_type TEXT NOT NULL, filename TEXT NOT NULL,
            status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
            reject_reason TEXT, created_at TEXT NOT NULL);
        INSERT INTO upload_batches
            (batch_type, filename, status, row_count, reject_reason, created_at)
        VALUES ('reading','legacy.csv','committed',2,NULL,'2026-01-01T00:00:00Z'),
               ('replacement','legacy-repl.csv','committed',1,NULL,'2026-01-01T00:00:00Z');
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
            account TEXT NOT NULL, meter TEXT NOT NULL, read_at TEXT NOT NULL,
            reading_cum REAL NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(account, meter, read_at));
        INSERT INTO readings
            (batch_id, account, meter, read_at, reading_cum, created_at)
        VALUES (1,'W1','WM1','2026-01-01T00:00:00Z',100,'2026-01-01T00:00:00Z'),
               (1,'W1','WM1','2026-01-02T00:00:00Z',110,'2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    # Re-run schema initialization against the legacy database: migration path.
    import app.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.init_db(db_path)

    # Reload the app so its connection points at the migrated database.
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        batches = c.get("/api/batches").json()["batches"]
        legacy = next(b for b in batches if b["filename"] == "legacy.csv")
        assert legacy["withdrawable"] == 0
        # Withdrawal is refused with an explicit reason.
        resp = c.post(f"/api/batches/{legacy['id']}/withdraw")
        assert resp.status_code == 409 and "不可撤回" in resp.json()["detail"]
        # Sources endpoint explains the legacy situation.
        src = c.get(f"/api/batches/{legacy['id']}/sources").json()
        assert src["withdrawable"] is False and src["sources"] == []

        # Legacy readings still feed generation normally.
        c.post(
            "/api/uploads/replacements",
            files={"file": ("r.csv", REPL_W.encode(), "text/csv")},
        )
        gid = c.post("/api/generations").json()["detail"]["generation_id"]
        k = _by_key(c.get(f"/api/generations/{gid}/rows?limit=1000").json()["rows"])
        assert k[("W1", "WM1", "2026-01-02T00:00:00Z")]["consumption_m3"] == 10.0
        # New uploads after migration get full per-row sources and are withdrawable.
        up = c.post(
            "/api/uploads/readings",
            files={"file": ("n.csv",
                            ("account,meter,read_at,reading_cum\n"
                             "W1,WM1,2026-01-03T00:00:00Z,120\n").encode(),
                            "text/csv")},
        )
        assert up.status_code == 200
        new_bid = up.json()["detail"]["batch_id"]
        assert c.post(f"/api/batches/{new_bid}/withdraw").status_code == 200


def test_concurrent_withdrawals_uploads_and_generations_never_split(client):
    """Stress test: mixed operations must always leave at most one active
    generation, and any active generation must match one committed snapshot."""
    import threading

    _files(client, "replacements", REPL_W)
    base = (
        "account,meter,read_at,reading_cum\n"
        "W1,WM1,2026-01-01T00:00:00Z,100\n"
        "W1,WM1,2026-01-02T00:00:00Z,110\n"
    )
    r = _files(client, "readings", base)
    first_batch = r.json()["detail"]["batch_id"]
    _gen(client)

    errors: list[Exception] = []

    def upload_worker(idx):
        try:
            payload = (
                "account,meter,read_at,reading_cum\n"
                f"W1,WM1,2026-01-0{3 + (idx % 6)}T00:00:00Z,{120 + idx}\n"
            )
            resp = _files(client, "readings", payload, name=f"u{idx}.csv")
            assert resp.status_code in (200, 422)
        except Exception as exc:  # pragma: no cover - failure capture
            errors.append(exc)

    def withdraw_worker():
        try:
            for b in client.get("/api/batches").json()["batches"]:
                if (b["batch_type"] == "reading" and b["status"] == "committed"
                        and b["withdrawable"] and b["id"] != first_batch):
                    client.post(f"/api/batches/{b['id']}/withdraw")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def generate_worker():
        try:
            resp = client.post("/api/generations")
            assert resp.status_code in (200, 409)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = []
    for i in range(6):
        threads.append(threading.Thread(target=upload_worker, args=(i,)))
    for _ in range(3):
        threads.append(threading.Thread(target=withdraw_worker))
    for _ in range(4):
        threads.append(threading.Thread(target=generate_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # Invariant 1: at most one active generation ever exists.
    gens = client.get("/api/generations").json()["generations"]
    active = [g for g in gens if g["status"] == "active"]
    assert len(active) <= 1

    # Invariant 2: every generation's manifest references real reading batches
    # that were committed (a withdrawn batch may still appear in an older
    # generation's snapshot, but a rejected one must never appear anywhere).
    by_id = {b["id"]: b for b in client.get("/api/batches").json()["batches"]}
    for g in gens:
        ids = {int(x) for x in g["reading_batch_ids"].split(",") if x}
        for bid in ids:
            assert bid in by_id
            assert by_id[bid]["status"] in ("committed", "withdrawn")
            assert by_id[bid]["batch_type"] == "reading"

    # Invariant 3: the final active generation reproduces directly from the
    # current committed snapshot (deterministic recomputation).
    if active:
        gid = active[0]["id"]
        rows_now = client.get(f"/api/generations/{gid}/rows?limit=10000").json()["rows"]
        re_gen = client.post("/api/generations")
        assert re_gen.status_code == 200
        new_id = re_gen.json()["detail"]["generation_id"]
        rows_again = client.get(f"/api/generations/{new_id}/rows?limit=10000").json()["rows"]
        sig = lambda rs: sorted(
            (r["account"], r["meter"], r["interval_end"], r["status"],
             r["review_reason"], r["consumption_m3"]) for r in rs)
        assert sig(rows_now) == sig(rows_again)

    # The first batch is still withdrawable at the end and its withdrawal is
    # atomic (batch status, sources and reading flags move together).
    resp = client.post(f"/api/batches/{first_batch}/withdraw")
    assert resp.status_code in (200, 409)
