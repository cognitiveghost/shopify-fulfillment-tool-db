---
tags: [project, design]
updated: 2026-11-13
status: draft, awaiting owner approval
---
# Supabase + Shopify API fork: design

Trial fork of [[shopify-fulfillment-tool]] (Fulfilment) and [[packing-tool]] (Packer). Supabase becomes the data layer, the file server stays as a fallback, and Fulfilment can pull orders from Shopify instead of loading a CSV. Background: options B and F in [[fulfilment-architecture.canvas]].

## 1. Intent

The owner wants to find out, on real hardware, whether a database-backed version of both apps is worth adopting before touching the production apps. "Worth it" means running real sessions end to end:
- on both warehouse PCs at once,
- and on the owner's own PC outside the work network, where the file server cannot be reached.

Today that is impossible. Both apps refuse to start without the UNC share (`\\192.168.88.101\_Fulfilment_\0UFulfilment`), and the Packer stops writing the moment the share drops.

Second goal: stop loading orders by hand. Fulfilment pulls open orders straight from Shopify. The code is shaped so another e-commerce platform could be added later.

## 2. Goals

- G1. Two new private repos, copied from current `main`. Production repos and their CI, releases and runner targets are untouched.
- G2. Supabase (project `ntzqarcksthplcdzivep`, EU region) holds everything the file server holds today, for both apps.
- G3. Both apps start and work fully against Supabase with no file server reachable (home PC).
- G4. File server fallback: if Supabase is unreachable at startup, the app runs that whole run in today's file mode against a configured folder.
- G5. Packer offline buffer: if Supabase drops mid-run, the Packer keeps packing on local files and uploads when back.
- G6. Fulfilment "Pull orders" fetches open, unfulfilled orders from a Shopify store into the same orders table a CSV import builds. Everything after that is unchanged.
- G7. The 26 anonymised replay sessions load into Supabase and can be analysed and packed end to end on all three PCs.
- G8. A Shopify Partner account and development store, seeded with fake products and orders built from the anonymised ALMA/HERBAR SKUs.

## 3. Non-goals (this trial)

- Writing anything back to Shopify: no fulfilment status, tracking or stock. Pulling is read-only.
- Pulling stock from Shopify. Stock still comes from the stock file. Who owns stock is still an open question in the canvas.
- Platforms other than Shopify. Only the seam for them exists (§7.4).
- Merging any of this back into production. That is a separate decision after the trial.
- A second warehouse, packers pulling orders on demand, or a web or mobile client.
- Per-worker logins and permissions beyond "a signed-in PC sees everything".

## 4. Anti-goals (a failure even if it works)

- **Any real customer data in Supabase, the fork repos or the dev store.** Only anonymised replay data and fake dev-store orders. The fork must never be pointed at the production share or a real Shopify store. Raw `~/Desktop/Server./` data never enters the fork.
- **A trial build reaching a warehouse PC's production install by accident.** The updater must not see fork releases, and production must not see fork builds.
- **Two sources of truth.** An app never writes the same data to Supabase and the file server in one run.
- **Silent data loss.** Today's `read_registry` returns an empty registry on a read error. No backend may copy that pattern: a failed read is an error, not "empty".
- **A database open to anyone with the URL.** Every table has row level security (RLS), and only signed-in users get through.

## 5. Constraints and facts (from the code, 2026-11-13)

- Both apps resolve the server root in `shared/server_connection.py` (env `FULFILLMENT_SERVER_PATH` > QSettings > default). `ProfileManager` in each app binds `Clients/`, `Sessions/`, `Stats/`, `Logs/`, `Workers/`.
- File I/O is scattered. Fulfilment has about 69 direct path joins in 14 files and about 104 raw read/write calls in 39 files. The Packer has about 44 `open(` and 36 `json.load/dump` calls, 33 of its path references in `profile_manager.py`. Common helpers: `shared/atomic_write.py`, `shared/file_lock.py`, `shared/stats_manager.py`.
- Formats: mostly JSON. Plus CSV (`fulfillment_history.csv`, input copies), a pandas pickle (`analysis/current_state.pkl`), XLSX packing lists and stock exports, label PDFs, and the PDF guides.
- Two lock styles: `shared/file_lock.py` (5 s retry) and `session_manager._exclusive_lock` (blocking). The Packer list lock is `<session>/packing/<list>/.session.lock`, with a 60 s heartbeat (a QTimer in `main_window.py:930`) and stale after 120 s.
- `shared/` is canonical in Fulfilment and synced to the Packer.
- Ingest: `run_full_analysis` → `_load_and_validate_files` reads the CSVs. `_validate_dataframes` checks **raw** headers against the client's `column_mappings`, and the rename to internal names happens later in `analysis._clean_and_prepare_data`. Default Shopify headers: `Name, Lineitem sku, Lineitem quantity, Lineitem name, Shipping Method, Shipping Country, Tags, Notes, Total, Subtotal, Shipping Name, Created at`.
- Tests: Fulfilment uses `.venv/bin/python -m pytest` with `QT_QPA_PLATFORM=offscreen` (a full run is about 10 min). The Packer uses `.venv/bin/pytest -q`. CI uses Python 3.11 and the VM 3.14. Coverage gates: Fulfilment 90%, Packer 85% app code.
- Existing helpers to reuse: `tests/replay/` (26 anonymised sessions, `anonymise.py`), `scripts/build_guide_server.py` (seeds a throwaway server folder), `core.analyse(...)`.

## 6. Decided by the owner

- Trial in separate fork repos only, not in the main copies.
- Anonymised data only, for now.
- Both apps move to Supabase together. The file server becomes the fallback.
- Fallback behaviour as in §7.3 (owner: "seems good").
- Fallback is a separate shared test share the owner sets up themselves; both apps are redirected to it.
- Order pulling: Shopify first, other platforms later. Read-only for this trial.
- The owner creates the Shopify Partner account and dev store with Claude's help.

## 7. Design

### 7.1 Repos

- `cognitiveghost/shopify-fulfillment-tool-db` and `cognitiveghost/packing-tool-db`, both private. They are created with `gh repo create` plus a push of current `main` with full history, because GitHub won't fork a repo into the account that owns it.
- Each has a remote `upstream` pointing at the production repo, so later fixes come in with `git merge upstream/main`.
- Default branch `main`. The fork's CI runs the same gates.
- Release and updater: `shared/updater.py` points at the fork's own GitHub releases, and the app name and window title get a visible "DB trial" suffix. Fork installs use a different install folder and QSettings org (`ShopifyTool-DB`, `PackingTool-DB`), so they never read or overwrite a production install's saved server path, language or worker.
- `shared/` stays canonical in the Fulfilment fork and syncs to the Packer fork, as today.
- This design doc is the first commit, at `docs/superpowers/specs/2026-11-13-supabase-fork-design.md`.

### 7.2 One storage seam, two backends

All server I/O goes through one module in `shared/`, `shared/store.py`. It is a small `Store` with two implementations, `FileStore` (today's behaviour) and `SupabaseStore`. Paths stay the vocabulary: callers keep saying "`Sessions/CLIENT_X/2026-10-09_1/session_info.json`", and the store decides where that lives. That keeps the change at each call site mechanical and the existing tests meaningful. Most `atomic_write_json`, `open()` and `json.load` calls on server paths turn into `store.read_json` / `store.write_json` / `store.read_bytes` / `store.write_bytes` / `store.list` / `store.lock`.

`SupabaseStore` maps paths to storage. The data that matters for the trial goes into real tables, not one blob table:

| Today (path) | Supabase |
|---|---|
| `Clients/CLIENT_X/client_config.json`, `shopify_config.json`, `packer_config.json` (incl. `sku_mapping`, inventory memory) | `clients` (id, name, archived, config jsonb, shopify_config jsonb, packer_config jsonb, updated_at) |
| `Clients/CLIENT_X/backups/*.json` | `client_config_history` (client_id, kind, body jsonb, saved_at), last 10 kept |
| `Clients/groups.json` | `groups` (body jsonb) |
| `Clients/CLIENT_X/fulfillment_history.csv` | `fulfillment_history` (client_id, order_number, row jsonb, ...): one row per CSV row |
| `Sessions/CLIENT_X/<session>/` + `session_info.json` | `sessions` (id, client_id, name, status, info jsonb, created_at, updated_at). The session claim is a unique `(client_id, name)` insert instead of `mkdir` |
| `session_index.json`, `registry_index.json` | gone: a query on `sessions` / `packing_lists` replaces both caches |
| `analysis/analysis_data.json`, `analysis_stats.json`, `memory_baseline.json`, `operations_history_<PC>.json` | `session_documents` (session_id, path, body jsonb, updated_at) |
| `packing_lists/<name>.json`, `withdrawn/` | `packing_lists` (session_id, name, body jsonb, withdrawn bool, fingerprint) |
| `packing/<list>/packing_state.json`, `session_summary.json` | `packing_state` (session_id, list_name, state jsonb, summary jsonb, updated_at, updated_by) |
| `.session.lock` | `list_locks` (session_id, list_name, holder_pc, holder_user, heartbeat_at). Acquire, heartbeat, release and take-over are Postgres functions (RPC), so the stale rule (120 s) is checked in one transaction |
| `Workers/workers.json` | `workers` (id, name, ...) |
| `Stats/global_stats.json` | `stats` (body jsonb) updated through an RPC that does the read-modify-write in one transaction |
| `input/*.csv`, `analysis/current_state.pkl`, `*.xlsx`, label PDFs, `stock_exports/*`, `Guides/*.pdf` | Supabase Storage, private bucket `files`, object key = the same relative path |
| `Logs/` | stay local (`~/.packingtool/logs` style fallback already exists). Not part of the trial |

Rules:
- Every table has RLS enabled, with policies that allow only the `authenticated` role. No `anon` access. The bucket is private.
- Writes that are read-modify-write today (stats, session_info, history append) use an RPC or `updated_at` compare-and-set, never client-side read-then-overwrite.
- A failed read raises. It never returns "empty".
- Schema lives as SQL migration files in the Fulfilment fork (`supabase/migrations/`), applied through the Supabase MCP or CLI. The Packer fork does not own schema.

*Builder's call:* the exact `Store` method list, how `FileStore` wraps today's helpers, and whether the pickle stays a pickle in Storage or becomes parquet.

### 7.3 Modes, fallback and offline

At startup each app picks one **mode** for the whole run:
1. **Supabase**: signed in and the project answers. Normal mode.
2. **File fallback**: Supabase unreachable at startup and the fallback folder reachable. Today's behaviour exactly, with a persistent banner "Database unreachable: working from file server". The fallback folder for the trial is a **shared test share that the owner sets up** (owner's decision). Both work PCs point at it, so locks and two-PC packing can be tested in fallback too. The fork only needs a way to point at it: `FULFILLMENT_SERVER_PATH` / the fork's own QSettings. It never points at the production share. A seeding script (`scripts/build_guide_server.py` style, from the anonymised fixtures) fills the share's initial contents and can reset it.
3. Neither reachable: today's recovery prompt.

The app never switches mode mid-run and never writes to both. Moving data between modes is a manual, explicit script (`scripts/sync_file_to_db.py`), not automatic.

Mid-run loss of Supabase:
- **Packer** keeps going in an **offline buffer**: a local folder (`%LOCALAPPDATA%/PackingTool-DB/buffer/`) with the same layout. `packing_state` writes go to the buffer plus an outbox. On reconnect the outbox is pushed in order. Offline is shown in the UI.
  - Lock conflict: if the PC was offline longer than the stale timeout and another PC took the list over, the push is refused. The buffered state is saved as `packing_state_conflict_<pc>_<time>` and the packer is told. Nothing is overwritten.
  - Starting a new list while offline is not allowed. Only the list already open continues, and it can be **finished** offline: the session summary, stats RPC, worker stats and session progress for that list are queued in the same outbox, in order.
  - The buffer and outbox are **durable on disk** and survive a crash or reboot. *(Claude's call.)* At startup, if the outbox is not empty, the Packer checks it before picking a mode. If Supabase answers, it pushes first. If not, it reopens only that buffered list in offline mode, and never falls into file-fallback mode with a buffer still waiting. File fallback is offered only when the outbox is empty.
- **Fulfilment** shows "offline" and blocks writes, as it does today when the share drops. No buffer.

Sign-in: each PC has its own Supabase Auth user (email + password, made by the owner in the dashboard). The app asks once and keeps the refresh token in the OS keyring. The publishable key and project URL ship in config. The service-role key is never in the app or the repos.

### 7.4 Order pulling (Fulfilment only)

- **Seam:** `shopify_tool/order_sources/` holds one function per platform, `fetch_orders(client_settings) -> DataFrame`. It returns a DataFrame **with the raw Shopify CSV export headers** listed in §5, so `_validate_dataframes`, the column mapping and `analyse` run unchanged. Shopify is the only source now. A future WooCommerce source returns the same headers.
- **Shopify:** Admin GraphQL API, `orders(query: "fulfillment_status:unfulfilled status:open")`, paginated. One output row per line item, the way the CSV export does it. API version pinned in code.
- **One store per client** *(Claude's call)*, as in real life where each client is its own Shopify merchant and sends its own CSV export. The trial uses one dev store per client being tested (ALMA first, HERBAR second). Dev stores are free. No tag or vendor filtering.
- **Where tokens live:** per client, under `shopify_config.order_source = {platform: "shopify", shop: "<x>.myshopify.com", token: "..."}` in `clients`. Trial only; dev store tokens. *Upgrade before any real store:* move tokens to Supabase Vault or an Edge Function.
- **UI:** a "Pull orders" action next to the CSV load on the session start flow. It shows how many orders and lines came in. The pulled DataFrame is also saved to `input/orders_export.csv` (in Storage) so a session stays replayable, like a CSV session.
- **No duplicates:** the pulled rows are the session's input, the same as one CSV export. Pulling again in the same session replaces the input, it does not append.
- **Read-only:** the custom app only gets the `read_orders` and `read_products` scopes.

### 7.5 Shopify development store (owner + Claude)

1. The owner creates a Shopify Partner account and one development store per trial client (ALMA, then HERBAR). Claude walks the owner through it.
2. In the store, create a custom app with scopes `read_orders`, `read_products` (trial) plus `write_orders`, `write_products` **only on a separate seeding app**, so the seeding script can create fake data. The trial app the fork uses stays read-only.
3. `scripts/seed_dev_store.py --client ALMA` (Fulfilment fork) creates products from that client's anonymised SKUs in `tests/replay/fixtures`, and a few hundred test orders with names and addresses from a fixed fake-name list in the script, using the shipping methods and countries seen in the fixtures. It also writes a matching stock CSV for those SKUs, which is uploaded as that client's stock for app sessions.

### 7.6 Loading the replay data

`scripts/load_fixtures_to_db.py` reads `tests/replay/fixtures` (already anonymised) and creates clients, sessions, inputs and configs in Supabase, so the trial starts with the 26 sessions in place. The fixtures have no stock export, so the loader builds each session's stock CSV from `memory_baseline.json` via `core.baseline_stock_df`, as `replay.py` does. *(Claude's call.)* Most fixtures are `config_exact: false`, so on the Windows PCs "end to end" means the session runs through analysis and packing. Exact reproduction of `expected/` is checked on the VM (§9), not by hand. It refuses to run on any folder outside `tests/replay/fixtures` or a seeded throwaway folder.

## 8. Order of work

1. Fork repos, updater/QSettings separation, design commit.
2. Supabase schema + RLS + RPCs (locks, stats). Auth users for 3 PCs.
3. `shared/store.py` with `FileStore` only, every call site moved onto it in both apps. All tests still green. **This step must change no behaviour.**
4. `SupabaseStore` + modes + sign-in. Fixture loader. The 26 replay sessions run against Supabase.
5. Packer offline buffer + conflict handling.
6. Shopify dev store + seeding script, then `order_sources/shopify` + "Pull orders".
7. Field trial: both work PCs at once and the home PC, checklist in §9.

## 9. Done means

- The full test suites and coverage gates pass in both forks, plus a test suite that runs the store contract against both `FileStore` and a Supabase test schema.
- On the VM: all 26 replay sessions reproduce `analysis_data.json` when read from Supabase.
- Windows trial checklist, run by the owner:
  - Both work PCs pack different lists of one session at the same time.
  - A second PC trying to open a list that is in use is refused.
  - Pulling the network cable mid-list on a Packer keeps it packing, and progress lands after reconnect.
  - The home PC does a whole session: pull orders from the dev store, analyse, packing list, pack.
  - With Supabase blocked, both work PCs start in file fallback on the shared test share and can pack different lists of one session.
- Nothing from `~/Desktop/Server./` or a real store is in Supabase. `scripts/check_no_real_data.py` checks that every Customer and Notes value in Supabase is either the anonymiser's masked form (see `tests/replay/anonymise.py`) or a name from `seed_dev_store.py`'s fake-name list. Anything else fails. It never reads `~/Desktop/Server./`.

## 10. Left to the builder

- Store API details, table column details and indexes. Use `supabase-py` vs plain PostgREST over `httpx`.
- How each call site is moved (one PR per area is fine).
- Exact UI text and placement of the mode banner, sign-in prompt and "Pull orders".
- The keyring library and how the PyInstaller build bundles it.
