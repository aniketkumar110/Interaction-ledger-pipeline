# Submission

## Candidate
- **Name:** Aniket Kumar
- **Assignment chosen:** Assignment A: Interaction ledger pipeline
- **Time spent (honest estimate):**  4.5hours (setup and running the pipeline, reading and understanding the code, writing up the docs)

## How to run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/
pytest            # unit + scenario tests + cross-check against a naive oracle on seeds 42 and 7
```
`python -m solution ingest data` ingests every drop folder in order. `--db` picks the SQLite file.

## Design
**Principle: store facts, derive everything else.** Ingestion only writes immutable facts (leads
with merge semantics, the event ledger, rejections, per-file counters). Buckets, effective
dispositions, orphan/unknown counts and all five reports are computed from those tables at report
time. That is what makes "events that arrive before their lead" and "drop by drop == all at once"
true by construction: nothing is ever "resolved" at ingest time that a later drop could change.

```
ingested_files(id, drop_name, relpath, sha256, kind, counters...)   -- idempotency + data_quality counters
leads(id, workflow_id, phone_hash, phone_last4, batch_id, external_ref, attrs JSON, pii_enc)
      UNIQUE(workflow_id, phone_hash)
rejections(file_id, workflow_id, batch_id, row_number, reason)       -- no PII
events(seq, event_id UNIQUE, event_type, workflow_id, call_id, occurred_at_us, phone_hash,
       disposition_code, payload JSON, file_id)                      -- append-only (triggers abort UPDATE/DELETE)
dispositions(workflow_id, code, is_terminal, is_success)             -- synced from dispositions.csv each run
```
* **Ledger:** every well-formed, first-seen event is stored, including ones that link to no lead yet
  (`v_unlinked_events` is the quarantine view). Linking is a join on `(workflow_id, phone_hash)`
  evaluated at read time. Overrides (no phone) link through `call_id` to the lead of the call's other
  events. SQLite triggers make UPDATE/DELETE on `events` abort, so "overrides never edit the original"
  is enforced by the database, not just by convention.
* **Idempotency:** each file is processed in one transaction together with its `ingested_files` row
  (key = drop name + relative path, plus a SHA-256). Crash mid-file => rollback, nothing half-applied.
  Re-run => file skipped. Same path but different content => hard error instead of silently
  double-counting.
* **PII:** `customer_name` and the full phone live only inside a Fernet token (`pii_enc`). Matching
  uses `HMAC-SHA256(key, "+91XXXXXXXXXX")`, a *keyed* hash, because a plain hash of a 10-digit number
  is brute-forceable in minutes. Events store only that hash, never the phone. `phone_last4` is stored
  because the lead report must show it. The key comes from `LEDGER_PII_KEY` or a `<db>.key` file
  (dev convenience; production => KMS). A key fingerprint in `meta` stops you opening a DB with the
  wrong key. A test greps the raw DB + WAL bytes for every name/phone in the generated data.
* **Reports:** one streaming pass over events builds per-call facts, then per-lead buckets and the
  daily table in Python. Chosen for clarity and so I can cross-check it against a second,
  independent implementation (see Testing).

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Orphan / unknown-disposition counts computed from the *final* state at report time; an event that is both unlinkable and has an unknown code counts only as orphan | Spec order (orphan before unknown) + "orphan counts reflect the final state" | Counting at ingest and fixing up later: stateful and error-prone |
| Required non-phone field present but invalid gets reason `invalid_<type>:<field>` | Spec defines only two reasons; schemas never trigger this | Treat as missing |
| Phone field is always needed as identity: missing => `missing_required:<phone field>` even if a schema made it optional | A lead without identity can't exist | n/a |
| Event with unparseable `occurred_at` counts as **malformed** | Can't be placed in time, so unusable; generator never produces it | Store with NULL time |
| Ties on `occurred_at` broken by `event_id` | Deterministic regardless of ingest order | Ingest order (differs between bulk vs incremental) |
| `connectivity_rate` formatted with Python `f"{x:.4f}"` (round-half-even on the binary float) | Most likely how a Python reference formats it; differs from half-up only on exact ties such as 1/32 | `Decimal` half-up |
| Batches with zero valid leads do not appear in `batch_summary.csv` | It is a per-lead summary; those rows are in `rejections.csv` | Emit zero rows |
| A merged duplicate can change `external_ref` (last write wins, as for every field); `batch_id` never changes | Spec: overwrite non-null fields; batch_id permanent | n/a |
| Calls are keyed `(lead, call_id)`; events with no `call_id` link but affect no call | Robust to a call_id reused by two leads | Key by call_id only |
| Override with no matching call but with its own phone falls back to phone linking | Defensive; generator never does this | Always orphan |
| Header normalisation also strips after collapsing (`_phone` -> `phone`); leftmost column wins if two columns match one field | Sensible; spec silent | Literal order |
| Dates accept 1- or 2-digit day/month (`1/2/2026`) | Lenient on a format the spec lists as DD/MM | Strict 2-digit |
| SQLite, aggregation in Python | Zero setup, easy to cross-check | Postgres + SQL aggregation (stretch) |

## Failure modes considered
- **Crash mid-ingest:** per-file transaction + rollback. **Re-run / overlap:** file-level skip; event-level `event_id UNIQUE` + `INSERT OR IGNORE` so even a re-delivered *different* file can't double-count events; lead merges happen once per file.
- **Dirty input:** BOM, CRLF, short/long rows, extra and re-ordered columns, blank `,,,` rows, bad UTF-8 (decoded with replacement, then rejected as malformed/invalid), non-object JSON, non-string ids.
- **Wrong key / lost key:** fingerprint check aborts. A lost key means leads' names are unrecoverable (by design).
- **Not handled:** a changed file under an already-ingested path is refused, not versioned; no concurrent writers (SQLite single-writer, `BEGIN IMMEDIATE` makes a second writer wait/fail rather than corrupt); `payload` is stored as received, so free-text fields like `overridden_by` / `reason` are not scrubbed (supervisor identity, not customer PII, but worth a policy decision); no key rotation; reports recompute from scratch each time.

## Testing
67 tests (pytest, 10-25 s):
- Table-driven unit tests: phone (all six formats + invalid), dates (day-first, month names, impossible dates, leap years), money, enums, headers, timestamps/IST day boundary.
- Hand-built scenarios: override beats a later `set` and the original stays in the ledger; latest-of-each; all four buckets incl. "terminal beats telephony-failed"; call without `call.initiated`; orphan resolving when the lead arrives in a later drop; malformed/duplicate/unknown/orphan counters; naive == IST; row numbering/blank rows/warnings-only-for-accepted-rows/merge/defaults/batch_id permanence.
- **Oracle test:** a deliberately naive, plaintext, in-memory re-implementation of the spec (`tests/oracle.py`, shares only the leaf parsers) is diffed against the pipeline's output on generator seeds 42 and 7 (every row of every report).
- Idempotency + incremental == bulk (including an intermediate report between drops, and re-ingest of the parent folder); no plaintext PII in the DB/WAL bytes; ledger triggers.
- Not tested: concurrent runs, Postgres, the oracle and pipeline share my reading of the spec (they'd agree on a misreading, so the decisions table is where to challenge me).
- Verified on Windows with Python 3.11.8 (all 67 tests pass), plus an end-to-end run on a second unseen seed (99).

## Results
- Small (seed 42): 2,057 lead rows -> 108 rejected, 57 duplicates merged, 122 field warnings; 8,745 event lines -> 8 malformed, 237 duplicates dropped, 462 orphans, 18 unknown dispositions.
- Large (`--scale large`, ~140k lead rows, 632,602 event lines), measured in a Linux sandbox: **ingest ~37 s, report ~7 s (~44 s total)**; re-ingest is a no-op (<1 s).
- Orphans come from three sources worth knowing: calls to numbers never uploaded, events of leads whose row was *rejected* (missing name/ref), and events with an invalid phone. Rejected leads still have calls, which is a data-quality finding in itself.

## If I had another day
1. Move aggregation into SQL (window functions for latest-by-time) and run on Postgres; keep the Python oracle as the regression check.
2. Truly incremental reporting: maintain a per-call/per-lead derived table updated only for calls touched by new events. Trade-off: more moving parts and invalidation logic (a late lead re-links old events) for O(new data) instead of O(all data) per report.
3. Production at 100k leads/day: object-store drops -> a scheduler (Airflow/Temporal) runs `ingest` per file with the same file-level idempotency key; dead-letter bucket for malformed lines; backfill = re-run with a new DB and diff reports; alerts on rejection / orphan / malformed rate spikes; KMS-managed key with rotation (re-encrypt `pii_enc`, re-hash needs a key id column).
4. A `payload` allow-list/scrubber and a retention policy for the ledger.
