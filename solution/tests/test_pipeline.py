import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from solution.cli import DEFAULT_SCHEMAS

from . import helpers as h
from .oracle import run_oracle


@pytest.fixture(scope="module", params=[42, 7])
def gen(request, tmp_path_factory):
    d = tmp_path_factory.mktemp(f"data{request.param}")
    h.generate(d, request.param)
    return d


def test_matches_naive_oracle(gen, tmp_path):
    db = tmp_path / "l.db"
    h.run(db, "ingest", gen / "drop_1")
    h.run(db, "ingest", gen / "drop_2")
    h.run(db, "report", "--out", tmp_path / "rep")
    o = run_oracle([gen / "drop_1", gen / "drop_2"], DEFAULT_SCHEMAS)
    rep = tmp_path / "rep"

    got = [tuple(r.values()) for r in h.rows(rep / "lead_report.csv")]
    want = [tuple(str(x) for x in r) for r in o["leads"]]
    assert got == want

    got_b = [(r["workflow_id"], r["batch_id"], int(r["total_leads"]), int(r["yet_to_reach_out"]),
              int(r["in_progress_telephony_failed"]), int(r["in_progress_dialed"]), int(r["completed"]))
             for r in h.rows(rep / "batch_summary.csv")]
    want_b = [(w, b, v["total"], v["yet_to_reach_out"], v["in_progress_telephony_failed"],
               v["in_progress_dialed"], v["completed"]) for (w, b), v in sorted(o["batch"].items())]
    assert got_b == want_b

    got_d = [(r["workflow_id"], r["date"], int(r["total_calls"]), int(r["connected_calls"]),
              r["connectivity_rate"], int(r["success_leads"])) for r in h.rows(rep / "daily_overview.csv")]
    want_d = [(w, d, v[0], v[1], f"{v[1]/v[0]:.4f}", len(v[2])) for (w, d), v in sorted(o["daily"].items())]
    assert got_d == want_d

    got_r = [(r["workflow_id"], r["batch_id"], int(r["row_number"]), r["reason"])
             for r in h.rows(rep / "rejections.csv")]
    assert got_r == o["rej"]

    dq = json.loads((rep / "data_quality.json").read_text())
    assert dq["lead_rows_total"] == o["rows_total"]
    assert dq["lead_rows_rejected"] == len(o["rej"])
    assert dq["duplicate_lead_rows_merged"] == o["merged"]
    assert dq["field_warnings"] == o["warns"]
    for k in ("events_total", "malformed_events", "duplicate_events_dropped", "orphan_events",
              "unknown_disposition_events"):
        assert dq[k] == o["dq"][k], k


def test_idempotent_and_incremental_equals_bulk(gen, tmp_path):
    # drop by drop (with re-runs and an intermediate report) ...
    db1 = tmp_path / "a.db"
    h.run(db1, "ingest", gen / "drop_1")
    h.run(db1, "report", "--out", tmp_path / "mid")
    h.run(db1, "ingest", gen / "drop_1")            # re-run: no-op
    h.run(db1, "ingest", gen / "drop_2")
    h.run(db1, "ingest", gen / "drop_2")            # re-run: no-op
    h.run(db1, "report", "--out", tmp_path / "a")
    # ... must equal everything at once, in a single drop folder
    allin = tmp_path / "all" / "drop_all"
    for sub in ("drop_1", "drop_2"):
        shutil.copytree(gen / sub, allin, dirs_exist_ok=True)
    db2 = tmp_path / "b.db"
    h.run(db2, "ingest", tmp_path / "all")
    h.run(db2, "report", "--out", tmp_path / "b")
    assert h.read_reports(tmp_path / "a") == h.read_reports(tmp_path / "b")
    # and ingesting the parent folder (all drops) after the fact changes nothing
    h.run(db1, "ingest", gen)
    h.run(db1, "report", "--out", tmp_path / "a2")
    assert h.read_reports(tmp_path / "a") == h.read_reports(tmp_path / "a2")


def test_no_plaintext_pii_in_database(gen, tmp_path):
    db = tmp_path / "pii.db"
    h.run(db, "ingest", gen / "drop_1")
    h.run(db, "ingest", gen / "drop_2")
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("pii.db*") if p.suffix != ".key")
    n = 0
    for csv_path in gen.glob("drop_*/leads/*/*.csv"):
        for r in h.rows(csv_path):
            for k, v in r.items():
                if k and k.lower() in ("customer_name", "name", "insured name") and v.strip():
                    assert v.strip().encode() not in blob
                    n += 1
    assert n > 100
    for ev in gen.glob("drop_*/events/*.jsonl"):
        for line in ev.read_text().splitlines()[:300]:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            digits = "".join(c for c in e.get("phone", "") if c.isdigit())[-10:]
            if len(digits) == 10:
                assert digits.encode() not in blob


def test_ledger_is_append_only(gen, tmp_path):
    db = tmp_path / "ao.db"
    h.run(db, "ingest", gen / "drop_1")
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET event_type='x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events")
