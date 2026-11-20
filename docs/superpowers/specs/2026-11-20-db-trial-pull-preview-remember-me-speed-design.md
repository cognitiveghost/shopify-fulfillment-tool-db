# DB trial: Pull orders preview, Remember me, faster processing

Date: 2026-11-20. Size: three bounded parts in one task, built in this order: §1, §2, §3.
Repos: F = `shopify-fulfillment-tool-db` (worktree `.claude/worktrees/dr-57`), P = `packing-tool-db` (worktree
`.claude/worktrees/dr-57`), both on branch `dr/57-db-trial-pull-orders-preview-select-reme`.
Background: `docs/superpowers/specs/2026-11-13-supabase-fork-design.md` (the store seam, `SupabaseStore`).
No mockup exists. The UI here is two small dialogs and one menu item, built from existing components.

## Why

The owner tested both DB builds on 2026-11-20 and asked for three things:

1. Pull orders imports every open, unfulfilled order with no chance to look first or choose.
2. Sign-in should be the owner's choice per PC. Today the app always remembers it, and there is no way to sign out.
3. DB mode feels slow. Measure the slow paths, fix the worst ones, and report before/after numbers.

## Owner's decisions (2026-11-20)

| Question | Answer |
|---|---|
| Was the quick-test PC asking for sign-in on every start? | No. Remembering works; the owner wants the choice. |
| What does Sign out do? | Forget the saved sign-in and close the app. The next start asks for email and password. |
| Remember me ticked by default? | **Unticked.** |
| How far does the speed work go? | Cut round trips **and** move session open off the UI thread. |

## Goals

1. Pull orders shows the fetched orders in a preview, and imports only the ones the user ticks.
2. The sign-in dialog in both apps has a "Remember me" box, unticked by default. Ticked keeps the PC signed in
   across restarts. Unticked asks at every start. Sign out forgets the saved sign-in in both apps.
3. A benchmark script gives time and HTTP request counts for five actions. The PR carries before and after
   numbers. Opening a session no longer freezes the window.

## Non-goals

- Remembering which orders were left out last time. An unticked order is still open and unfulfilled, so it is
  offered again at the next pull.
- Per-user tokens on one Windows login. The saved sign-in stays one entry per app per Windows login, as today.
- Pre-filling the email field.
- Moving analysis, packing-list build or Packer list open onto threads. Analysis and the packing-list build already
  run on workers. Packer list open is changed only if the benchmark shows it is slow (§3.4).
- Any change to file mode behaviour. FileStore callers must behave exactly as today.

## Anti-goals

- Storing the password anywhere, or storing the refresh token outside the OS keyring. QSettings and the registry
  are plain text.
- Revoking the token for other PCs on sign out. `sign_out()` defaults to the global scope, which would sign out
  every PC using that user. Use `scope: "local"`, or skip the server call.
- Swapping the store's client in the middle of a run. The store is fixed for a run (spec 2026-11-13 §7.3), which is
  why Sign out closes the app.
- "Faster" by dropping a read the code needs for correctness, such as the ADR 0011 state stamp or the lock check.
- A speed claim without numbers from the benchmark.

---

## 1. Pull orders: preview and select (F only)

### Today

`FileHandler.pull_orders` (`gui/file_handler.py:135`) runs `_pull_into_session` (`:812`) on a `Worker`. That
function fetches the orders (`fetch_orders`, `shopify_tool/order_sources/__init__.py:29`), writes
`<session>/input/orders_export.csv` with `store.write_bytes`, and returns `(df, store.local_path(rel))`.
`_on_orders_pulled` (`:180`) then calls `load_file("orders", path)` and shows a toast. The DataFrame has one row
per line item, with the Shopify CSV headers in `SHOPIFY_CSV_HEADERS`. Removed and refunded lines are already
dropped (`shopify.py:165`, PR #13).

### New flow

1. **Fetch worker:** calls `fetch_orders(source)` only, and returns the DataFrame. Nothing is written yet.
2. **Empty result:** keep today's toast ("No open unfulfilled orders in {shop}"). No dialog opens.
3. **Preview dialog** on the UI thread: `PullPreviewDialog(parent, orders_df)` in a new `gui/pull_preview_dialog.py`.
   It returns the selected order names, or `None` on Cancel.
4. **Cancel:** nothing is written. The session's existing input is left as it is. No toast.
5. **Import:** a second `Worker` writes `orders_df[orders_df["Name"].isin(selected)]` to the same `rel` with the
   same `sep` as today, then returns `(selected_df, local_path)`. `_on_orders_pulled` then runs unchanged, and its
   toast counts the selected orders and lines.
6. The session-changed guard in `_on_orders_pulled` stays. Add the same guard after the dialog closes: if the
   session changed while the dialog was open, write nothing.
7. `_pull_worker` stays set from the fetch until the write finishes, so a second click is ignored for the whole
   flow, including while the dialog is open.

### The dialog

One row per order, built by grouping the line rows by `Name`. Rows keep the order they arrived in. Columns:

| Column | Source |
|---|---|
| (checkbox) | ticked by default |
| Order | `Name` |
| Date | `Created at`, date part only (`YYYY-MM-DD`) |
| Country | `Shipping Country` |
| Shipping | `Shipping Method` |
| Lines | number of line rows |
| Units | sum of `Lineitem quantity` |
| Tags | `Tags` |

Order-level columns repeat on every line row, so take the first row's value.

- **Filter:** reuse `FilterBar` (`shared/components/filterbar.py:25`). The caller does the filtering: a
  case-insensitive substring match over Order, Country, Shipping and Tags. Non-matching rows are hidden, not
  unticked.
- **Select all / Select none:** two buttons. They act on the visible rows only, so "filter by DHL, Select none"
  works.
- **Count line:** "{selected} of {total} orders selected".
- **Buttons:** the primary button says "Import {n} orders" and is disabled at 0. The other is Cancel. Use
  `apply_dialog_button_roles` as `SignInDialog` does.
- **Widget:** `QTableWidget` with a checkable item in column 0, read-only cells, sorting off, and
  `setUniformRowHeights`-style performance settings where `QTableWidget` supports them (CLAUDE.md). A few hundred
  orders is the expected size.
- Theme colours only. Every string goes through `_()`, with Bulgarian in the catalogue (ADR 0021).
- **Builder's call:** column widths, minimum size (about 760×480), and the window title (suggested: "Pull orders
  from {shop}").

Ticked by default is the builder-side call (marked here): clicking Import straight away does what Pull does today.

### Tests

- `tests/test_pull_preview_dialog.py`: grouping (lines and units per order, first-row values), filtering hides rows,
  Select none on a filtered view unticks only the visible rows, Import disabled at 0, `selected()` returns names.
- `tests/test_pull_orders.py`: extend the existing tests (they monkeypatch `gui.file_handler.fetch_orders`). Add a
  way to stub the dialog, such as a module-level hook or monkeypatching `PullPreviewDialog.ask`. Cases: Cancel
  writes nothing and leaves the earlier input; a subset writes only the subset's rows; empty shows the toast and
  no dialog; the session changing while the dialog is open writes nothing.

---

## 2. Remember me and Sign out (shared/ in F, then P)

### Today

`shared/db_auth.py` `connect(app_name, prompt)` always saves the refresh token in the keyring under service
`f"{app_name}-db"`, user `"refresh_token"`. It does this on sign-in (`:80`) and on every `TOKEN_REFRESHED`
(`:50-52`). At start it signs in silently from that token. `SignInDialog` (`shared/components/sign_in_dialog.py`)
has Email and Password fields and returns `(email, password)`. Nothing ever deletes the token. App names:
F `"ShopifyTool-DB"` (`gui/main_window_pyside.py:82`), P `"PackingTool-DB"` (`gui/main_window.py:1278`, plus the
profile manager at `:1094`). The service names `ShopifyTool-DB-db` and `PackingTool-DB-db` stay as they are, so
existing saved sign-ins keep working.

### Behaviour

- **Saved token = remembered.** No separate flag is stored. If the keyring holds a token at start, the app signs in
  silently, as today. That token only exists because someone ticked Remember me.
- **The dialog** gets a `QCheckBox` "Remember me on this PC", **unticked**, below Password. `values()` and `ask()`
  return `(email, password, remember)`.
- **On a successful password sign-in:**
  - remember ticked: save the token, as today;
  - remember unticked: delete any saved token (`keyring.delete_password`; ignore `PasswordDeleteError` and
    `KeyringError`).
- **Rotation (`TOKEN_REFRESHED`):** write to the keyring only while this run is remembering. A run is remembering
  when it signed in from a saved token, or the user ticked the box.
- **Run memory:** `db_auth` keeps the latest refresh token per service in a module-level dict, always, whatever
  the remember setting. `connect()` tries that in-memory token first, then the keyring. This matters for P: its
  offline sync worker reconnects later in the same run with `prompt=lambda _error: None`
  (`gui/main_window.py:1258-1260`). With Remember me off there is no keyring token, so without run memory the
  packer would be asked to sign in again after every outage. With no usable token the existing path is
  unchanged: the prompt returns None, `connect()` raises `SignInCancelled` (`db_auth.py:70-72`), and the
  worker emits `_sign_in_needed` (`:1263-1265`).
- **Tolerance:** `connect()` accepts a 2-tuple from `prompt` as `remember=False`, so P's existing test fakes keep
  working until they are updated.
- **`db_auth.forget(app_name)`:** a new function. It deletes the keyring entry and the in-memory token, ignoring
  "not found" and keyring errors.

### Sign out

- **Where:** the ⋯ overflow menu, section "THIS PC", directly under "Server connection…". In F it is built in
  `gui/ui_manager.py:390-394`. In P it is `gui/main_window.py:~621`. Show the item only when this run's mode is
  Supabase or offline-buffer (DB mode). In file mode there is nobody to sign out.
- **Action:** `ConfirmDialog` with the text "Sign out of the database on this PC? The app closes, and the next start
  asks for your email and password." and the buttons Sign out / Cancel. On confirm:
  1. call `db_auth.forget(app_name)`;
  2. optionally call `client.auth.sign_out({"scope": "local"})`, wrapped so that any error is only logged (never
     global scope, see Anti-goals);
  3. call `window.close()`, the normal close path, so P releases its list lock and both apps flush their write
     queues as they do on any close.
- **P:** if the outbox still has unsynced work, closing is already handled by the existing close path. Sign out
  adds no new data-loss path, because the outbox lives on disk and survives the restart.

### Tests (F)

`tests/shared/test_db_auth.py` already has an in-memory keyring fake. Add:
- unticked sign-in saves nothing and deletes a pre-existing token;
- ticked sign-in saves the token;
- `TOKEN_REFRESHED` writes only when remembering;
- `forget()` clears both the keyring and memory;
- a second `connect()` in the same run, with a prompt that returns None, reuses the in-memory token and never prompts.

Also: a dialog test (checkbox unticked by default; the 3-tuple), and an F menu test (the item is present in DB mode
and absent in file mode; confirming calls `forget` and `close`, with both stubbed).

### P

Run `python scripts/sync_shared.py <F worktree>` from the P worktree after F's shared commit is pushed. Then add
the Sign out menu item and its handler, with a P test of the same shape as F's menu test. Check P's existing tests
for fakes of `SignInDialog.ask` or `connect` that depend on the 2-tuple (`tests/test_offline_ui.py`), and update them.

---

## 3. Faster processing (measure, then fix)

### 3.1 Benchmark first

Create `scripts/bench_db.py` in F. It prints one Markdown table:

| action | store | wall ms | HTTP requests | est. hosted ms |

- **Stores:**
  - `supabase`: the local stack, reset and loaded per `docs/trial-checklist.md` "Reset" (`--fixtures
    tests/replay/fixtures`). The client is created with the service key, as `scripts/load_fixtures_to_db.py`
    does, from `SUPABASE_SERVICE_KEY`. Target `FULFILLMENT_DB_URL`. Check `os.environ` directly and refuse to run
    if it is unset or not a `127.0.0.1`/`localhost` URL: `shared/db_config.py` falls back to the hosted URL.
    Never run against the hosted project.
  - `file`: a `FileStore` over a temp folder made by `scripts/seed_fallback_share.py`. This is local disk, the
    floor, not a real SMB share. The PR says so.
- **Request count:** wrap `httpx.Client.send` for the duration of each action and count the calls. Every
  supabase-py sub-client goes through it. This is the honest metric, because local round trips take about 1 ms.
- **Estimated hosted time:** wall ms + requests × RTT. RTT is the median of 5 GETs to
  `https://ntzqarcksthplcdzivep.supabase.co/auth/v1/health` with the publishable key from `shared/db_config.py`,
  or `--rtt-ms N` to skip the network. Print the RTT used.
- **Actions** (client ALMA, one replay session named in the script):
  - **(c) analysis:** `core.run_full_analysis(...)`, the session-writing path the GUI's analysis worker uses
    (`gui/actions_handler.py:~206`; copy its arguments). Not `core.analyse`, which the replay tests use and which is
    compute only. Each benchmark run creates a **fresh** ALMA session (`session_manager.create_session("ALMA")`)
    with the replay session's `orders_export.csv` and `inventory.csv` as inputs, so the fixture sessions are never
    overwritten. Run this first, because (a) and (d) use the session it analysed. Before and after runs are
    comparable after a reset and load each time.
  - **(a) session open:** a `MainWindow` offscreen, with `open_profile_manager` monkeypatched to return a
    `ProfileManager(store=store)` (pattern: `tests/test_supabase_tag_undo.py:36-41`). Time
    `load_existing_session(path)`. After §3.2, also time the read phase on its own.
  - **(b) pull orders:** `fetch_shopify_orders(shop, token, transport=...)` (`shopify.py:56`; `fetch_orders` has
    no transport parameter) with an `httpx.MockTransport` serving
    `tests/order_sources/fixtures/orders_page_*.json` (the Shopify side is not ours to speed up), then the write
    and load steps. The request count covers store calls only, because MockTransport bypasses `Client.send`'s
    network. The builder verifies this and excludes Shopify calls if they are counted.
  - **(d) packing lists:** build that session's packing lists through `shopify_tool/report_jobs.py`, the way the
    GUI's generate-reports worker does.
  - **(e) Packer list open:** in P, `scripts/bench_open_list.py`, built the same way (same table format and
    counting). It opens one of the guide server's packing lists through the same calls P's open path makes
    (`gui/main_window.py:~1606`, `packing_tool/session_lock_manager.py:158-184`, `gui/workers.py:33-70`,
    `packing_tool/packer_logic.py:349-372`). It needs the default (guide server) load from the checklist.
- Commit the "before" table as `docs/perf/2026-11-db-trial-before.md` (F; P's line can be pasted in). Run it before
  changing any code.

### 3.2 Session open off the UI thread (owner's decision)

`MainWindow.load_existing_session` (`gui/main_window_pyside.py:1295`) runs on the UI thread and mixes store reads
with UI updates. It has about 20 synchronous callers in tests. Split it:

- **`read_session(store, session_manager, session_path, config) -> SessionLoad | None`**, a module-level function
  that touches no widgets. It returns a small dataclass with `session_info`, `stock_rel` and `stock_local_path`
  (or None), `analysis_df`, `analysis_stats`, `state_stamp`, `lot_table`, and whatever else the apply step needs.
  It carries the body of `_load_session_analysis` and the IO half of `_restore_session_inputs`, and reads
  `session_info` **once**. The undo history reload (`undo_manager.reload_session_history`) moves here too if it
  reads the store. The builder checks.
- **`_apply_session(session_path, loaded)`**, on the UI thread: everything `load_existing_session` does with
  widgets, attributes, toasts and `log_activity` today, in the same order and with the same messages. It calls
  `file_handler.validate_file("stock")` with the already-downloaded path.
- **`load_existing_session(path)`** stays **synchronous**: `_reset_session_state()`, then `read_session`, then
  `_apply_session`. The tests stay unchanged.
- **`open_session(path)`** is new and asynchronous. It does `_reset_session_state(wait=False)` and sets
  `session_path` and the bar state to SESSION. The window shows the session is opening: reuse the client-switch
  loading affordance if one fits, otherwise a status line. Then it runs a `Worker` that flushes the write queue
  (the work `_reset_session_state` would have done on the UI thread, as the client switch does, spec B2) and
  calls `read_session`. On result: if `self.session_path != path` (the user opened another session or switched
  client), discard it. Otherwise call `_apply_session`. On error: today's
  `show_error(_("The session wasn't loaded"), ...)`. Keep a strong reference to the worker, as
  `_client_load_workers` does (`:202`, `:827-831`).
- **The UI entry point** moves to `open_session`: `on_session_selected` (`:977-984`, reached from `:435` and
  `:467`). `scripts/render_guide.py:192` and other programmatic callers stay on the synchronous path.
- **Thread safety:** the worker shares the store with the UI thread, as the client switch, pull, analysis
  workers and write queue already do. There is no store lock, and none is added. Rule: between `open_session`
  and `_apply_session` the UI thread makes no store calls for the session being opened. `_reset_session_state`
  already cleared the frame, so there is nothing to autosave.
- **Test:** an offscreen test that `open_session` returns before the read finishes (a fake store whose `read_json`
  blocks on an event), that the result is applied after, and that a stale result is discarded.

### 3.3 Round-trip fixes (each verified by a benchmark delta)

These are candidates from reading the code. Each fix lands only with a test that pins the behaviour, and is kept
only if the benchmark's request count drops.

1. **Session open:** `session_info` is read twice (`main_window_pyside.py:1323` and `:1242`). Fixed by §3.2's
   single read.
2. **Session open:** `inventory.csv` is downloaded twice (`_restore_session_inputs` `local_path`, and
   `core.session_lot_table` at `core.py:988-1010`). Fix: `session_lot_table` reads from the local path already
   fetched, or `SupabaseStore.read_bytes` records the blob stamp, as `local_path` does, so the second access is a
   cache hit.
3. **`exists()` then `read_*()`** pairs (`_load_session_analysis`: pkl, stats, analysis_data, report). Replace
   them with a read that catches `StoreNotFound`, in the new `read_session` only. This halves the calls on that
   path. FileStore raises `StoreNotFound` on a missing file too: check `shared/store.py`.
4. **`SupabaseStore.exists` on a row route** fetches the whole body (`store_supabase.py:432-433`, through
   `_read_row`). Select only the key column. This is a contract-preserving change in `shared/`, covered by the
   existing store contract tests.
5. **Pull:** `_pull_into_session` uploads the CSV, then `local_path` downloads it again. Write the bytes to the
   cache path and record the stamp in `write_bytes` (blob route), so `local_path` is a hit. If the stamp cannot be
   known without a request, write the local copy directly in the pull write step.
6. **Analysis:** `write_queue.flush(timeout=30)` runs on the UI thread before the analysis worker starts
   (`gui/actions_handler.py:~183`). Move it into the worker. History is read in full twice
   (`core.py:838-845` and `fulfillment_history.py:128-160`): read it once and pass it down.
7. **Packing lists:** `update_packing_list_statistics` reads each list body in turn (`report_jobs.py:~290-302`).
   Fix it only if the benchmark shows this as a top cost. A batch read would be one `packing_lists` select per
   session.
8. **Packer list open (P):** an `exists` call and the lock RPC run on the UI thread. Move or remove them only if
   the benchmark shows more than 300 ms estimated hosted time on the UI thread. Otherwise leave them and record
   the number.

The builder may add a fix the benchmark reveals that is not on this list, under the same rule (test plus
benchmark delta). It may drop a candidate whose delta is nil, and says so in the PR.

### 3.4 After

Re-run both benchmark scripts and commit `docs/perf/2026-11-db-trial-after.md`. The F PR description carries both
tables. The P PR carries its line.

---

## Constraints

- `shared/` is edited in F only. P gets it through `scripts/sync_shared.py`. Never hand-edit P's `shared/`.
- Every user-facing string uses `_()` (`N_()` at module level) with Bulgarian in `locales/` (the catalogue guard
  tests in `tests/test_i18n_catalogue.py`).
- No UI calls from worker threads. Workers return data, and slots on the UI thread apply it.
- Theme tokens only, and no new colours.
- Gate F: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` (or `scripts/run_tests.sh` for the Supabase
  tests) plus `ruff check .`. Gate P: its own suite plus `python scripts/coverage_check.py`.
- The `@pytest.mark.supabase` tests wipe the local stack. Run the benchmark after a fresh reset and load, never
  interleaved with a test run.
- Python 3.11 floor.

## Left to the builder

Column widths and dialog size. The exact loading affordance for `open_session`. The `SessionLoad` field list. Which
of the §3.3 candidates survive the benchmark. The session used by the benchmark.
