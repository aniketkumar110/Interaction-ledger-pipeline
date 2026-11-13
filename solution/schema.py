"""Workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import norm_header


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]
    header_lookup: dict[str, str] = field(default_factory=dict)  # normalised header -> field name

    def __post_init__(self):
        # first field (in schema order) wins if two fields share an alias
        for f in self.fields:
            for key in (f.name, *f.aliases):
                self.header_lookup.setdefault(norm_header(key), f.name)
        refs = [f for f in self.fields if f.reference]
        phones = [f for f in self.fields if f.type == "phone"]
        if len(refs) != 1 or not phones:
            raise ValueError(f"{self.workflow_id}: need exactly one reference field and a phone field")
        if refs[0].pii:
            raise ValueError(f"{self.workflow_id}: reference field must not be PII")
        self.reference_field = refs[0].name
        self.phone_field = phones[0].name


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    out = {}
    for p in sorted(Path(schema_dir).glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        fields = [
            Field(
                name=f["name"], type=f["type"], required=f.get("required", False),
                pii=f.get("pii", False), reference=f.get("reference", False),
                aliases=tuple(f.get("aliases", [])), values=tuple(f.get("values", [])),
                default=f.get("default"),
            )
            for f in d["fields"]
        ]
        out[d["workflow_id"]] = WorkflowSchema(d["workflow_id"], fields)
    return out


def load_dispositions(schema_dir: Path) -> list[tuple[str, str, int, int]]:
    rows = []
    with open(Path(schema_dir) / "dispositions.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((r["workflow_id"].strip(), r["code"].strip(),
                         int(r["is_terminal"].strip().lower() == "true"),
                         int(r["is_success"].strip().lower() == "true")))
    return rows
