# AI Usage Disclosure

## Tools used
Claude (Anthropic), via the Claude chat interface.

## What I used them for
Claude read the assignment and the generator, then wrote the first full draft of the solution:
the normalisation functions, the SQLite schema, ingestion, the report logic, the CLI, the
test-suite (including the naive "oracle" implementation used to cross-check the pipeline), and
I asked it to explain each and every code.

## What I wrote or decided myself

Claude drafted the code and design; I didn't write the core solution myself. What I did:

- Picked Assignment A because its output can be checked exactly, so I could verify correctness
  instead of trusting it.
- Set it up on my own machine, ran all 67 tests, and ran the pipeline end to end on two seeds
  (42 and a fresh one, 99), including re-running `ingest` to confirm nothing changes the second time.
- Read through the main design choices and made sure I can explain them: events are stored as
  received and linked to leads only when reports are built (so a late-arriving lead still
  picks up its earlier events), the event ledger can't be edited or deleted, and phone numbers
  are matched with a keyed hash rather than stored.
- Filled in my details and verification notes in `SUBMISSION.md`.


## Where the AI was wrong or unhelpful
- A scenario test Claude wrote failed because its own CSV fixture left `Rs. 1,000` unquoted, so
  the comma split the cell and shifted the columns (field_warnings came out 3 instead of 1). The
  bug was in the test data, not the pipeline; fixed by quoting. It was noticed because the test
  asserted exact counters.
- The setup instructions Claude first gave me assumed Linux/Mac (`python3`, `source .venv/bin/activate`),
  so they didn't work in my Windows PowerShell and I had to switch to the Windows equivalents.
- Claude's `.gitignore` setup missed the encryption key file (`ledger.db.key`) that the pipeline
  creates next to the database, and the extra data folders I generated. I caught this before committing
  and added `*.key` to the ignore rules so no secret or generated data would be pushed.

## How I verified AI-generated code
- Ran the full test suite on my own machine (Windows, Python 3.11.8): all 67 tests pass. These include
  a cross-check against a separate, deliberately naive implementation of the spec on two generator
  seeds, and checks that re-running `ingest` and ingesting drop by drop vs. all at once give
  identical reports.
- Ran the pipeline myself end to end on seed 42 and re-ran `ingest drop_2` to confirm it skips
  already-ingested files. The summary numbers matched what Claude had reported.
- Ran it on a second, unseen seed (99) with a fresh database to confirm it works on data it
  wasn't built against, then opened the report files to sanity-check them (phones masked,
  rejections contain no personal data).
- I read through `normalize.py`, `ingest.py` and `reports.py` so I can explain how they work.