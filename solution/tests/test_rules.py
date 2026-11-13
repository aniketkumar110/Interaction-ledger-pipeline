"""Hand-built scenarios for the rules most likely to break."""
import json

from . import helpers as h

HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
WF = "motor_renewal"


def ev(i, typ, call, ts, phone="9876543210", payload=None, wf=WF):
    e = {"event_id": f"e{i}", "event_type": typ, "workflow_id": wf, "call_id": call,
         "occurred_at": ts, "payload": payload or {}}
    if phone:
        e["phone"] = phone
    return e


def build(tmp_path, leads_csv, events, extra_drops=()):
    root = tmp_path / "data"
    h.write_drop(root, "drop_1", {(WF, "batch_001"): leads_csv}, events)
    for name, lcsv, evs in extra_drops:
        h.write_drop(root, name, lcsv, evs)
    db = tmp_path / "t.db"
    h.run(db, "ingest", root)
    h.run(db, "report", "--out", tmp_path / "rep")
    return h.rows(tmp_path / "rep" / "lead_report.csv"), tmp_path / "rep"


LEAD = HDR + "Asha Rao,9876543210,P1,,,\n"


def test_override_beats_later_set_and_original_kept(tmp_path):
    evs = [ev(1, "call.initiated", "c1", "2026-10-01T10:00:00Z"),
           ev(2, "call.connected", "c1", "2026-10-01T10:00:05Z"),
           ev(3, "disposition.override", "c1", "2026-10-01T11:00:00Z", phone=None,
              payload={"disposition_code": "renewed"}),
           ev(4, "disposition.set", "c1", "2026-10-01T12:00:00Z", payload={"disposition_code": "not_interested"})]
    (lead,), _ = build(tmp_path, LEAD, evs)
    assert lead["current_disposition"] == "renewed" and lead["ever_success"] == "true"
    assert lead["bucket"] == "completed"
    import sqlite3  # the original disposition.set is still in the ledger, untouched
    n = sqlite3.connect(tmp_path / "t.db").execute(
        "SELECT COUNT(*) FROM events WHERE event_type='disposition.set'").fetchone()[0]
    assert n == 1


def test_latest_override_and_latest_set_win(tmp_path):
    evs = [ev(1, "call.initiated", "c1", "2026-10-01T10:00:00Z"),
           ev(2, "disposition.set", "c1", "2026-10-01T10:10:00Z", payload={"disposition_code": "callback_requested"}),
           ev(3, "disposition.set", "c1", "2026-10-01T10:20:00Z", payload={"disposition_code": "payment_link_sent"}),
           ev(4, "disposition.override", "c1", "2026-10-02T10:00:00Z", phone=None,
              payload={"disposition_code": "renewed"}),
           ev(5, "disposition.override", "c1", "2026-10-03T10:00:00Z", phone=None,
              payload={"disposition_code": "callback_requested"})]
    (lead,), _ = build(tmp_path, LEAD, evs)
    assert lead["current_disposition"] == "callback_requested"
    assert lead["ever_success"] == "false" and lead["bucket"] == "in_progress_dialed"


def test_buckets(tmp_path):
    leads = HDR + "".join(f"N{i},98765432{i:02d},P{i},,,\n" for i in range(1, 5))
    evs = [
        # lead 2: only telephony failures -> in_progress_telephony_failed
        ev(1, "call.initiated", "a", "2026-10-01T10:00:00Z", "9876543202"),
        ev(2, "call.telephony_failed", "a", "2026-10-01T10:00:02Z", "9876543202"),
        ev(3, "call.initiated", "b", "2026-10-02T10:00:00Z", "9876543202"),
        ev(4, "call.telephony_failed", "b", "2026-10-02T10:00:02Z", "9876543202"),
        # lead 3: one failed, one connected non-terminal -> in_progress_dialed
        ev(5, "call.initiated", "c", "2026-10-01T10:00:00Z", "9876543203"),
        ev(6, "call.telephony_failed", "c", "2026-10-01T10:00:02Z", "9876543203"),
        ev(7, "call.initiated", "d", "2026-10-02T10:00:00Z", "9876543203"),
        ev(8, "call.connected", "d", "2026-10-02T10:00:09Z", "9876543203"),
        ev(9, "disposition.set", "d", "2026-10-02T10:05:00Z", "9876543203", {"disposition_code": "language_barrier"}),
        # lead 4: terminal after a failed call -> completed (terminal beats failed)
        ev(10, "call.initiated", "e", "2026-10-01T10:00:00Z", "9876543204"),
        ev(11, "call.telephony_failed", "e", "2026-10-01T10:00:02Z", "9876543204"),
        ev(12, "call.initiated", "f", "2026-10-02T10:00:00Z", "9876543204"),
        ev(13, "disposition.set", "f", "2026-10-02T10:05:00Z", "9876543204", {"disposition_code": "dnd"}),
    ]
    got, _ = build(tmp_path, leads, evs)
    assert {r["external_ref"]: r["bucket"] for r in got} == {
        "P1": "yet_to_reach_out", "P2": "in_progress_telephony_failed",
        "P3": "in_progress_dialed", "P4": "completed"}


def test_call_without_initiated_is_not_counted(tmp_path):
    evs = [ev(1, "call.connected", "x", "2026-10-01T10:00:05Z"),
           ev(2, "disposition.set", "x", "2026-10-01T10:05:00Z", payload={"disposition_code": "renewed"})]
    (lead,), _ = build(tmp_path, LEAD, evs)
    assert lead["total_calls"] == "0" and lead["bucket"] == "yet_to_reach_out" and lead["ever_success"] == "false"


def test_orphan_resolves_when_lead_arrives_in_later_drop(tmp_path):
    evs = [ev(1, "call.initiated", "c1", "2026-10-01T10:00:00Z", "9000000001"),
           ev(2, "call.connected", "c1", "2026-10-01T10:00:05Z", "9000000001"),
           ev(3, "disposition.override", "c1", "2026-10-01T11:00:00Z", None, {"disposition_code": "renewed"})]
    root = tmp_path / "data"
    h.write_drop(root, "drop_1", {(WF, "batch_001"): LEAD}, evs)
    db = tmp_path / "t.db"
    h.run(db, "ingest", root / "drop_1")
    h.run(db, "report", "--out", tmp_path / "r1")
    assert json.loads((tmp_path / "r1/data_quality.json").read_text())["orphan_events"] == 3
    h.write_drop(root, "drop_2", {(WF, "batch_002"): HDR + "Zed,9000000001,P9,,,\n"}, [])
    h.run(db, "ingest", root / "drop_2")
    h.run(db, "report", "--out", tmp_path / "r2")
    dq = json.loads((tmp_path / "r2/data_quality.json").read_text())
    assert dq["orphan_events"] == 0
    lead = [r for r in h.rows(tmp_path / "r2/lead_report.csv") if r["external_ref"] == "P9"][0]
    assert lead["total_calls"] == "1" and lead["connected_calls"] == "1" and lead["current_disposition"] == "renewed"


def test_event_filtering_counters(tmp_path):
    good = ev(1, "call.initiated", "c1", "2026-10-01T10:00:00Z")
    lines = [good, json.dumps(good), '{"event_type": "call.initiated"}', '{"event_id": "bad', "",
             ev(2, "disposition.set", "c1", "2026-10-01T10:10:00Z", payload={"disposition_code": "RENEWD"}),
             ev(3, "disposition.set", "c1", "2026-10-01T10:10:00Z", phone="123",
                payload={"disposition_code": "renewed"})]
    _, rep = build(tmp_path, LEAD, [lines[0]] + lines[1:])
    dq = json.loads((rep / "data_quality.json").read_text())
    assert dq["events_total"] == 6          # blank line not counted
    assert dq["malformed_events"] == 2
    assert dq["duplicate_events_dropped"] == 1
    assert dq["unknown_disposition_events"] == 1
    assert dq["orphan_events"] == 1


def test_naive_timestamp_is_ist_and_dates_are_ist(tmp_path):
    evs = [ev(1, "call.initiated", "c1", "2026-10-01T20:00:00Z"),      # = 02 Oct 01:30 IST
           ev(2, "call.initiated", "c2", "2026-10-02 23:30:00")]        # naive: IST, still 02 Oct
    (lead,), rep = build(tmp_path, LEAD, evs)
    assert lead["last_call_at"] == "2026-10-02T23:30:00+05:30"
    assert [r["date"] for r in h.rows(rep / "daily_overview.csv")] == ["2026-10-02"]
    assert h.rows(rep / "daily_overview.csv")[0]["total_calls"] == "2"


def test_lead_ingestion_rules(tmp_path):
    csv_text = ("Insured Name, Mobile_No ,Policy No,EXPIRY,Premium,language,extra\r\n"
                "Asha,98765 43210,P1,31/02/2026,\"Rs. 1,000\",HI,x\r\n"        # bad optional date -> warning
                ",,,,,,\r\n"                                                 # blank: skipped, not counted
                "Asha2,+91 98765 43210,P1b,01-Dec-2026,\"₹2,000.5\",,y\r\n"     # duplicate: overwrites non-null
                "  ,9876543211,P2,,,,\r\n"                                   # row 3: missing name
                "Bob,12345,P3,,,,\r\n"                                       # row 4: invalid phone
                ",12345,P4,,,,\r\n")                                         # row 5: name checked first
    root = tmp_path / "data"
    h.write_drop(root, "drop_1", {(WF, "batch_001"): csv_text}, [])
    db = tmp_path / "t.db"
    h.run(db, "ingest", root)
    h.run(db, "report", "--out", tmp_path / "rep")
    rej = h.rows(tmp_path / "rep/rejections.csv")
    assert [(r["row_number"], r["reason"]) for r in rej] == [
        ("3", "missing_required:customer_name"), ("4", "invalid_phone"), ("5", "missing_required:customer_name")]
    dq = json.loads((tmp_path / "rep/data_quality.json").read_text())
    assert dq["lead_rows_total"] == 5 and dq["lead_rows_rejected"] == 3
    assert dq["duplicate_lead_rows_merged"] == 1
    assert dq["field_warnings"] == 1      # only the non-rejected row with the bad date
    (lead,) = h.rows(tmp_path / "rep/lead_report.csv")
    assert lead["external_ref"] == "P1b" and lead["batch_id"] == "batch_001"   # last write wins
    # merged values were persisted correctly (decrypt via the vault)
    import sqlite3
    from solution import pii
    vault = pii.PiiVault(pii.load_key(db))
    attrs, blob = sqlite3.connect(db).execute("SELECT attrs, pii_enc FROM leads").fetchone()
    assert json.loads(attrs) == {"policy_expiry": "2026-12-01", "premium_amount": "2000.50", "preferred_language": "hi"}
    assert vault.decrypt_json(blob) == {"customer_name": "Asha2", "phone": "+919876543210"}


def test_batch_id_is_first_valid_row_forever(tmp_path):
    root = tmp_path / "data"
    h.write_drop(root, "drop_1", {(WF, "batch_001"): LEAD}, [])
    h.write_drop(root, "drop_2", {(WF, "batch_002"): HDR + "Asha,9876543210,P1-new,,,en\n"}, [])
    db = tmp_path / "t.db"
    h.run(db, "ingest", root)
    h.run(db, "report", "--out", tmp_path / "rep")
    (lead,) = h.rows(tmp_path / "rep/lead_report.csv")
    assert lead["batch_id"] == "batch_001" and lead["external_ref"] == "P1-new"
    (b,) = h.rows(tmp_path / "rep/batch_summary.csv")
    assert b["batch_id"] == "batch_001" and b["total_leads"] == "1"


def test_default_language_applied_only_when_null(tmp_path):
    root = tmp_path / "data"
    h.write_drop(root, "drop_1", {(WF, "batch_001"): LEAD}, [])   # language blank -> default en
    db = tmp_path / "t.db"
    h.run(db, "ingest", root)
    import sqlite3
    attrs = json.loads(sqlite3.connect(db).execute("SELECT attrs FROM leads").fetchone()[0])
    assert attrs["preferred_language"] == "en"
