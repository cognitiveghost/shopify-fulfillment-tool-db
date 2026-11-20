# DB trial: benchmark before any code change

Measured with `scripts/bench_db.py` (added in the commit that adds this file; `git log -- scripts/bench_db.py`).
Client ALMA, replay session `2026-09-30_6` copied as the input. Each run creates a fresh ALMA session.

## Supabase (local stack, one run after `supabase db reset` and both loads)

Command: `FULFILLMENT_DB_URL=http://127.0.0.1:54321 SUPABASE_SERVICE_KEY=… scripts/bench_db.py --store supabase --rtt-ms 0`

| action | store | wall ms | HTTP requests | est. hosted ms |
|---|---|---:|---:|---:|
| c analysis | supabase | 630.7 | 57 | 630.7 |
| a session open | supabase | 335.7 | 50 | 335.7 |
| b pull write | supabase | 35.1 | 3 | 35.1 |
| d packing lists | supabase | 208.7 | 20 | 208.7 |

RTT used: **not measured (0 ms).** This run made no hosted call, so the "est. hosted ms" column equals wall ms.
Hosted estimate = wall ms + HTTP requests × RTT. Compute it with the owner's hosted RTT (`--rtt-ms N`, or the
default probe run from a machine with access to the hosted project).

## File (local disk)

Command: `scripts/bench_db.py --store file`

| action | store | wall ms | HTTP requests | est. hosted ms |
|---|---|---:|---:|---:|
| c analysis | file (local disk) | 177.7 | 0 | 177.7 |
| a session open | file (local disk) | 105.4 | 0 | 105.4 |
| b pull write | file (local disk) | 0.4 | 0 | 0.4 |
| d packing lists | file (local disk) | 46.0 | 0 | 46.0 |

File = local disk, a floor, not an SMB share. A real share is slower; these numbers are not the file-fallback trial.

## Notes

- Session: replay `2026-09-30_6`. The Supabase run analysed `2026-11-20_4`.
- The file store is loaded with the same ALMA replay fixtures as the stack (`build_fixture_server`), not with
  `scripts/seed_fallback_share.py`, so both stores hold the same session.
- Single run per store. Rerun after a reset and load for a comparable "after" table.
- Action (e), the packer list open in P, is not measured here.
