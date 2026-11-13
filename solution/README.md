# Assignment A: Interaction Ledger & Reporting Pipeline

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution ingest data/drop_1          # idempotent; run it as often as you like
python -m solution ingest data/drop_2
python -m solution report --out reports/       # always reflects everything ingested so far
pytest                                         # 67 tests, ~10 s
```

* `python -m solution ingest data` (a folder of drops) ingests all drops in natural name order.
* `--db PATH` selects the SQLite file (default `./ledger.db`). A PII key file `ledger.db.key` is created
  next to it on first use; set `LEDGER_PII_KEY` (a Fernet key) to supply your own.

Layout: `normalize.py` (pure rules) · `schema.py` · `db.py` (DDL) · `ingest.py` · `reports.py` · `pii.py` · `cli.py`.
Design notes, decisions and trade-offs: see `../SUBMISSION.md`.
