# Supabase B1: trial tooling and the Packer registry from one Store query

Runner task 48 (Todoist 6j2vcGvxrCVr227M). Target `supabase-trial`: Fulfilment fork `F` =
`cognitiveghost/shopify-fulfillment-tool-db`, Packer fork `P` = `cognitiveghost/packing-tool-db`.
Parent design: `docs/superpowers/specs/2026-11-13-supabase-fork-design.md` (§ numbers below refer to it).
Parent plan: `~/Obsidian/Claude/supabase-fork-plan.md` (Tasks 10, 12, 15). Ledger:
`~/Desktop/Projects/shopify-fulfillment-tool-db/.superpowers/sdd/supabase-fork-plan/progress.md`.

One PR per repo. The F PR merges first: P's CI checks out F at the sha pinned in
`scripts/shared_synced_from.txt`.

## Why

The trial (§9) needs data to run on, a check that no real data got in, fake Shopify orders to pull, a seeded
fallback share, and a checklist to work through. None of that exists except a 5-session loader. Separately, the
Packer's Sessions page in Supabase mode rebuilds its registry by walking every session over HTTP on every refresh
(about 8 requests per session plus 5+ per list, every 120 s while the page is open), and a read that fails halfway
silently drops that session from the list. The owner ruled (run 123) that this becomes one Store query before the
trial goes live.

## Goals

- **Part A (F only):** the code steps of plan Task 10 steps 1–2, Task 12 steps 2–3 (no live push), Task 15 step 1.
- **Part B (F `shared/` + P):** `Store.packing_overview(client_id)` answers the Packer registry in a fixed number
  of requests per client; `SupabaseStore.local_path` stops re-downloading an unchanged blob.

## Non-goals

- No migrations, no remote schema change, nothing applied to project `ntzqarcksthplcdzivep`.
- No live Shopify push, no tokens anywhere in git, chat or logs.
- No change to the Packer's file-mode registry (`registry_index.json`, `refresh_available_lists`, the one-time
  `ensure_registry` migration). File mode is what the fallback share runs; it stays byte-for-byte today's code.
- No `v0.1.0-db` tag, no Windows build (Task 15 step 2), no owner-side trial steps.
- Not fixing (B3's job): `gui/workers.py` probing the local cache with `os.listdir` in Supabase mode.
- Not here: §9's "26 sessions reproduce `analysis_data.json` from Supabase" run (plan Task 10 step 3 / the VM
  trial run). A remote wipe/reset command (the loader never deletes anything).

## Anti-goals

- Real customer data reaching Supabase, the repos or a dev store (§4).
- A failed read shown as an empty registry or empty session list (§4). The new query raises `StoreError`.
- A script that can be pointed at the production share or `~/Desktop/Server.` and write or delete there.

---

## Part A: trial tooling (F)

### A1. `scripts/load_fixtures_to_db.py` — two modes

Keep today's behaviour as the default (no arguments): build the guide server (`build_guide_server.build`) into a
temp folder and copy every wanted file through `SupabaseStore` with the existing `load(store, root)`.

Add `--fixtures PATH [--client ID]`:

- `PATH` is resolved and must be `REPO/tests/replay/fixtures` or below it (`Path.resolve()` then
  `is_relative_to`). Anything else: print `refusing: <path> is not under tests/replay/fixtures` to stderr, exit 2,
  before any network call. A `PATH` below the root is accepted only when it is a client folder (it has
  `*/meta.json` children); otherwise the same refusal.
- New pure function `build_fixture_server(root: Path, fixtures: Path, client: str | None = None) -> int` writes a
  server folder in today's file layout into an empty `root` and returns the number of sessions written. For each
  client folder (ALMA, HERBAR, LIDA; or just `--client`):
  - Client profile on `root` through `ProfileManager(base_path=str(root))`: `create_client_profile(client_id,
    client_id)` then `save_shopify_config(client_id, config, backup=False)` with the config named by the newest
    session's `meta.json["config"]` (same as `build_guide_server._latest_config`), `inventory_memory.enabled = True`.
  - For each session folder `<CLIENT>/<session>/` that has `meta.json` (26 in total), under
    `Sessions/CLIENT_<id>/<session>/` with the fixture folder name as the session name:
    - `session_info.json`: the same keys `SessionManager.create_session` writes
      (`shopify_tool/session_manager.py:144`), with `created_at` and `last_modified` = `meta["created_at"]`,
      `session_name` = folder name, `pc_name` = `"fixture-loader"`, `status` = `"active"`,
      `orders_file` = `"orders_export.csv"`, `stock_file` = `"inventory.csv"`.
    - `input/orders_export.csv`: the fixture `orders.csv` bytes unchanged.
    - `analysis/memory_baseline.json`: the fixture `memory_baseline.json` unchanged.
    - `input/inventory.csv`: `core.baseline_stock_df(baseline)` written by `stock_csv_bytes(df, config)` (A4).
  - No analysis is run and no packing lists are written: the trial runs analysis on these sessions itself (§7.6).
- `main` then signs in exactly as today (`_client()`: `SUPABASE_SERVICE_KEY` if set, else email + password with
  the publishable key, so it works against the remote with the owner's sign-in) and calls `load(store, root)`.
- Idempotent: a second run writes the same paths with the same bytes, so the count stays 26. Session names come
  from the fixtures, never from today's date.
- Default mode is not idempotent and stays that way: each run adds that day's guide sessions (`<date>_N`), as today.
  Fixture mode writes no workers or `packer_config.json`; default mode does, so a full trial dataset is both runs.
- Usage line and the module docstring describe both modes and the local reset (A6).

### A2. `scripts/check_no_real_data.py`

Exit 0 when every Customer and Notes value in Supabase is anonymised, exit 1 listing the first 20 offenders
(`table/key: value`) otherwise. Reads only through a signed-in Supabase client (`_client()` imported from the
loader); never reads the local disk except its own code.

A value is accepted when any of:
1. it is blank, or `"nan"`;
2. it is in `seed_dev_store.FAKE_NAMES`;
3. masking it again leaves it unchanged: `anonymise.mask_text(value, placeholder, words) == value`, where
   `placeholder` is `"Customer"` or `"Note"` and `words = anonymise.rule_words(config, "Customer"|"Notes")` for that
   row's client (`clients.shopify_config`). The anonymiser's output is a fixed point of its own mask, and a real
   name is not, which is exactly the test we want. Import `mask_text`/`rule_words` from `tests/replay/anonymise.py`.

Where it looks (all paged with `.range()`, PostgREST caps a select at 1000 rows):
- `session_documents.body`, `packing_lists.body`, `fulfillment_history.row`: walk the JSON; every value under a key
  named `customer` or `notes`, compared case-insensitively (packing lists and `analysis_data.json` use lowercase
  `notes`), at any depth, is checked; `system_note` is app text, not a person's, and is skipped.
  `fulfillment_history` today holds only `Order_Number, Execution_Date, Session`; the same walker runs over its
  `row` jsonb so a future Customer column is covered, and finds nothing today.
- Storage blobs `Sessions/CLIENT_<id>/<session>/input/orders_export.csv` for every session row: read with pandas,
  map columns through the client's `column_mappings.orders` (CSV header → internal name), check the columns mapped
  to `Customer` and `Notes`. *(Builder's call extending the plan: §9 says "every Customer and Notes value in
  Supabase", and pulled or loaded order CSVs carry names before any analysis runs.)*

### A3. `scripts/seed_dev_store.py`

Pure core, network at the edge:

- `FAKE_NAMES: tuple[str, ...]`: `tuple(f"{first} {last}" for first in _FIRST for last in _LAST)` from two
  committed tuples of obviously fake words (e.g. `_FIRST = ("Testa", "Proba", ...)`, `_LAST = ("Probova",
  "Fiktiva", ...)`), at least 200 names, no real-looking name. *(Builder's call: two committed lists multiplied
  are as deterministic as one literal tuple of 200.)*
- `SeedPlan` dataclass: `client`, `products: list[dict]` (`sku`, `title`), `orders: list[dict]` (`name` from
  `FAKE_NAMES`, `country`, `shipping_method`, `lines: list[dict(sku: str | None, title, quantity)]`).
- `build_seed(client, fixtures, n_orders, rng_seed) -> SeedPlan`: pure, `random.Random(rng_seed)`. Products = the
  distinct SKUs (with their `Lineitem name`) across the client's fixture `orders.csv` files. Orders draw shipping
  methods and countries from the same files' `Shipping Method` / `Shipping Country` values. 1–3 lines per order;
  about 1.5% of lines have `sku=None` (exercises the no-SKU rule). Same arguments, same plan.
- `push_seed(plan, shop, token, transport=None)`: `httpx.Client(transport=transport)` against
  `https://{shop}/admin/api/{API_VERSION}/graphql.json` (`API_VERSION` imported from
  `shopify_tool.order_sources.shopify`), header `X-Shopify-Access-Token`. One `productCreate` per product, then one
  `orderCreate` per order (test orders, `sendReceipt`/`sendFulfillmentReceipt` false). A non-empty `userErrors`
  or GraphQL `errors` raises `RuntimeError` naming the product or order; it does not continue silently. Tested
  only with `httpx.MockTransport`; it is never run in this task.
- `write_stock_csv(plan, path, config)`: one row per product with a stock quantity from the plan's rng, written by
  `stock_csv_bytes` (A4) so headers are the client's (`Артикул, Име, Наличност`).
- CLI: `seed_dev_store.py --client ALMA --shop <x>.myshopify.com --orders 300 [--seed 1] [--stock-out PATH]`.
  Token from env `SHOPIFY_SEED_TOKEN` only (never argv, never printed). Refuses a shop not ending in
  `.myshopify.com`.

### A4. Stock CSV helper (shared by A1 and A3)

`stock_csv_bytes(df, config) -> bytes` in `scripts/load_fixtures_to_db.py`: renames internal columns
(`SKU`, `Product_Name`, `Stock`) back to the client's CSV headers by inverting `config["column_mappings"]["stock"]`,
writes UTF-8 CSV with `,`. Its test reads the bytes back with the app's own stock reader path
(`core` with `"auto"` delimiter) and gets the same SKUs and quantities. `seed_dev_store` imports it.

### A5. `scripts/seed_fallback_share.py`

`seed_fallback_share.py DEST [--reset]` fills a folder for the file-fallback trial (§7.3) with the same data the
loader's default mode puts in Supabase: it calls `build_guide_server.build(DEST)`, then writes a marker file
`DEST/.fallback-share-seed`.

Refuses (exit 2, nothing written or deleted):
- a DEST under `~/Desktop/Server.` (resolved), or whose string form starts with `\\192.168.88.101\` (case-insensitive,
  either slash direction) — the production share;
- a non-empty DEST without `--reset`;
- `--reset` on a non-empty DEST that has no marker file (it was not seeded by this script, so it is not ours to wipe).

`--reset` with the marker: delete DEST's contents, then seed. *(Builder's call: the marker makes reset safe on a
share someone else may have pointed us at by mistake.)*

### A6. `docs/trial-checklist.md`

§9's Windows checklist as checkboxes (English), plus:
- **Reset step (local stack)** at the top of each round: in the F main checkout `supabase db reset` (re-applies the
  migrations on an empty database), then `scripts/load_fixtures_to_db.py --fixtures tests/replay/fixtures` and
  `scripts/load_fixtures_to_db.py` with `SUPABASE_SERVICE_KEY` from `supabase status`. Say why: the
  `@pytest.mark.supabase` tests wipe every table and the bucket of the local stack, so any test run destroys the
  manual trial data. For the remote: load once with the owner's sign-in; no reset there.
- Setup: sign-in users per PC, `seed_fallback_share.py` for the fallback share, `seed_dev_store.py` for the dev
  store (owner runs it with the seeding app's token), `check_no_real_data.py` after loading and after each round.

### A tests (F, `tests/`)

- `test_load_fixtures.py` (extend): `build_fixture_server` on a temp dir → 26 sessions (`FileStore(root)
  .query_sessions` summed over ALMA/HERBAR/LIDA), 3 clients, rerun into a second temp dir gives identical bytes;
  `--client HERBAR` → only HERBAR; `main(["--fixtures", "/tmp/x"])` → `SystemExit(2)` and "refusing" on stderr,
  with `_client` monkeypatched to fail if called. Supabase-marked: load the 26 into the test stack, count via
  `SupabaseStore.query_sessions` = 26, run again = 26.
- `test_check_no_real_data.py` (Supabase-marked): passes after the 26-session load; fails after inserting a
  packing list whose order has `"Customer": "Ivan Petrov"`; plus pure tests of the acceptance rule.
- `test_seed_dev_store.py`: `build_seed` deterministic for a seed, every SKU from fixtures, every name in
  `FAKE_NAMES`, no-SKU share between 1% and 2% over 300 orders; `len(FAKE_NAMES) >= 200` and unique; `push_seed`
  against a `MockTransport` sends N productCreate + M orderCreate with the token header, and raises on `userErrors`;
  `write_stock_csv` headers are the client's.
- `test_seed_fallback_share.py`: each refusal; seeded DEST read by `FileStore` (`query_sessions` per client
  non-empty, a packing list readable); reset with marker reseeds; reset without marker refuses and deletes nothing.

---

## Part B: the Packer registry from one Store query

### B1. `Store.packing_overview(client_id) -> list[dict]` (F `shared/store.py`)

One row per live packing list of the client's non-archived sessions, plus (FileStore only) one per legacy
`barcodes/` session. Unknown client or no sessions → `[]`. Any failed read → `StoreError`; it never returns a
partial list. Row keys:

| key | value |
|---|---|
| `session_name` | session folder name |
| `list_name` | packing list name (file stem); legacy: `state["packing_list_name"]` or `"unknown"` |
| `work_rel` | `Sessions/CLIENT_<id>/<session>/packing/<list>`; legacy: `.../<session>/barcodes` |
| `packing_list` | the list document trimmed to `PACKING_LIST_KEYS`, or `None` for legacy rows |
| `started` | the list has a work folder (`packing/<list>` exists / a `packing_state` row exists) |
| `state` | `packing_state.json` trimmed to `PACKING_STATE_KEYS`, or `None` |
| `summary` | `session_summary.json` whole, or `None` |
| `lock` | `LockInfo` for `work_rel` (what `lock_info` returns), or `None` |
| `session_info` | `{"pc_name": ...}` when the session has `session_info.json`, else `None` |

Module constants in `shared/store.py`:
`PACKING_LIST_KEYS = ("courier", "filters_applied", "created_at", "total_orders", "total_items")`,
`PACKING_STATE_KEYS = ("worker_id", "worker_name", "pc_name", "started_at", "last_updated", "progress",
"skipped_orders")`. Trimming keeps only keys present in the document (an absent key stays absent, so the Packer's
`.get(k, default)` behaves as today). A trimmed state with none of the keys is `None` (both backends). Order: by `session_name`, then `list_name`.

**FileStore:** walks `Sessions/CLIENT_<id>/` exactly as `build_from_scan` does today
(P `packing_tool/session_registry_manager.py:219-330`): `packing_lists/*.json` per session, `packing/<list>`
existence, `packing_state.json` / `session_summary.json`, `lock_info(work_rel)`, `session_info.json`; legacy
`barcodes/packing_state.json` when the session has no `packing_lists/`. A list document without `total_orders`
gets `total_orders = len(orders)` before trimming (today's fallback for old files). Unlike today, an unreadable or
corrupt document raises `StoreError` instead of being skipped. *(Builder's call: follows the "a failed read
raises" ruling; it only reaches the Packer in derived mode, see B3.)*

**SupabaseStore:** five paged selects per client (`self._all`, ordered), independent of the number of sessions,
joined in Python:
1. `sessions` `name` where `client_id`, `archived=false` → the live session names.
2. `sessions` `name, pc_name:info->>pc_name` where `client_id`, `archived=false`, `info` not null → the sessions
   that have `session_info`.
3. `packing_lists` `session_name, name` plus one `<key>:body-><key>` per `PACKING_LIST_KEYS`, where `client_id`,
   `withdrawn=false`; keep rows whose session is in (1); drop null keys.
4. `packing_state` `session_name, list_name, summary` plus `<key>:state-><key>` per `PACKING_STATE_KEYS`.
   A row means `started`. `state` = the non-null projected keys, or `None` when none is set (a real
   `packing_state.json` always carries `progress`).
5. `list_locks` `session_name, list_name, holder_pc, holder_user, heartbeat_at` → `LockInfo(True, ...)` exactly as
   `lock_info` builds it.
Rows are driven by (3): `packing_state` and `list_locks` rows only attach to a (session, list) that (3) kept, so an
archived session or a withdrawn started list never appears, the same as FileStore's walk from `packing_lists/*.json`.
No legacy rows (the trial never holds Excel-workflow sessions). Wrapped in `_guard`, so any API/network error is a
`StoreError`. Lists without `total_orders` stay without it (every DB-era list carries it).

**Contract test** (F `tests/shared/test_store_contract.py`, both backends through the existing `store` fixture):
seed one client with three sessions through the store's own writes — one with an unstarted list, one with a started
list holding `packing_state.json` and a lock taken with `acquire_list_lock`, one with a finished list holding
`session_summary.json`, plus a withdrawn list that must not appear (archived sessions and a withdrawn list that still has state/lock rows are Supabase-only tests: FileStore archives whole clients) — then assert
`packing_overview` returns the same rows on both backends. Plus: unknown client → `[]`; a failing backend read
raises `StoreError` (FileStore: monkeypatch its read to raise; Supabase: a client whose `table()` raises an `httpx.ConnectError`, which
`_guard` turns into `StoreError`; `_guard`'s existing exception set is enough, nothing new to wrap).
FileStore-only test: legacy `barcodes/` row and the `len(orders)` fallback.

### B2. `SupabaseStore.local_path` freshness (F)

Keep `self._fetched: dict[str, object]` (rel → the blob stamp of the copy on disk). In `local_path` for a blob
file: `current = self._blob_stamp(rel)` (one metadata call; `StoreNotFound` → return the path, as today); if
`self._fetched.get(rel) == current` and the file exists, return it without downloading; else download, write,
record `current`. A blob changed between the stamp and the download is fetched again next time (safe direction).
Remove the `ponytail:` comment. Test (Supabase-marked, `tests/shared/test_store_supabase.py`): two calls download
once (count `download` calls through a wrapper on `store._bucket`); after `write_bytes` with new content, the next
call returns the new bytes.

### B3. Packer: derived registry from the overview (P)

- `scripts/sync_shared.py <F worktree>` after the F commits are pushed; commit the synced `shared/` and the new pin.
- `packing_tool/offline_buffer.py` `BufferedStore`: forward `packing_overview` like `query_sessions`
  (`self._inner(self.inner.packing_overview, client_id)`).
- `session_registry_manager.py`: `build_from_scan` in derived mode (`store.derived_indexes`) builds the registry
  from `store.packing_overview(client_id)` and makes no other store call except `local_path` for the
  `work_dir`/`session_path`/`packing_list_path` strings. A `StoreError` propagates (no `except Exception` around it).
  File mode keeps today's walk unchanged.
- One entry shape: extract the two entry dict literals into helpers (`_started_entry(...)`,
  `_available_entry(...)`) used by both the file-mode `_register_*` methods and the derived path, so the 17-key
  session entry and the 9-key available entry exist once. Status rules stay: summary → `completed`/`incomplete`;
  else state → lock present → `in_progress`, session_info present → `paused`, else `incomplete`; available lists get
  `mtime = None` in derived mode. Remove the `ponytail:` comment.
- Tests (P `tests/test_session_registry.py`, which already runs derived mode on a `FileStore` subclass with
  `derived_indexes = True`): the existing derived tests still pass; new: the derived read makes exactly one store
  call besides `local_path` (spy store); a `StoreError` from `packing_overview` reaches the caller; derived and
  file-mode scans of the same folder give the same entries (minus `mtime`).

Skipped: `_effective_status` still calls `lock_info` once per `in_progress` entry to age the heartbeat (a handful
per refresh, not per session). Add the heartbeat to the entry if that ever shows up.

## Order of work

F: A1+A4 → A2 → A3 → A5 → A6 → B1 → B2 (gate) → push. P: sync → B3 (gate) → push. One PR each (Stage C).

## Gates

- F: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` via `scripts/run_tests.sh` (sets the Supabase env
  from the local stack in the F main checkout; do not start a second stack) and `ruff check .`.
- P: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` and `python scripts/coverage_check.py`.
- After the F suite, the local trial data is gone: rerun `scripts/load_fixtures_to_db.py` with the local stack's
  service key (both modes) to restore it.

## Left to the builder

Names of private helpers, how `build_seed` spreads lines and quantities, the exact GraphQL mutation text (checked
against Shopify's `2026-10` Admin API docs via context7/web, still only mock-tested), the CSV quoting.
