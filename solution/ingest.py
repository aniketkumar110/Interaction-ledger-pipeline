"""Ingestion of lead CSVs and event JSONL files.

Each file is ingested in ONE transaction that also writes its ``ingested_files``
row, so a crash leaves either everything or nothing from that file, and a
re-run skips files that are already recorded.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .normalize import (parse_date, parse_decimal, parse_enum, parse_phone,
                        parse_timestamp_us)
from .pii import PiiVault
from .schema import Field, WorkflowSchema


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


# ------------------------------------------------------------------ drops
def find_drops(path: Path) -> list[Path]:
    """`path` is either a drop (has leads/ or events/) or a folder of drops."""
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    return sorted((p for p in path.iterdir() if p.is_dir()), key=natural_key)


def ingest_drop(conn, vault: PiiVault, schemas: dict[str, WorkflowSchema], drop: Path) -> dict:
    stats = {"files_ingested": 0, "files_skipped": 0}
    lead_files = []
    leads_dir = drop / "leads"
    if leads_dir.is_dir():
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            for f in sorted(wf_dir.glob("*.csv"), key=natural_key):
                lead_files.append((wf_dir.name, f))
    # "files in filename order": order by file name, workflow as tie-break
    lead_files.sort(key=lambda t: (natural_key(t[1]), t[0]))
    for wf, f in lead_files:
        _run_file(conn, stats, drop, f, "leads",
                  lambda data, fid, wf=wf, f=f: _ingest_lead_file(conn, vault, schemas, wf, f.stem, data, fid))
    events_dir = drop / "events"
    if events_dir.is_dir():
        for f in sorted(events_dir.glob("*.jsonl"), key=natural_key):
            _run_file(conn, stats, drop, f, "events",
                      lambda data, fid: _ingest_event_file(conn, vault, data, fid))
    return stats


def _run_file(conn, stats, drop: Path, f: Path, kind: str, handler) -> None:
    rel = str(f.relative_to(drop))
    data = f.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute("SELECT sha256 FROM ingested_files WHERE drop_name=? AND relpath=?",
                       (drop.name, rel)).fetchone()
    if row:
        if row[0] != sha:
            raise SystemExit(f"{drop.name}/{rel} was already ingested with different content; "
                             "refusing to ingest a changed file (the ledger is append-only).")
        stats["files_skipped"] += 1
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT INTO ingested_files (drop_name, relpath, kind, sha256) VALUES (?,?,?,?)",
                           (drop.name, rel, kind, sha))
        counters = handler(data, cur.lastrowid)
        sets = ", ".join(f"{k}=?" for k in counters)
        conn.execute(f"UPDATE ingested_files SET {sets} WHERE id=?", (*counters.values(), cur.lastrowid))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    stats["files_ingested"] += 1


# ------------------------------------------------------------------ leads
def coerce(f: Field, raw: str):
    """-> (ok, value). `raw` is already trimmed and non-empty."""
    if f.type == "string":
        return True, raw
    if f.type == "phone":
        v = parse_phone(raw)
    elif f.type == "date":
        v = parse_date(raw)
    elif f.type == "decimal":
        v = parse_decimal(raw)
    elif f.type == "enum":
        v = parse_enum(raw, f.values)
    else:
        raise ValueError(f"unknown field type {f.type}")
    return v is not None, v


@dataclass
class RowResult:
    values: dict | None = None      # field -> value|None (None => rejected)
    reason: str | None = None
    warnings: int = 0


def validate_row(schema: WorkflowSchema, colmap: dict[str, int], cells: list[str]) -> RowResult:
    values, warnings = {}, 0
    for f in schema.fields:                      # schema order: first failure wins
        idx = colmap.get(f.name)
        raw = cells[idx].strip() if idx is not None and idx < len(cells) else ""
        if raw == "":
            if f.required:
                return RowResult(reason=f"missing_required:{f.name}")
            values[f.name] = None
            continue
        ok, v = coerce(f, raw)
        if ok:
            values[f.name] = v
        elif f.type == "phone":
            return RowResult(reason="invalid_phone")
        elif f.required:                         # not covered by the spec; see SUBMISSION.md
            return RowResult(reason=f"invalid_{f.type}:{f.name}")
        else:
            values[f.name] = None
            warnings += 1
    return RowResult(values=values, warnings=warnings)


def _ingest_lead_file(conn, vault, schemas, wf: str, batch_id: str, data: bytes, file_id: int) -> dict:
    schema = schemas.get(wf)
    if schema is None:
        raise SystemExit(f"No lead schema for workflow directory '{wf}'")
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    colmap: dict[str, int] = {}
    for i, h in enumerate(header or []):
        fname = schema.header_lookup.get(re.sub(r"[_\s]+", " ", h.strip().lower()).strip())
        if fname is not None:
            colmap.setdefault(fname, i)          # leftmost matching column wins

    n_rows = n_rej = n_merged = n_warn = 0
    cache: dict[bytes, dict] = {}                # phone_hash -> lead state (merged in memory)
    dirty: set[bytes] = set()
    rejections = []
    by_name = {f.name: f for f in schema.fields}

    for cells in reader:
        if all(c.strip() == "" for c in cells):  # blank row: skipped, not counted
            continue
        n_rows += 1
        res = validate_row(schema, colmap, cells)
        if res.reason:
            n_rej += 1
            rejections.append((file_id, wf, batch_id, n_rows, res.reason))
            continue
        n_warn += res.warnings
        phone = res.values[schema.phone_field]
        ph = vault.phone_hash(phone)
        lead = cache.get(ph)
        if lead is None:
            lead = _load_lead(conn, vault, schema, ph)
            if lead is not None:
                cache[ph] = lead
        if lead is None:                         # first valid row: creates the lead
            lead = {"new": True, "batch_id": batch_id, "values": {}}
            cache[ph] = lead
        else:
            n_merged += 1
        for k, v in res.values.items():          # last write wins, nulls never overwrite
            if v is not None:
                lead["values"][k] = v
        dirty.add(ph)

    for ph in dirty:
        lead = cache[ph]
        vals = lead["values"]
        for f in schema.fields:                  # defaults only fill fields still null
            if vals.get(f.name) is None and f.default is not None:
                vals[f.name] = f.default
        pii = {k: v for k, v in vals.items() if by_name[k].pii or k == schema.phone_field}
        attrs = {k: v for k, v in vals.items()
                 if k not in pii and k != schema.reference_field and v is not None}
        blob = vault.encrypt_json(pii)
        attrs_json = json.dumps(attrs, sort_keys=True)
        ref = vals[schema.reference_field]
        if lead.get("new"):
            conn.execute(
                "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref, attrs, pii_enc)"
                " VALUES (?,?,?,?,?,?,?)",
                (wf, ph, vals[schema.phone_field][-4:], lead["batch_id"], ref, attrs_json, blob))
        else:
            conn.execute("UPDATE leads SET external_ref=?, attrs=?, pii_enc=? WHERE id=?",
                         (ref, attrs_json, blob, lead["id"]))
    conn.executemany("INSERT INTO rejections VALUES (?,?,?,?,?)", rejections)
    return {"rows_total": n_rows, "rows_rejected": n_rej, "rows_merged": n_merged, "field_warnings": n_warn}


def _load_lead(conn, vault, schema, ph) -> dict | None:
    row = conn.execute("SELECT id, batch_id, external_ref, attrs, pii_enc FROM leads "
                       "WHERE workflow_id=? AND phone_hash=?", (schema.workflow_id, ph)).fetchone()
    if row is None:
        return None
    lid, batch, ref, attrs, blob = row
    return {"id": lid, "batch_id": batch,
            "values": {**json.loads(attrs), **vault.decrypt_json(blob), schema.reference_field: ref}}


# ------------------------------------------------------------------ events
_INSERT_EVENT = ("INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, occurred_at_us,"
                 " phone_hash, disposition_code, payload, file_id) VALUES (?,?,?,?,?,?,?,?,?)")


def _ingest_event_file(conn, vault, data: bytes, file_id: int) -> dict:
    total = malformed = dups = 0
    batch = []

    def flush():
        nonlocal dups
        if not batch:
            return
        before = conn.total_changes
        conn.executemany(_INSERT_EVENT, batch)
        dups += len(batch) - (conn.total_changes - before)   # OR IGNORE swallowed the repeats
        batch.clear()

    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        total += 1
        ev = _parse_event(vault, line)
        if ev is None:
            malformed += 1
            continue
        batch.append((*ev, file_id))
        if len(batch) >= 20000:
            flush()
    flush()
    return {"rows_total": total, "malformed": malformed, "duplicates": dups}


def _parse_event(vault, line: bytes):
    try:
        o = json.loads(line.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    eid = o.get("event_id")
    if isinstance(eid, bool) or not isinstance(eid, (str, int)) or str(eid).strip() == "":
        return None
    ts = parse_timestamp_us(o.get("occurred_at"))
    if ts is None:                       # unusable timestamp == unusable event (see SUBMISSION.md)
        return None
    etype = o.get("event_type")
    etype = etype if isinstance(etype, str) else ""
    wf = o.get("workflow_id") if isinstance(o.get("workflow_id"), str) else None
    call = o.get("call_id")
    call = str(call) if isinstance(call, (str, int)) and not isinstance(call, bool) else None
    phone = parse_phone(o.get("phone")) if isinstance(o.get("phone"), (str, int)) else None
    ph = vault.phone_hash(phone) if phone else None
    payload = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    code = payload.get("disposition_code") if etype.startswith("disposition.") else None
    code = code.strip() if isinstance(code, str) else None
    return (str(eid), etype, wf, call, ts, ph, code, json.dumps(payload, separators=(",", ":")))
