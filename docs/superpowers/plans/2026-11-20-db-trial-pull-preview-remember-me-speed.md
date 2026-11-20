# DB trial: Pull preview, Remember me, faster processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull orders gets a preview and select step. Both apps get an opt-in "Remember me" and a Sign out. DB mode
gets measured, then faster: fewer round trips, and session open moves off the UI thread.

**Architecture:** The pull splits into a fetch worker, a preview dialog and a write worker. Remember me lives in
`shared/db_auth.py` and `shared/components/sign_in_dialog.py` (canonical in F, synced to P). Speed work starts
with two benchmark scripts. Session open splits into a widget-free `read_session` that runs on a worker and an
`_apply_session` that runs on the UI thread. Round-trip fixes go in `SupabaseStore` and the analysis path.

**Tech Stack:** Python 3.11+, PySide6, pandas, supabase-py v2 / httpx, keyring, the local Supabase stack.

**Spec:** `docs/superpowers/specs/2026-11-20-db-trial-pull-preview-remember-me-speed-design.md`. Read the section
your task names before starting.

**Repos:**
- F = `/home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-57`.
- P = `/home/gloopy/Desktop/Projects/packing-tool-db/.claude/worktrees/dr-57`.
- Both are on branch `dr/57-db-trial-pull-orders-preview-select-reme`.
- Every task runs in F unless it says P.

## Global Constraints

- `shared/` is edited in F only. P gets it with `.venv/bin/python scripts/sync_shared.py <F path>`, run from P
  after F's commit is pushed. Never hand-edit `P/shared/` (a hook blocks it).
- Every user-facing string goes through `_()` from `shared.i18n` (`N_()` at module level). Bulgarian goes in the
  catalogue:
  - strings in `shared/*.py` go in `shared/assets/locales/bg.json`;
  - app strings go in `locales/bg.json`.
  - `tests/test_i18n_catalogue.py` fails if one is missing.
- No UI calls from worker threads. A `Worker` (`gui/worker.py`) returns data, and its `result` slot runs on the UI
  thread. Keep a strong reference to every `Worker` until `finished`, as `_client_load_workers` does
  (`gui/main_window_pyside.py:202`, `:827-831`).
- Theme tokens only (`shared/theme.py`). No hard-coded colours.
- The refresh token goes in the OS keyring or in process memory, never QSettings or a file. The password is never
  stored.
- File mode (FileStore) behaviour must not change.
- The F gate is `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` plus `.venv/bin/ruff check .`. For
  `@pytest.mark.supabase` tests use `scripts/run_tests.sh`, which sets `SUPABASE_TEST_*` from `supabase status`.
  The P gate is `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` plus `.venv/bin/python scripts/coverage_check.py`.
- The Supabase tests **wipe the local stack**. Benchmarks need a fresh reset and load first
  (`docs/trial-checklist.md` "Reset"), and must never run interleaved with a test run.
- Git: `/usr/bin/git`, one git command per Bash call, and commit with `git commit -F <absolute path to message
  file>`. No `&&` chains with git.
- Each commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Write test files with Write/Edit. A hook blocks Bash text containing "pytest" other than the plain run form.
- Python 3.11 floor: no 3.12+ syntax.

## Review Focus

1. **A pulled order with no shipping address or no tags** (NaN in `Shipping Country`, `Tags`, `Shipping Method`).
   The dialog shows blanks, and the filter and grouping never crash. Pinned in Task 2
   (`test_summary_handles_missing_fields`).
2. **Cancel in the preview, then Pull again.** The second pull runs. `_pull_worker` is cleared on Cancel. Pinned in
   Task 3 (`test_cancel_then_pull_again`).
3. **No keyring backend on the PC** (`NoKeyringError` from get, set or delete). Sign-in with the box unticked,
   `forget()` and Sign out never raise. Pinned in Task 4 (`test_no_keyring_never_raises`).
4. **Two sessions clicked in quick succession.** Only the last one ends up open. The first result is discarded.
   Pinned in Task 9 (`test_open_session_discards_stale_result`).
5. **A store read fails during `open_session`.** The error dialog shows, and the window is not left in the
   "opening" state. Pinned in Task 9 (`test_open_session_error_clears_loading`).

---

## Part 0: Baseline numbers (before any code change)

### Task 1: `scripts/bench_db.py` (F) and the "before" table

**Files:**
- Create: `scripts/bench_db.py`, `tests/test_bench_db.py`, `docs/perf/2026-11-db-trial-before.md`

**Interfaces:**
- Produces:
  - `count_requests()`: a context manager yielding an object with `.n: int`. It patches `httpx.Client.send` to
    count calls while active.
  - `require_local(url: str | None) -> str`: raises `SystemExit` unless the host is `127.0.0.1` or `localhost`.
  - `est_hosted_ms(wall_ms: float, requests: int, rtt_ms: float) -> float`: `wall_ms + requests * rtt_ms`.
  - CLI: `.venv/bin/python scripts/bench_db.py --store supabase|file [--rtt-ms N] [--session NAME]`. It prints a
    Markdown table with the columns `action | store | wall ms | HTTP requests | est. hosted ms` and the RTT used.
- Task 8 and Task 12 consume the same CLI and table format.

Spec §3.1 has the details. Key points:
- **Supabase store:** `create_client(os.environ["FULFILLMENT_DB_URL"], os.environ["SUPABASE_SERVICE_KEY"])`, then
  `SupabaseStore(client, pc="bench", cache_dir=<tempdir>)`, as `scripts/load_fixtures_to_db.py` does. Read
  `FULFILLMENT_DB_URL` from `os.environ` through `require_local`, never from `shared/db_config.py`, which falls back
  to the hosted URL.
- **File store:** `FileStore(<tempdir>)` seeded by `scripts/seed_fallback_share.py <tempdir>`. In the table, label
  the store `file (local disk)`.
- **RTT:** without `--rtt-ms`, the median of 5 `httpx.get(f"{shared.db_config.SUPABASE_URL}/auth/v1/health",
  headers={"apikey": SUPABASE_PUBLISHABLE_KEY})`. Measure it outside `count_requests`.
- **Order and actions.** Client `ALMA`. The source session is the `--session` replay session (default: the first
  name under `tests/replay/fixtures/ALMA`).
  - **c, analysis.** `session_manager.create_session("ALMA")`. Copy the replay session's `orders_export.csv` and
    `inventory.csv` into its `input/` through the store. Time `core.run_full_analysis(...)` with the same
    arguments `gui/actions_handler.py:~206` passes (read that call).
  - **a, session open.** Assign `gui.main_window_pyside.open_profile_manager = lambda _p: (Mode.SUPABASE, manager)`
    (or `(None, manager)` for file) and `gui.session_browser_widget.SessionBrowserWidget.USE_ASYNC = False`. Build
    `MainWindow()` offscreen, select client ALMA as `tests/test_supabase_tag_undo.py:43-46` does, then time
    `win.load_existing_session(path)` on the analysed session.
  - **b, pull write.** Build the DataFrame outside the timer with
    `fetch_shopify_orders("x.myshopify.com", "t", transport=httpx.MockTransport(...))`, serving
    `tests/order_sources/fixtures/orders_page_1.json` and `_2.json` the way `tests/order_sources/test_shopify.py:29-54`
    does. Time only `store.write_bytes(rel, df.to_csv(index=False).encode())` followed by `store.local_path(rel)`,
    with `rel = <session>/input/orders_export.csv`.
  - **d, packing lists.** Build the analysed session's packing lists the way the GUI's generate-reports worker
    does (`gui/actions_handler.py:~392` and `shopify_tool/report_jobs.py`).
- Set `QT_QPA_PLATFORM=offscreen` at the top of the script before importing PySide6.

- [ ] **Step 1: Write `tests/test_bench_db.py`.** Import the script as a module with
  `importlib.util.spec_from_file_location`.

```python
def test_require_local_refuses_hosted():
    with pytest.raises(SystemExit):
        bench.require_local("https://ntzqarcksthplcdzivep.supabase.co")
    with pytest.raises(SystemExit):
        bench.require_local(None)
    assert bench.require_local("http://127.0.0.1:54321") == "http://127.0.0.1:54321"

def test_count_requests_counts_client_sends():
    t = httpx.MockTransport(lambda r: httpx.Response(200))
    with bench.count_requests() as c:
        with httpx.Client(transport=t) as cl:
            cl.get("http://x/a"); cl.get("http://x/b")
    assert c.n == 2

def test_est_hosted_ms():
    assert bench.est_hosted_ms(10.0, 4, 50.0) == 210.0
```

  Note: if `MockTransport` bypasses `Client.send`, patch `httpx.Client.send` anyway. `send` is the public entry
  that `get` calls, so the test pins that.
- [ ] **Step 2: Run it.** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_bench_db.py -q`.
  Expected: FAIL (no script).
- [ ] **Step 3: Write `scripts/bench_db.py`** to the interfaces above.
- [ ] **Step 4: Run it again.** Expected: PASS. Run `.venv/bin/ruff check scripts/bench_db.py tests/test_bench_db.py`.
- [ ] **Step 5: Reset and load the local stack** per `docs/trial-checklist.md` "Reset" (`supabase db reset`, then
  both loader runs with `FULFILLMENT_DB_URL` and `SUPABASE_SERVICE_KEY` from `supabase status`).
- [ ] **Step 6: Run the benchmarks.**
  `FULFILLMENT_DB_URL=… SUPABASE_SERVICE_KEY=… .venv/bin/python scripts/bench_db.py --store supabase`, then
  `--store file`. Expected: four rows each, and every Supabase row has more than 0 requests.
- [ ] **Step 7: Write `docs/perf/2026-11-db-trial-before.md`.** It holds both tables, the RTT, the session name,
  the commit SHA, and the note "file = local disk, a floor, not an SMB share".
- [ ] **Step 8: Commit** (`perf: DB benchmark script and baseline numbers`).

> **Order:** run Task 8 (the P benchmark, Part 3) right after Task 1, before any P code changes. Then Tasks 2-7, 9-12.

---

## Part 1: Pull orders preview (spec §1)

### Task 2: `PullPreviewDialog` and `summarise_orders`

**Files:**
- Create: `gui/pull_preview_dialog.py`, `tests/test_pull_preview_dialog.py`
- Modify: `locales/bg.json`

**Interfaces:**
- Produces:
  - `summarise_orders(orders_df: pd.DataFrame) -> pd.DataFrame`, with the columns `Order, Date, Country, Shipping,
    Lines, Units, Tags`. There is one row per `Name`, in first-appearance order. Order-level values come from the
    first line row. `Date` is the first 10 characters of `Created at`. `Units` is the int sum of
    `Lineitem quantity`. NaN becomes `""`.
  - `PullPreviewDialog(parent, orders_df: pd.DataFrame, shop: str)`, with:
    - `.selected() -> list[str]`: the ticked order names, in row order;
    - `.set_filter(text: str)`: the same thing `FilterBar` emitting does;
    - `.select_all()` and `.select_none()`: visible rows only;
    - `.import_button: QPushButton`;
    - `.count_label: QLabel`.
  - `PullPreviewDialog.ask(parent, orders_df, shop) -> list[str] | None` (classmethod): `None` on Cancel.

UI per spec §1 "The dialog":
- `QTableWidget`, with column 0 as a checkable item, ticked by default. Cells are read-only and sorting is off.
- `FilterBar` from `shared/components/filterbar.py` sits above the table. Read its signal name there.
- Select all / Select none buttons. The count label reads `_("{selected} of {total} orders selected")`.
- The import button reads `_("Import {n} orders")` and is disabled at 0. Cancel sits beside it.
- `apply_dialog_button_roles` as in `shared/components/sign_in_dialog.py`.
- The title is `_("Pull orders from {shop}")`. The minimum size is about 760×480.
- The filter is a case-insensitive substring match over Order, Country, Shipping and Tags. It hides rows and does
  not untick them.

- [ ] **Step 1: Write the tests.** Build a frame with these line rows:
  - `#1001`: two lines (qty 1 and 2), country `BG`, method `DHL`, tags `vip`;
  - `#1002`: one line (qty 3), `Shipping Country` NaN, `Tags` NaN, method `Econt`;
  - `#1003`: one line (qty 1), `DE`, `DHL`, tags `""`.

```python
def test_summary_groups_lines():
    s = summarise_orders(FRAME)
    assert list(s["Order"]) == ["#1001", "#1002", "#1003"]
    assert list(s["Lines"]) == [2, 1, 1] and list(s["Units"]) == [3, 3, 1]
    assert s.loc[0, "Date"] == "2026-11-18"

def test_summary_handles_missing_fields():
    s = summarise_orders(FRAME)
    assert s.loc[1, "Country"] == "" and s.loc[1, "Tags"] == ""

def test_all_ticked_by_default(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    assert d.selected() == ["#1001", "#1002", "#1003"]
    assert d.import_button.isEnabled()

def test_filter_hides_and_select_none_acts_on_visible(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    d.set_filter("dhl")
    d.select_none()
    d.set_filter("")
    assert d.selected() == ["#1002"]

def test_filter_matches_tags_case_insensitive(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    d.select_none(); d.set_filter("VIP"); d.select_all(); d.set_filter("")
    assert d.selected() == ["#1001"]

def test_import_disabled_at_zero(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    d.select_none()
    assert not d.import_button.isEnabled()
    assert "0 of 3" in d.count_label.text()
```

- [ ] **Step 2: Run the tests.** Expected: FAIL (no module).
- [ ] **Step 3: Implement** `gui/pull_preview_dialog.py` and add the Bulgarian for every new string to
  `locales/bg.json`.
- [ ] **Step 4: Run the tests and `tests/test_i18n_catalogue.py`.** Expected: PASS.
- [ ] **Step 5: Render check.** Render the dialog offscreen to a PNG
  (`widget.grab().save(...)` in `$CLAUDE_JOB_DIR/tmp` or a temp dir) in the light and dark themes, and look at it.
  Do not commit the PNG.
- [ ] **Step 6: Commit** (`feat: pull orders preview dialog`).

### Task 3: Pull flow: fetch, preview, write

**Files:**
- Modify: `gui/file_handler.py:135-198` (`pull_orders`, `_on_pull_failed`, `_on_orders_pulled`) and `:812-818`
  (`_pull_into_session`)
- Test: `tests/test_pull_orders.py`

**Interfaces:**
- Consumes: `PullPreviewDialog.ask(parent, orders_df, shop) -> list[str] | None` (Task 2).
- Produces: `_write_pulled(store, rel: str, orders_df: pd.DataFrame, sep: str) -> tuple[pd.DataFrame, str]`, a
  module-level worker body that writes the CSV and returns `(orders_df, str(store.local_path(rel)))`. It replaces
  `_pull_into_session`, which is deleted.

Flow (spec §1 "New flow"):
1. The fetch `Worker(fetch_orders, source)` result goes to a new slot `_on_orders_fetched(df, shop, session_path,
   rel, sep)`.
2. In that slot:
   - empty: today's toast `_("No open unfulfilled orders in {shop}", shop=shop)` and clear `_pull_worker`;
   - otherwise: `PullPreviewDialog.ask(self.mw, df, shop)`;
   - `None`: clear `_pull_worker` and stop;
   - `self.mw.session_path != session_path` after the dialog: log at info, clear `_pull_worker` and stop;
   - otherwise: start `Worker(_write_pulled, store, rel, df[df["Name"].isin(selected)], sep)`. Its result goes to
     the existing `_on_orders_pulled`, which still checks the session and then calls `load_file`.
3. `_pull_worker` holds whichever worker is in flight. It is cleared at every exit (the write worker's `finished`,
   empty, cancel, session changed, error), so a second click is ignored from the fetch until the write finishes.
4. `_on_orders_pulled` no longer needs its `path is None` branch.

- [ ] **Step 1: Write the tests.** Extend `tests/test_pull_orders.py`, which already monkeypatches
  `gui.file_handler.fetch_orders`. Stub the dialog with
  `monkeypatch.setattr(file_handler_module.PullPreviewDialog, "ask", classmethod(lambda cls, p, df, shop: ...))`.
  Tests:
  - `test_subset_writes_only_selected`: the stub returns `["#1001"]`. The written `input/orders_export.csv` holds
    only `#1001`'s rows, and the toast counts 1 order.
  - `test_cancel_writes_nothing`: write an earlier input first. The stub returns `None`. The file's bytes are
    unchanged and `fh._pull_worker is None`.
  - `test_cancel_then_pull_again`: cancel, then a second `pull_orders()` reaches the dialog stub again (count the
    calls == 2).
  - `test_empty_shows_toast_no_dialog`: the stub asserts it is never called.
  - `test_session_changed_during_dialog_writes_nothing`: the stub sets `mw.session_path` to another path and
    returns all names. No file is written in either session.
  - Wait for workers the way the existing tests in that file do.
  - Update existing tests that assumed a direct write (for example `test_pull_writes_input_and_runs_analysis`) to
    stub the dialog as "all".
- [ ] **Step 2: Run the tests.** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_pull_orders.py -q`.
  Expected: the new tests FAIL.
- [ ] **Step 3: Implement** per the flow above.
- [ ] **Step 4: Run the file again, plus `tests/order_sources/`.** Expected: PASS. Run ruff.
- [ ] **Step 5: Commit** (`feat: pull orders previews and imports only selected orders`).

---

## Part 2: Remember me and Sign out (spec §2)

### Task 4: `db_auth`: opt-in remember, run memory, `forget()`

**Files:**
- Modify: `shared/db_auth.py`
- Test: `tests/shared/test_db_auth.py`, which has the `MemKeyring` and `kr` fixtures at `:13-35`

**Interfaces:**
- Consumes: `prompt(error) -> (email, password) | (email, password, remember) | None`. A 2-tuple means
  `remember=False`.
- Produces:
  - `connect(app_name, prompt, factory=_make_client)`: same signature and return as today.
  - `forget(app_name: str) -> None`: deletes the keyring entry `(f"{app_name}-db", "refresh_token")` and the run
    token. Never raises.
  - `_run_tokens: dict[str, str]`: module-level, keyed by service.

Rules (spec §2 "Behaviour"):
- The token sources are `_run_tokens.get(service)` first, then the keyring. Signing in from either one makes the
  run **remembering** only if the token came from the keyring.
- A password sign-in with remember ticked saves to the keyring and marks the run remembering. Unticked deletes any
  keyring token.
- Every new session (sign-in, refresh, `TOKEN_REFRESHED`) updates `_run_tokens[service]`. Only a remembering run
  writes the keyring.
- Every keyring call is wrapped. `KeyringError` (which includes `NoKeyringError`) and `PasswordDeleteError` are
  logged at warning, never raised.

- [ ] **Step 1: Write the tests.** Add an `autouse` fixture that clears `db_auth._run_tokens`. Reuse the existing
  fake-client pattern in this file for sign-in and refresh. Tests:
  - `test_unticked_sign_in_saves_nothing_and_clears_old`: `kr.d` starts with a token that the fake refuses, and
    the prompt returns `(e, p, False)`. After connect, `("ShopifyTool-DB-db", "refresh_token") not in kr.d`.
  - `test_ticked_sign_in_saves_token`: the prompt returns `(e, p, True)`, and the token is in `kr.d`.
  - `test_two_tuple_prompt_means_not_remembered`.
  - `test_refresh_event_writes_keyring_only_when_remembering`: fire `TOKEN_REFRESHED` the way
    `test_background_refresh_saves_the_rotated_token` (`:73`) does, once for an unticked run (keyring untouched)
    and once for a run started from a saved token (keyring updated).
  - `test_second_connect_reuses_run_token`: an unticked sign-in, then `connect(app, lambda e: None)` with a fresh
    fake. It refreshes with the run token and the prompt is never called.
  - `test_forget_clears_keyring_and_run_token`.
  - `test_no_keyring_never_raises`: a backend whose get, set and delete raise `keyring.errors.NoKeyringError`.
    Unticked sign-in, ticked sign-in and `forget()` all return normally.
- [ ] **Step 2: Run the tests.** Expected: the new tests FAIL.
- [ ] **Step 3: Implement** in `shared/db_auth.py`.
- [ ] **Step 4: Run `tests/shared/test_db_auth.py` and `tests/test_store_startup.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (`feat(shared): opt-in remember me, run token, forget()`).

### Task 5: Sign-in dialog checkbox

**Files:**
- Modify: `shared/components/sign_in_dialog.py`, `shared/assets/locales/bg.json`
- Test: the existing sign-in dialog test file (find it with `rg -l SignInDialog tests`), or create
  `tests/shared/test_sign_in_dialog.py`

**Interfaces:**
- Produces:
  - `SignInDialog.remember_box: QCheckBox`, labelled `_("Remember me on this PC")`, **unticked**, below Password
    in the form;
  - `values() -> tuple[str, str, bool]`;
  - `ask(...) -> tuple[str, str, bool] | None`.

- [ ] **Step 1: Write the tests.**
  - `test_remember_unticked_by_default`.
  - `test_values_include_remember`: fill both fields and tick the box, so `values() == ("a@b.c", "pw", True)`.
  - Update any existing test that unpacks two values.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Add the Bulgarian to `shared/assets/locales/bg.json`.
- [ ] **Step 4: Run the dialog tests, `tests/test_i18n_catalogue.py` and `tests/shared/test_db_auth.py`.**
  Expected: PASS.
- [ ] **Step 5: Commit** (`feat(shared): remember me checkbox on sign-in`).

### Task 6: Sign out in F

**Files:**
- Modify: `gui/ui_manager.py:388-394` (overflow, section "THIS PC"), `locales/bg.json`
- Test: `tests/test_sign_out.py` (create)

**Interfaces:**
- Consumes: `db_auth.forget(app_name)` (Task 4) and `self.mw.store_mode`
  (`gui/main_window_pyside.py:214`, set at `:299`).
- Produces: `UIManager._sign_out() -> None`.

Behaviour:
- Add `menu.add_item(_("Sign out"), self._sign_out)` directly after "Server connection…", only when
  `self.mw.store_mode is Mode.SUPABASE`. Check whether the overflow is built before or after `store_mode` is set.
  If it is built before, set its visibility once the mode is known.
- `_sign_out` opens `ConfirmDialog(self.mw, title=_("Sign out of the database on this PC?"), body=_("The app closes,
  and the next start asks for your email and password."), verb=_("Sign out"))`.
- If `exec()` is accepted: `db_auth.forget("ShopifyTool-DB")`, then `self.mw.close()`.
- No server-side `sign_out()` call. This is a decision: the token is deleted locally, and a global sign-out would
  sign out other PCs (spec Anti-goals).

- [ ] **Step 1: Write the tests.** Build `MainWindow` the way `tests/test_supabase_tag_undo.py` does, but with a
  FileStore-backed manager and `store_mode` forced, or with whatever lighter fixture `tests/test_shell.py` uses.
  - `test_sign_out_item_only_in_db_mode`: the action text "Sign out" is in the overflow's actions when
    `store_mode is Mode.SUPABASE`, and absent when it is `Mode.FILE` or `None`.
  - `test_sign_out_confirm_forgets_and_closes`: monkeypatch `ConfirmDialog.exec` to return `True`, and
    `db_auth.forget` and `mw.close` to record calls. Both are called, with `"ShopifyTool-DB"`.
  - `test_sign_out_cancel_does_nothing`: `exec` returns `False`, so neither is called.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Add the Bulgarian to `locales/bg.json`.
- [ ] **Step 4: Run the tests, `tests/test_i18n_catalogue.py` and `tests/test_shell.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (`feat: sign out of the database from the overflow menu`), then push F's branch:
  `/usr/bin/git push origin dr/57-db-trial-pull-orders-preview-select-reme`.

### Task 7 (P): Sync shared, Sign out in P

**Files (P):**
- Modify: `shared/` (by `scripts/sync_shared.py` only), `scripts/shared_synced_from.txt` (written by the script),
  `gui/main_window.py:616-622` (`_init_overflow`), `locales/bg.json`, `tests/test_offline_ui.py`
- Test: `tests/test_sign_out.py` (create, in P)

**Interfaces:**
- Consumes: `db_auth.forget`, `SignInDialog.values()` returning a 3-tuple (Tasks 4 and 5, via sync).
- Produces: `MainWindow._sign_out() -> None`.

Behaviour:
- The menu item comes right after "Server connection…". It shows only in DB mode. In P that means
  `self._run_store is not None` (set at `gui/main_window.py:~1083-1115`). Check that `_init_overflow` runs after
  the store is chosen; if not, set visibility once it is.
- Same ConfirmDialog copy as F (Task 6). Put the strings in P's `locales/bg.json`.
- On confirm: `db_auth.forget("PackingTool-DB")`, then `self.close()`. `closeEvent` (`:1475`) already releases the
  lock and handles the outbox.

- [ ] **Step 1: Sync.** From P, run `.venv/bin/python scripts/sync_shared.py
  /home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-57`. Expected: it reports the
  copied files, and `scripts/shared_synced_from.txt` holds F's pushed HEAD SHA.
- [ ] **Step 2: Run the P suite.** Fix any fake in `tests/test_offline_ui.py` (`:87-90`, `:190-202`) or elsewhere
  that depends on the 2-tuple. Expected: green before any feature code.
- [ ] **Step 3: Write `tests/test_sign_out.py` (P)** with the same three tests as Task 6, using P's existing
  MainWindow fixture (see `tests/test_offline_ui.py` for how it builds a window in DB mode).
- [ ] **Step 4: Run it.** Expected: FAIL. Implement. Run it again. Expected: PASS.
- [ ] **Step 5: Run the P gate.** `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` and
  `.venv/bin/python scripts/coverage_check.py`. Expected: green.
- [ ] **Step 6: Commit (P)** (`feat: remember me (shared sync) and sign out`). Push P's branch.

---

## Part 3: Faster processing (spec §3)

### Task 8 (P): `scripts/bench_open_list.py` and P's "before" line

Run this right after Task 1, before Task 7 touches P.

**Files (P):**
- Create: `scripts/bench_open_list.py`, `docs/perf/2026-11-db-trial-before.md`

**Interfaces:**
- Consumes: nothing from F code. Copy `count_requests`, `require_local` and `est_hosted_ms` from F's
  `scripts/bench_db.py`. P cannot import F.
- Produces: the same table format, with one action, `e, Packer list open`.

Approach:
- Target the local stack after the default (guide server) load from `docs/trial-checklist.md`.
- Build `SupabaseStore` with the service key (as in Task 1), wrapped the way P's startup wraps it (`BufferedStore`,
  `gui/main_window.py:~1103`).
- Time the calls P makes to open one guide-server packing list, in order:
  1. the `exists` check at `gui/main_window.py:~1606`;
  2. `SessionLockManager.acquire_lock` (`packing_tool/session_lock_manager.py:158-184`);
  3. the worker reads in `gui/workers.py:33-70` and `packing_tool/packer_logic.py:349-372`.
- Release the lock at the end.
- Also report which part of the requests ran on the UI thread in the real app: steps 1 and 2.

- [ ] **Step 1:** Write the script and run it. Expected: one row, with more than 0 requests, plus a UI-thread
  sub-row.
- [ ] **Step 2:** Write P's `docs/perf/2026-11-db-trial-before.md` and commit (P) (`perf: Packer list-open
  benchmark and baseline`).

### Task 9: Session open off the UI thread

**Files:**
- Create: `gui/session_open.py`, `tests/test_session_open_async.py`
- Modify: `gui/main_window_pyside.py:977-984` (`on_session_selected`), `:1136-1257` (`_load_session_analysis`,
  `_restore_session_inputs`), `:1295-1371` (`load_existing_session`)

**Interfaces:**
- Produces:
  - In `gui/session_open.py`, with no widgets and no `MainWindow` import:

```python
@dataclass
class SessionLoad:
    session_info: dict | None
    stock_path: str | None          # store.local_path of input/<stock_file>, None if absent
    analysis_df: pd.DataFrame | None
    analysis_stats: dict | None
    state_stamp: object             # session_state.state_stamp(...) taken BEFORE the analysis read (ADR 0011)
    lot_table: object | None        # core.session_lot_table(...), only when analysis_df is not None

def read_session(store, session_manager, session_path: str, config: dict | None) -> SessionLoad: ...
```

  - `MainWindow._apply_session(session_path: str, loaded: SessionLoad) -> None`: UI thread only.
  - `MainWindow.open_session(session_path: str) -> None`: asynchronous.
  - `MainWindow.load_existing_session(session_path)`: stays synchronous, and is now
    `_reset_session_state(); loaded = read_session(...); _apply_session(path, loaded)`, inside today's
    try/except and `show_error`.
- Task 11 relies on `read_session` reading `session_info` exactly once.

Rules (spec §3.2):
- **`read_session`** carries:
  - the body of `_load_session_analysis`: priority pkl → stats json (or `recalculate_statistics`) → the
    `analysis_data.json`-gated xlsx;
  - the IO half of `_restore_session_inputs`: the `stock_file` name from `session_info`, `local_path`, and the
    existence check;
  - one `session_manager.get_session_info(session_path)` call;
  - the `core.session_lot_table` call.
  It raises on store errors. It keeps today's "failed pickle, fall back" behaviour.
  `inventory.csv` must be downloaded once (spec §3.3 item 2): if `core.session_lot_table` (`core.py:988-1010`)
  re-reads it, pass it the already-fetched `stock_path` (optional keyword), or rely on Task 10's blob reuse; pin
  with a count in `test_read_session_reads_info_once`'s sibling `test_read_session_downloads_stock_once`.
  Replace each `exists()`-then-read pair with a read that catches `StoreNotFound` (spec §3.3 item 3). `FileStore`
  raises it too (`shared/store.py:254`, `:291`).
- **`_apply_session`** does, in today's order and with today's messages:
  - set `_state_stamp`, `_list_fingerprints` (`report_jobs.recorded_fingerprints(info)`) and `stock_file_path`;
  - `file_handler.validate_file("stock")` and `check_files_ready()`;
  - set `analysis_results_df = with_stock_left(df)`, `analysis_stats` and `lot_table`;
  - `_update_all_views()`, `show_results_when_painted()`, the toasts, `log_activity` and `update_ui_state()`;
  - the no-analysis branch (Setup tab plus the info toast);
  - `command_bar.set_state(BarState.SESSION)`, `update_session_chips()`, `undo_manager.reload_session_history()`
    and `update_session_info_label()`.
  If `reload_session_history` reads the store, move that read into `read_session` (as a new `SessionLoad` field)
  and leave only the apply step here.
- **`open_session(path)`:**
  1. `_reset_session_state(wait=False)`, set `self.session_path = path`, and show the loading state. Reuse the
     client-switch affordance (`command_bar.set_client_loading`) if it fits, otherwise disable the session picker
     and show a status line.
  2. Start a `Worker` whose body runs `self.write_queue.flush(timeout=10)` (log a warning on timeout, as
     `_reset_session_state` does), then `read_session(...)`.
  3. On result: if `self.session_path != path`, discard it and log at debug. Otherwise clear the loading state and
     call `_apply_session`.
  4. On error: clear the loading state, `logger.error` the details, and show
     `show_error(self, _("The session wasn't loaded"), _("Details are in Logs."))`. Clear `session_path`.
  5. Keep the worker in `self._client_load_workers` until `finished`.
- `on_session_selected` calls `open_session`. `scripts/render_guide.py:192` and the tests keep using
  `load_existing_session`.
- Thread rule: no UI-thread store calls for the session between `open_session` and `_apply_session`.

- [ ] **Step 1: Write `tests/test_session_open_async.py`.** Build a window over a FileStore session that has an
  analysis (reuse a fixture from `tests/test_session_restore.py`). Wait with `qtbot.waitUntil` or the
  `processEvents` loop the existing tests use. Tests:
  - `test_open_session_returns_before_read`: monkeypatch `gui.session_open.read_session` with a wrapper that waits
    on a `threading.Event`. `open_session(path)` returns while `win.analysis_results_df is None`. Set the event,
    wait, and the frame is loaded.
  - `test_open_session_discards_stale_result`: the first call's read blocks. Call `open_session(B)` (not blocked),
    release A. The window shows B's frame, and `session_path == B`.
  - `test_open_session_error_clears_loading`: `read_session` raises `StoreError`. `show_error` is called once (use
    a monkeypatched recorder), the loading state is cleared, and `session_path is None`.
  - `test_read_session_reads_info_once`: a counting wrapper around `session_manager.get_session_info`. Call
    `read_session(...)` directly. Expected count == 1.
  - `test_load_existing_session_still_synchronous`: after the call returns, `analysis_results_df` is set.
- [ ] **Step 2: Run the tests.** Expected: FAIL (no module).
- [ ] **Step 3: Implement** `gui/session_open.py` and the `MainWindow` changes.
- [ ] **Step 4: Run the new tests and every test that calls `load_existing_session`.** That is
  `rg -l load_existing_session tests` (about 9 files, including `tests/test_supabase_tag_undo.py` through
  `scripts/run_tests.sh`). Expected: PASS.
- [ ] **Step 5: Run the full F gate.** Expected: green, coverage ≥ 90%.
- [ ] **Step 6: Commit** (`perf: open sessions on a worker; read session info once`).

### Task 10: `SupabaseStore` round trips (shared)

**Files:**
- Modify: `shared/store_supabase.py`: `exists` at `:411-433`, `read_bytes` at `:259-266`, `write_bytes` (blob
  route), `local_path` at `:708-731`
- Test: `tests/shared/test_store_contract.py`, plus the Supabase store tests under `tests/shared/` (find them with
  `rg -l SupabaseStore tests/shared`)

**Interfaces:**
- Produces: no signature changes. The contract is unchanged, with fewer requests.

Changes (spec §3.3 items 2, 4, 5):
1. **`exists` on a row route** selects only the key columns, not the body, instead of `_read_row`. It is true iff
   the row exists and its body is not null. Today's rule is `cur[0] is not None`. Keep it: select the body's
   null-ness, for example `select("path")` plus `.not_.is_("body", "null")`, or whatever the row's table needs.
   Read `_row_for`.
2. **`read_bytes` (blob)** writes the downloaded bytes to `cache_dir / rel` and records `self._fetched[rel]`, the
   same stamp `local_path` uses, so a later `local_path(rel)` makes one stamp request and no download. If getting
   the stamp costs an extra request on every `read_bytes`, do not do this. Instead record the stamp only when it
   comes free (see 3), and say so in the commit.
3. **`write_bytes` (blob)** writes the same bytes to `cache_dir / rel` and records the stamp from the upload
   response if it carries one (`_blob_stamp` shows the fields). Otherwise record nothing and leave `local_path`
   as is.

- [ ] **Step 1: Write the tests** (Supabase-marked, in the existing SupabaseStore test file). Use a request counter:
  copy `count_requests` from `scripts/bench_db.py`, or patch `httpx.Client.send`.
  - `test_exists_row_does_not_fetch_body`: write a large JSON row. `exists` is still `True`. A missing row gives
    `False`. A row whose body is null gives `False`.
  - `test_local_path_after_write_bytes_skips_download`: `write_bytes(rel, b)`, then `local_path(rel)`. The file's
    bytes == `b`, and the download count is 0. Count calls whose URL contains `/object/` with method GET and not
    `info`. Adjust to the storage URLs the client uses.
  - `test_local_path_after_read_bytes_skips_download` (only if change 2 is kept).
- [ ] **Step 2: Run the tests with `scripts/run_tests.sh tests/shared`.** Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `tests/shared/` (with Supabase) and `tests/shared/test_store_contract.py`.** Expected: PASS.
- [ ] **Step 5: Commit** (`perf(shared): fewer SupabaseStore round trips on exists and blob reuse`).

### Task 11: Analysis path

**Files:**
- Modify: `gui/actions_handler.py:~175-223` (the `flush(timeout=30)` at `:180`, then the worker start),
  `shopify_tool/core.py:~838-845`, `shopify_tool/fulfillment_history.py:~128-160`
- Test: `tests/test_analysis_flush.py` (create), plus the existing history tests (`rg -l fulfillment_history tests`)

**Interfaces:**
- Consumes: nothing new.
- Produces: a history loader that the analysis path calls once per run. Read `core.py:838-845` and
  `fulfillment_history.py:128-160` to see both reads. Pass the frame from the first read to the second consumer
  instead of re-reading. Keep the public signatures by adding an optional keyword (for example
  `history_df: pd.DataFrame | None = None`) where the second read happens.

Changes (spec §3.3 item 6):
1. Move `write_queue.flush(timeout=30)` and its failure handling from the UI thread into the start of the analysis
   worker's body. The "not flushed" branch must still stop the analysis. Today that branch shows an error on the
   UI thread. In the worker, raise a dedicated exception and map it to the same `show_error` text in the error
   slot. Read `:175-223` first.
2. Read the history once per analysis run.

- [ ] **Step 1: Write the tests.**
  - `test_flush_runs_off_ui_thread`: monkeypatch `write_queue.flush` to record `threading.current_thread()`.
    Start an analysis on a FileStore session (pattern: the existing run-analysis tests,
    `rg -l run_analysis tests`). The recorded thread is not the main thread.
  - `test_flush_timeout_still_stops_analysis`: flush returns `False`. `run_full_analysis` is never called, and the
    same error text is shown as before.
  - `test_history_read_once`: count `store.read_bytes` (or the history loader) calls for the history path during
    one `run_full_analysis`. Expected == 1.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the new tests, the history tests and the replay tests (`tests/replay/`).** Expected: PASS, with
  the replay outputs unchanged.
- [ ] **Step 5: Commit** (`perf: analysis flush on the worker; history read once`).

### Task 12: Re-measure, conditional fixes, "after" tables

**Files:**
- Create: F `docs/perf/2026-11-db-trial-after.md`, P `docs/perf/2026-11-db-trial-after.md`
- Maybe modify: `shopify_tool/report_jobs.py:~290-302` (condition A), P `gui/main_window.py:~1606` (condition B)

Steps:
- [ ] **Step 1: Re-run both benchmarks.** Reset and load first, then the same commands as Tasks 1 and 8. Push P's
  sync first, so P measures the new `shared/`.
- [ ] **Step 2: Condition A.** If row (d) still makes more requests than `2 + 2 × (number of packing lists)`,
  replace the per-list body reads in `update_packing_list_statistics` with one read of all of the session's
  `packing_lists` rows. Add the method to `SupabaseStore` only if no existing query fits:
  `packing_overview` (`store_supabase.py:463-507`) may already. Keep a FileStore path that behaves as today. Add a
  test that the list statistics are identical over both stores. Commit (`perf: one read for packing list stats`).
  Otherwise record "not needed" with the numbers.
- [ ] **Step 3: Condition B (P).** If the UI-thread sub-row of (e) is over 300 ms estimated hosted time, move the
  `exists` check into the existing open worker (`gui/workers.py`). Pin it with a P test that the main thread makes
  no store call during open. Commit (P). Otherwise record "not needed" with the number.
- [ ] **Step 4: If anything changed in Steps 2-3, re-run the affected benchmark.** Write both `after` files in
  the same format as `before`, including a "dropped candidates" line naming any §3.3 fix whose delta was nil.
- [ ] **Step 5: Run the F gate and the P gate in full.** Commit both docs (`perf: after numbers`), and push both
  branches.
