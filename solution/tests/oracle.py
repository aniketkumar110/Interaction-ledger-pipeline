"""Deliberately naive, plaintext, in-memory re-implementation of the spec, used
only to cross-check the SQLite pipeline. Shares only the leaf parsers."""
import csv
import io
import json
import re
from collections import defaultdict
from pathlib import Path

from solution.normalize import parse_date, parse_decimal, parse_enum, parse_phone, parse_timestamp_us, us_to_ist
from solution.schema import load_dispositions, load_schemas


def natural(p):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def run_oracle(drops, schema_dir):
    schemas = load_schemas(schema_dir)
    disp = {(w, c): (t, s) for w, c, t, s in load_dispositions(schema_dir)}
    leads, rejections, merged, warns, rows_total = {}, [], 0, 0, 0
    events, seen = [], set()
    dq = defaultdict(int)
    for drop in drops:
        files = sorted([(f, wf.name) for wf in (drop / "leads").iterdir() for f in wf.glob("*.csv")],
                       key=lambda t: (natural(t[0]), t[1]))
        for f, wf in files:
            sch = schemas[wf]
            rd = csv.reader(io.StringIO(f.read_text(encoding="utf-8"), newline=""))
            header = next(rd)
            n = 0
            for cells in rd:
                if not any(c.strip() for c in cells):
                    continue
                n += 1
                rows_total += 1
                rec = {h_i: c.strip() for h_i, c in enumerate(cells)}
                vals, reason, w = {}, None, 0
                for fld in sch.fields:
                    raw = ""
                    for i, h in enumerate(header):
                        key = re.sub(r"[_\s]+", " ", h.strip().lower()).strip()
                        names = {re.sub(r"[_\s]+", " ", x.strip().lower()).strip() for x in (fld.name, *fld.aliases)}
                        if key in names:
                            raw = rec.get(i, "")
                            break
                    if not raw:
                        if fld.required:
                            reason = f"missing_required:{fld.name}"
                            break
                        vals[fld.name] = None
                        continue
                    v = {"string": lambda: raw, "phone": lambda: parse_phone(raw), "date": lambda: parse_date(raw),
                         "decimal": lambda: parse_decimal(raw),
                         "enum": lambda: parse_enum(raw, fld.values)}[fld.type]()
                    if v is None:
                        if fld.type == "phone":
                            reason = "invalid_phone"
                            break
                        vals[fld.name] = None
                        w += 1
                    else:
                        vals[fld.name] = v
                if reason:
                    rejections.append((wf, f.stem, n, reason))
                    continue
                warns += w
                key = (wf, vals["phone"])
                if key in leads:
                    merged += 1
                    for k, v in vals.items():
                        if v is not None:
                            leads[key]["v"][k] = v
                else:
                    leads[key] = {"batch": f.stem, "v": dict(vals)}
        for f in sorted((drop / "events").glob("*.jsonl"), key=natural):
            for line in f.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                dq["events_total"] += 1
                try:
                    e = json.loads(line)
                    eid = e["event_id"]
                except Exception:
                    dq["malformed_events"] += 1
                    continue
                if eid in seen:
                    dq["duplicate_events_dropped"] += 1
                    continue
                seen.add(eid)
                e["_ts"] = parse_timestamp_us(e["occurred_at"])
                events.append(e)

    # link
    call_lead = {}
    for e in events:
        if e["event_type"] != "disposition.override":
            k = (e["workflow_id"], parse_phone(e.get("phone")))
            if k in leads:
                call_lead.setdefault(e["call_id"], k)
    per_call = defaultdict(list)
    for e in events:
        if e["event_type"] == "disposition.override":
            k = call_lead.get(e["call_id"])
        else:
            k = (e["workflow_id"], parse_phone(e.get("phone")))
            k = k if k in leads else None
        if k is None:
            dq["orphan_events"] += 1
            continue
        if e["event_type"].startswith("disposition.") and (k[0], e["payload"].get("disposition_code")) not in disp:
            dq["unknown_disposition_events"] += 1
            continue
        per_call[(k, e["call_id"])].append(e)

    def eff(evs, wf):
        for typ in ("disposition.override", "disposition.set"):
            c = [e for e in evs if e["event_type"] == typ]
            if c:
                return max(c, key=lambda e: (e["_ts"], e["event_id"]))["payload"]["disposition_code"]
        return None

    lead_calls = defaultdict(list)
    for (k, cid), evs in per_call.items():
        inits = [e["_ts"] for e in evs if e["event_type"] == "call.initiated"]
        if inits:
            lead_calls[k].append({
                "id": cid, "t": min(inits), "conn": any(e["event_type"] == "call.connected" for e in evs),
                "fail": any(e["event_type"] == "call.telephony_failed" for e in evs), "eff": eff(evs, k[0])})

    out_leads, batch, daily = [], defaultdict(lambda: defaultdict(int)), defaultdict(lambda: [0, 0, set()])
    for k, L in leads.items():
        cs = sorted(lead_calls.get(k, []), key=lambda c: (c["t"], c["id"]))
        T = lambda c: c["eff"] is not None and disp[(k[0], c["eff"])][0]
        S = lambda c: c["eff"] is not None and disp[(k[0], c["eff"])][1]
        if not cs:
            b = "yet_to_reach_out"
        elif any(T(c) for c in cs):
            b = "completed"
        elif all(c["fail"] for c in cs):
            b = "in_progress_telephony_failed"
        else:
            b = "in_progress_dialed"
        withd = [c["eff"] for c in cs if c["eff"] is not None]
        sch = schemas[k[0]]
        ref = L["v"][sch.reference_field]
        out_leads.append((k[0], ref, L["batch"], "+91******" + k[1][-4:], b, len(cs), sum(c["conn"] for c in cs),
                          us_to_ist(cs[-1]["t"]).isoformat(timespec="seconds") if cs else "",
                          withd[-1] if withd else "", "true" if any(S(c) for c in cs) else "false"))
        batch[(k[0], L["batch"])]["total"] += 1
        batch[(k[0], L["batch"])][b] += 1
        for c in cs:
            d = daily[(k[0], us_to_ist(c["t"]).date().isoformat())]
            d[0] += 1
            d[1] += c["conn"]
            if S(c):
                d[2].add(k)
    return {"leads": sorted(out_leads), "batch": batch, "daily": daily, "rej": sorted(rejections),
            "merged": merged, "warns": warns, "rows_total": rows_total, "dq": dq}
