import csv
import json
import subprocess
import sys
from pathlib import Path

from solution import cli

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "assignment-a-data" / "tools" / "generate_data.py"
REPORTS = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]


def generate(out: Path, seed: int):
    subprocess.run([sys.executable, str(GEN), "--seed", str(seed), "--scale", "small", "--out", str(out)],
                   check=True, capture_output=True)


def run(db, *args):
    assert cli.main(["--db", str(db), *map(str, args)]) == 0


def read_reports(d: Path) -> dict:
    return {n: (d / n).read_text(encoding="utf-8") for n in REPORTS}


def write_drop(root: Path, name: str, leads: dict, events: list):
    """leads: {(workflow, batch): csv text}; events: list of dicts or raw strings."""
    for (wf, batch), text in leads.items():
        p = root / name / "leads" / wf / f"{batch}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if events:
        p = root / name / "events" / f"events_{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events) + "\n")


def rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
