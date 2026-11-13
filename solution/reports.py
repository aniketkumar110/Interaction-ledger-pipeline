"""Report generation. Everything is derived from the ledger + leads tables at
call time, so reports always reflect everything ingested so far."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from .normalize import IST, us_to_ist

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")


class _Call:
    __slots__ = ("init_ts", "connected", "failed", "best_set", "best_ovr")

    def __init__(self):
        self.init_ts = None
        self.connected = False
        self.failed = False
        self.best_set = None   # (ts, event_id, code)
        self.best_ovr = None

    def effective(self):
        """Latest override wins outright; otherwise latest set; ties broken by event_id."""
        best = self.best_ovr or self.best_set
        return best[2] if best else None


def compute(conn) -> dict:
    leads = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, workflow_id, external_ref, batch_id, phone_last4 FROM leads")}
    disp = {(w, c): (t, s) for w, c, t, s in conn.execute(
        "SELECT workflow_id, code, is_terminal, is_success FROM dispositions")}

    # call_id -> lead, taken from the call's own phone-bearing events (overrides have no phone)
    call_lead = dict(conn.execute(
        "SELECT e.call_id, MIN(l.id) FROM events e JOIN leads l "
        "ON l.workflow_id=e.workflow_id AND l.phone_hash=e.phone_hash "
        "WHERE e.event_type != 'disposition.override' AND e.call_id IS NOT NULL GROUP BY e.call_id"))

    calls: dict[tuple, _Call] = {}
    orphans = unknown = 0
    cur = conn.execute(
        "SELECT e.event_id, e.event_type, e.call_id, e.occurred_at_us, e.disposition_code, l.id "
        "FROM events e LEFT JOIN leads l ON l.workflow_id=e.workflow_id AND l.phone_hash=e.phone_hash")
    for eid, etype, call_id, ts, code, own_lead in cur:
        if etype == "disposition.override":
            lead = call_lead.get(call_id) if call_id is not None else None
            if lead is None:
                lead = own_lead
        else:
            lead = own_lead
        if lead is None:
            orphans += 1
            continue
        if etype.startswith("disposition."):
            if (leads[lead][0], code) not in disp:
                unknown += 1
                continue
        if call_id is None:
            continue
        key = (lead, call_id)
        c = calls.get(key)
        if c is None:
            c = calls[key] = _Call()
        if etype == "call.initiated":
            if c.init_ts is None or ts < c.init_ts:
                c.init_ts = ts
        elif etype == "call.connected":
            c.connected = True
        elif etype == "call.telephony_failed":
            c.failed = True
        elif etype == "disposition.set":
            cand = (ts, eid, code)
            if c.best_set is None or cand > c.best_set:
                c.best_set = cand
        elif etype == "disposition.override":
            cand = (ts, eid, code)
            if c.best_ovr is None or cand > c.best_ovr:
                c.best_ovr = cand

    by_lead = defaultdict(list)
    for (lead, call_id), c in calls.items():
        if c.init_ts is not None:                # a call exists only if call.initiated is present
            by_lead[lead].append((c.init_ts, call_id, c))

    lead_rows, daily = [], {}
    batch = defaultdict(lambda: dict.fromkeys(BUCKETS, 0))
    for lid, (wf, ref, batch_id, last4) in leads.items():
        cl = sorted(by_lead.get(lid, []), key=lambda t: (t[0], t[1]))
        wf_disp = lambda code: disp[(wf, code)]
        effs = [(t[2].effective()) for t in cl]
        if not cl:
            bucket = "yet_to_reach_out"
        elif any(e is not None and wf_disp(e)[0] for e in effs):
            bucket = "completed"
        elif all(t[2].failed for t in cl):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        ever_success = any(e is not None and wf_disp(e)[1] for e in effs)
        cur_disp = next((e for e in reversed(effs) if e is not None), "")
        last = us_to_ist(cl[-1][0]).isoformat(timespec="seconds") if cl else ""
        lead_rows.append((wf, ref, batch_id, "+91******" + last4, bucket, len(cl),
                          sum(1 for t in cl if t[2].connected), last, cur_disp,
                          "true" if ever_success else "false"))
        batch[(wf, batch_id)]["total"] = batch[(wf, batch_id)].get("total", 0) + 1
        batch[(wf, batch_id)][bucket] += 1
        for (ts, _cid, c), e in zip(cl, effs):
            d = daily.setdefault((wf, us_to_ist(ts).date().isoformat()), [0, 0, set()])
            d[0] += 1
            d[1] += c.connected
            if e is not None and wf_disp(e)[1]:
                d[2].add(lid)

    return {"leads": lead_rows, "batch": batch, "daily": daily, "orphans": orphans, "unknown": unknown}


def write_reports(conn, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    r = compute(conn)

    def writer(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)

    writer("batch_summary.csv",
           ["workflow_id", "batch_id", "total_leads", *BUCKETS],
           [(wf, b, v["total"], *(v[k] for k in BUCKETS)) for (wf, b), v in sorted(r["batch"].items())])

    writer("daily_overview.csv",
           ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
           [(wf, d, v[0], v[1], f"{v[1] / v[0]:.4f}", len(v[2])) for (wf, d), v in sorted(r["daily"].items())])

    writer("lead_report.csv",
           ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
            "connected_calls", "last_call_at", "current_disposition", "ever_success"],
           sorted(r["leads"], key=lambda x: (x[0], x[1])))

    rej = conn.execute(
        "SELECT workflow_id, batch_id, row_number, reason FROM rejections "
        "ORDER BY workflow_id, batch_id, row_number").fetchall()
    writer("rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"], rej)

    s = conn.execute("SELECT COALESCE(SUM(rows_total),0), COALESCE(SUM(rows_rejected),0), "
                     "COALESCE(SUM(rows_merged),0), COALESCE(SUM(field_warnings),0) "
                     "FROM ingested_files WHERE kind='leads'").fetchone()
    e = conn.execute("SELECT COALESCE(SUM(rows_total),0), COALESCE(SUM(malformed),0), "
                     "COALESCE(SUM(duplicates),0) FROM ingested_files WHERE kind='events'").fetchone()
    by_reason = dict(conn.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason ORDER BY reason"))
    dq = {
        "lead_rows_total": s[0], "lead_rows_rejected": s[1], "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": s[2], "field_warnings": s[3],
        "events_total": e[0], "malformed_events": e[1], "duplicate_events_dropped": e[2],
        "orphan_events": r["orphans"], "unknown_disposition_events": r["unknown"],
    }
    (out / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
    return dq
