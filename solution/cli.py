"""CLI:  python -m solution ingest <drop-or-folder> [--db ledger.db]
        python -m solution report --out reports/ [--db ledger.db]"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import db, ingest, pii, reports, schema

DEFAULT_SCHEMAS = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m solution")
    ap.add_argument("--db", default="ledger.db", help="SQLite database path (default: ledger.db)")
    ap.add_argument("--schemas", default=str(DEFAULT_SCHEMAS), help="folder with workflow schemas + dispositions.csv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="ingest one drop (or a folder of drops, in name order)")
    p.add_argument("path")
    p = sub.add_parser("report", help="write the five report files")
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    t0 = time.time()
    vault = pii.PiiVault(pii.load_key(args.db))
    conn = db.connect(args.db)
    db.check_key(conn, vault)
    schemas = schema.load_schemas(Path(args.schemas))
    db.sync_dispositions(conn, schema.load_dispositions(Path(args.schemas)))

    if args.cmd == "ingest":
        path = Path(args.path)
        if not path.is_dir():
            print(f"not a directory: {path}", file=sys.stderr)
            return 2
        for drop in ingest.find_drops(path):
            st = ingest.ingest_drop(conn, vault, schemas, drop)
            print(f"{drop.name}: ingested {st['files_ingested']} file(s), "
                  f"skipped {st['files_skipped']} already-ingested ({time.time() - t0:.1f}s)")
    else:
        dq = reports.write_reports(conn, args.out)
        print(f"reports written to {args.out} ({time.time() - t0:.1f}s)")
        print(dq)
    conn.close()
    return 0
