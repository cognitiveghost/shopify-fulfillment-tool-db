# DB trial: benchmark after the speed changes

Measured with `scripts/bench_db.py` (file table at F commit `50317f9`; supabase tables at the branch head) (branch `dr/57-db-trial-pull-orders-preview-select-reme`).
Same commands and dataset as `2026-11-db-trial-before.md`: client ALMA, replay session `2026-09-30_6`, stack reset
and both fixture loads first, one run per store, `--rtt-ms 0` (local stack only, no hosted call). With 0 ms RTT the
"est. hosted" column equals wall ms. Hosted estimate = wall ms + HTTP requests × your hosted RTT, so **requests are
the number to compare**; wall ms on a single local run is noisy.

The benchmark now drains background pools before each step (`50317f9`). The first "before" run did not, so its
session-open count (50) included requests from the window's startup client gather. For a like-for-like comparison
"before" was re-run on the pre-change code (`1feddf3`) with only that harness fix applied, on a freshly reset and
loaded stack; the tables below compare against that re-run. The supabase "after" table is from the branch head
(pull write fix included), also on a fresh reset and load.

## Supabase (local stack)

| action | store | wall ms | HTTP requests | est. hosted ms |
|---|---|---:|---:|---:|
| c analysis | supabase | 618.8 | 55 | 618.8 |
| a session open | supabase | 213.7 | 10 | 213.7 |
| b pull write | supabase | 42.2 | 1 | 42.2 |
| d packing lists | supabase | 241.1 | 17 | 241.1 |

| action | requests before → after | wall ms before → after |
|---|---|---|
| c analysis | 57 → 55 | 638.2 → 618.8 (flush and history now on the worker) |
| a session open | 17 → 10 | 321.1 → 213.7 |
| b pull write | 3 → 1 | 81.4 → 42.2 |
| d packing lists | 20 → 17 | 267.2 → 241.1 |

## File (local disk)

| action | store | wall ms | HTTP requests |
|---|---|---:|---:|
| c analysis | file (local disk) | 222.7 | 0 |
| a session open | file (local disk) | 159.8 | 0 |
| b pull write | file (local disk) | 1.6 | 0 |
| d packing lists | file (local disk) | 51.8 | 0 |

File = local disk, a floor, not an SMB share. These are not the file-fallback trial numbers.

## What changed the numbers

- **Session open (a):** 17 → 10 requests. The stock file is downloaded once, `session_info` is read once, and the
  whole read runs on a worker, so the UI thread makes no store call while a session opens.
- **Analysis (c):** the write-queue flush and the history read no longer happen on the UI thread; the history is read
  once per run. Request count is nearly flat (57 → 55); the win is the UI thread, not the total.
- **Packing lists (d):** 20 → 17. `exists` on a row route no longer fetches the body; the packing-list listing reads
  name and withdrawn in one query; the folder check skips `exists` when a list is found.
- **Pull write (b):** 3 → 1. The pulled CSV is uploaded and its bytes written straight into the local cache,
  instead of a stamp check and a download of the file just uploaded (spec §3.3.5 fallback).

## Condition A (row d against `2 + 2 × lists`)

The session holds 1 packing list, so the threshold is 4 and row d (17) is over it. The plan's fix (one read of all
`packing_lists` rows) would save about nothing with one list: the 17 requests are not per-list body reads. They are
the barcode label-PDF invalidation (`exists` ×2, then listing after each failed info), the session row reads and
upserts (`_ensure_client`, `_ensure_parents`, `update_json`), and the xlsx upload. The plan's fix was **not built**.
Left on the table, with the saving each would give: drop the `exists` guards in
`barcode_processor.invalidate_label_pdfs` (about 4, skipped: it changes the error path on denied stat), cache the
ensured client/session per store instance (about 2), and read the session row once in `update_json` (about 1).

## Condition B (P)

Not needed: see P's `after` doc.

## Dropped candidates

- Blob stamp reuse on `read_bytes`/`write_bytes` (spec §3.3 items 2, 3): Supabase storage returns no stamp from an
  upload or download, so recording one costs an extra request. Dropped.
- Per-list statistics batch read (condition A): see above. Dropped.
