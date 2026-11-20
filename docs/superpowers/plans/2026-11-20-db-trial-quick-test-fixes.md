# DB Trial Quick-Test Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The barcode generator sees packing lists made in a Supabase session. Packer Resume reopens an incomplete
list in DB mode with its progress. A regression test pins that tag edits and their undo log nothing in DB mode.

**Architecture:** Two seam fixes. `SupabaseStore._list` also lists the storage blobs under `packing_lists`, as
FileStore does. Packer's `SessionLockManager._read` asks the store's `lock_info()` in DB mode instead of reading a
lock file that only exists on the file share. No new modules.

**Tech Stack:** Python 3.11 floor, PySide6, supabase-py v2, pytest with the local Supabase stack (`@pytest.mark.supabase`).

**Spec:** `docs/superpowers/specs/2026-11-20-db-trial-quick-test-fixes-design.md` (in F). Read it first: it has the
verified causes with file:line.

**Repos and worktrees** (branch `dr/56-db-trial-fixes-from-quick-test-barcode-l` in both):
- F = `/home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-56`
- P = `/home/gloopy/Desktop/Projects/packing-tool-db/.claude/worktrees/dr-56`

## Global Constraints

- File mode (FileStore) behaviour must not change. Every FileStore test passes unchanged.
- `shared/` is edited in F only. P gets it through `scripts/sync_shared.py`, never by hand (a hook blocks edits
  to P's `shared/`).
- A failed Supabase read raises `StoreError`. It never returns empty or default data.
- Stale lock = heartbeat older than 120 s (`shared.store.STALE_SECONDS`).
- Python 3.11 floor: no 3.12+ syntax.
- Git on this VM: `/usr/bin/git`, one plain git command per Bash call, commit with `git commit -F <absolute path>`.
  Message files go in the job's tmp dir, not the repo. End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```
- The pytest-guard hook blocks Bash text containing "pytest" other than the plain run form. Write test files with
  Write/Edit, and run tests only through the commands given here.
- Supabase tests need the local stack (already running: `supabase status` in F). In F,
  `./scripts/run_tests.sh <args>` exports the keys itself. In P, export them first. Run `supabase status -o env`
  from the F worktree and set `SUPABASE_TEST_URL` = `API_URL`, `SUPABASE_TEST_ANON_KEY` = `ANON_KEY`,
  `SUPABASE_TEST_SERVICE_KEY` = `SERVICE_ROLE_KEY`. A supabase test that **skips** has not passed. Check for
  `passed`, not `skipped`.
- P's worktree may have no `.venv`. If so, run `ln -s /home/gloopy/Desktop/Projects/packing-tool-db/.venv .venv` in P.
  F: run `./scripts/setup_venv.sh` if `.venv` is missing.
- Gates: F `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` (via `./scripts/run_tests.sh -q`, ~10 min)
  plus `ruff check .`. P `QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/coverage_check.py` plus `ruff check .`.

## Review Focus

1. **Resume after a crash on the same PC, within 120 s** (the lock was never released and the heartbeat is
   fresh). Expected: Resume reopens the list with no takeover prompt and no "active on another PC" refusal.
   Pinned in Task 5 (`test_resume_after_crash_same_pc_keeps_progress`).
2. **Resume when another PC holds a fresh lock.** Expected: it is refused, naming that PC. Progress is untouched.
   Pinned in Task 4 (`test_db_lock_other_pc_reads_as_locked_not_mine`).
3. **The lock table can't be read** (network or RLS error) during the Resume check. Expected: an unreadable lock
   (refused with `LOCK_UNREADABLE_MSG` unless it's stale), never "free". There is no `local_path(...).stat()`
   call in DB mode. Pinned in Task 4 (`test_db_lock_read_error_is_unreadable_not_free`).
4. **Progress counted twice after reopen.** Expected: packed counts after reopen equal what was packed before.
   Pinned in Task 5 (both resume tests assert the exact count).
5. **`packing_lists` holds a withdrawn list and blobs.** Expected: listing the folder gives the `.json`, the
   `.xlsx` and `withdrawn`, each once. Pinned in Task 1 (`test_packing_lists_lists_xlsx_with_json`).

---

### Task 1 (F): SupabaseStore lists the `.xlsx` blobs under `packing_lists`

**Files:**
- Modify: `shared/store_supabase.py:558-566` (the two `packing_lists` branches of `_list`)
- Test: `tests/shared/test_store_contract.py` (append)

**Interfaces:**
- Produces: `SupabaseStore.list_dir("<session>/packing_lists")` returns the table's `NAME.json`, `withdrawn` when
  any withdrawn row exists, and every storage object name under that prefix (e.g. `NAME.xlsx`). The same applies
  to `…/packing_lists/withdrawn`. This matches FileStore.

- [ ] **Step 1: Write the failing test.** Append to `tests/shared/test_store_contract.py` (it uses that file's
  parametrised `store` fixture and its `S = "Sessions/CLIENT_X/s1/"`):

```python
def test_packing_lists_lists_xlsx_with_json(store):
    store.write_json(S + "packing_lists/DHL.json", {"orders": []})
    store.write_bytes(S + "packing_lists/DHL.xlsx", b"xlsx")
    store.write_json(S + "packing_lists/withdrawn/OLD.json", {"orders": []})
    store.write_bytes(S + "packing_lists/withdrawn/OLD.xlsx", b"xlsx")
    assert store.list_dir(S + "packing_lists") == ["DHL.json", "DHL.xlsx", "withdrawn"]
    assert store.list_dir(S + "packing_lists/withdrawn") == ["OLD.json", "OLD.xlsx"]
```

- [ ] **Step 2: Run it.** `./scripts/run_tests.sh tests/shared/test_store_contract.py -q --no-cov -k packing_lists_lists`.
  Expected: `[file]` PASS, `[supabase]` FAIL (the list has no `.xlsx`).
- [ ] **Step 3: Fix.** In both `packing_lists` branches of `SupabaseStore._list`, add `+ self._storage_ls(rel)` to
  the returned list, as the `…/packing` branch below them does. `list_dir` already de-duplicates and sorts.
- [ ] **Step 4: Run** `./scripts/run_tests.sh tests/shared/test_store_contract.py tests/shared/test_store_supabase.py tests/test_supabase_flows.py tests/test_barcode_tool.py -q --no-cov`.
  Expected: all pass, with none of the supabase ones skipped.
- [ ] **Step 5: Commit** (`fix(store): list packing-list xlsx blobs in Supabase mode`) and push the branch. Task 3
  needs this commit on `origin`.

### Task 2 (F): Regression test, tag edits and undo log nothing in DB mode

**Files:**
- Create: `tests/test_supabase_tag_undo.py`

**Interfaces:**
- Consumes: `EDITS`, `FRAME`, `COMPARED` from `tests/test_undo_roundtrip.py`. The `supabase_client` and `qapp`
  fixtures from `tests/conftest.py`.

- [ ] **Step 1: Write the test.** It's a regression pin, so it passes on first run. The spec §3 says why there's
  no failing step. `pytestmark = pytest.mark.supabase`. Parametrise over the four tag entries of `EDITS`
  (`bulk_add_tag`, `bulk_remove_tag`, `add_internal_tag`, `remove_internal_tag`). For each:
  - `store = SupabaseStore(supabase_client, pc="pc1", cache_dir=tmp_path / "cache")`
  - `monkeypatch.setattr(gui.main_window_pyside, "open_profile_manager", lambda _p: (Mode.SUPABASE, ProfileManager(store=store, require_connection=False)))`
  - `monkeypatch.setattr(gui.session_browser_widget.SessionBrowserWidget, "USE_ASYNC", False)`
  - Patch `gui.main_window_pyside.show_error` to record its headline into a list. A dialog is a failure too.
  - `win = MainWindow(); win.show()`, then `create_client_profile("acme", "Client Acme")`, set
    `win.current_client_id = "acme"`, and call `win.load_client_config("acme")`. This is the setup from
    `tests/test_stale_session.py:16-25`.
  - `path = win.session_manager.create_session("acme")`. Then
    `frame = FRAME.assign(System_note="", Shipping_Provider="DHL")`,
    `session_state.save_state(store, path, frame, None)` and `win.load_existing_session(path)`.
  - Record `before = win.analysis_results_df[COMPARED]` (reset index, copy). Then `caplog.clear()` and
    `caplog.set_level(logging.WARNING)`.
  - Run `edit(win.actions_handler)`, drain, `win.undo_last_operation()`, drain. Here "drain" means
    `win.write_queue` has no pending work: wait on it if it has a wait/flush method, otherwise loop
    `QApplication.processEvents()` with a short sleep for up to 3 s.
  - Assert `[r for r in caplog.records if r.levelno >= logging.WARNING] == []`, assert no dialog was recorded, and
    assert the frame equals `before` (`pd.testing.assert_frame_equal(..., check_dtype=False)`).
  - `win.close()` in a `finally`.
- [ ] **Step 2: Run** `./scripts/run_tests.sh tests/test_supabase_tag_undo.py -q --no-cov`. Expected: 4 passed, 0
  skipped. If it fails, **stop and report the log lines** instead of changing product code. That would be the
  owner's bug reproduced, and it goes back to the orchestrator.
- [ ] **Step 3: Run the F gate**: `./scripts/run_tests.sh -q` (background, ~10 min) and `ruff check .`. Expected:
  green, coverage ≥ 90%.
- [ ] **Step 4: Commit** (`test: pin tag edit + undo logs nothing in Supabase mode`) and push.

### Task 3 (P): Sync `shared/` from F

**Files:**
- Modify: P `shared/store_supabase.py` (written by the sync script only), P `scripts/shared_synced_from.txt`

- [ ] **Step 1:** In P, run `.venv/bin/python scripts/sync_shared.py /home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-56`.
  Expected: only `shared/store_supabase.py` changes, and `shared_synced_from.txt` names F's Task 1 (or Task 2)
  commit. Check with `/usr/bin/git diff --stat`. If other `shared/` files change, stop and report: P was behind F
  before this task.
- [ ] **Step 2:** Run P's suite: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`. Expected: green.
- [ ] **Step 3: Commit** in P (`chore: sync shared from Fulfilment (packing-list xlsx listing)`).

### Task 4 (P): `SessionLockManager` reads the DB lock through `store.lock_info()`

**Files:**
- Modify: P `packing_tool/session_lock_manager.py` (`_read` :93-138, `_mine` :90-91 renamed to `is_mine`, every
  `_mine` call site in the file, `force_release_lock` :374-433)
- Modify: P `gui/sessions_page.py:339-342` (the inline "ours" check)
- Test: P `tests/test_supabase_session_lock.py` (create). Also update any P test that calls `_mine` (`rg -n "_mine" tests`).

**Interfaces:**
- Consumes: `Store.lock_info(list_dir: str) -> LockInfo | None` (`shared/store.py:173`).
  `LockInfo(acquired, holder_pc, holder_user, heartbeat_at: datetime | None)`. BufferedStore passes it through.
- Produces:
  - `SessionLockManager._read(session_dir) -> dict | None`. In DB mode (`not isinstance(self.store, FileStore)`),
    it returns `None` when `lock_info` is `None`. Otherwise it returns
    `{"locked_by": holder_pc, "user_name": holder_user, "lock_time": hb, "heartbeat": hb, "process_id": None, "worker_id": None, "worker_name": None}`
    with `hb = heartbeat_at.isoformat()`. A `StoreError` is retried `LOCK_READ_ATTEMPTS` times. If it keeps
    failing, `_read` returns the existing unreadable dict with `heartbeat` = now, and makes no `local_path` call.
    FileStore keeps today's path unchanged.
  - `SessionLockManager.is_mine(data: dict) -> bool`. DB mode: `data.get("locked_by") == self.hostname`.
    FileStore: hostname and `process_id == self.process_id` (today's rule).
  - `force_release_lock(session_dir, expected=None)`. In DB mode `expected=None` means `expected = the lock just
    read`, never `store.delete(...)`. The rest of the method (compare, then `force_take_list_lock`) is unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_supabase_session_lock.py`, with
  `pytestmark = pytest.mark.supabase`. Reuse the `pcs`-style setup from `tests/test_supabase_flows.py:18-31`: two
  `SupabaseStore`s "A" and "B" over one session, with `_backdate(supabase_admin, seconds)`. Build each manager with
  `SessionLockManager(ProfileManager(str(tmp_path / "config.ini"), store=store))`. Set `mgr.hostname` to "A" or
  "B", and `mgr.username` too. Use the work dir `Path(store.local_path(LIST1))`.
  - `test_db_lock_free_reads_as_unlocked`: A's `is_locked(work_dir) == (False, None)`.
  - `test_db_lock_other_pc_reads_as_locked_not_mine`: A runs `acquire_lock("ZED", wd)`, which is `(True, None, None)`.
    Then B's `is_locked` is `(True, lock)` with `lock["locked_by"] == "A"` and `lock["user_name"]` = A's username,
    and `B.is_mine(lock)` is False while `A.is_mine(lock)` is True. B's `acquire_lock` returns `False`, with a
    message containing "A".
  - `test_db_stale_lock_force_release_takes_it`: A acquires, then `_backdate(admin, 300)`. B's lock is
    `is_lock_stale`, `B.force_release_lock(wd, expected=lock)` is True, and `store_b.lock_info(LIST1).holder_pc == "B"`.
  - `test_db_force_release_without_expected_takes_over`: same as above, with `expected=None`. True, and B holds it.
  - `test_db_lock_read_error_is_unreadable_not_free`: monkeypatch A's store `lock_info` to raise
    `StoreError("down")`, and monkeypatch `LOCK_READ_RETRY_SECONDS` to 0. `is_locked` gives
    `(True, lock)` with `lock["unreadable"] is True`, and `store.local_path` is never called for the lock file
    (patch it to fail on a `.session.lock` path).
  - `test_sessions_page_treats_own_pc_db_lock_as_ours`: drive `SessionsPage._stale_lock` (see how
    `tests/test_sessions_page.py` builds the page, and reuse that). A's lock, backdated 300 s, with a manager whose
    hostname is "A" and a different `process_id`, gives `None` (no takeover prompt).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_supabase_session_lock.py -q`
  (Supabase env exported). Expected: they FAIL with `ValueError: not a JSON path`.
- [ ] **Step 3: Implement** the Produces block above. In `sessions_page.py` the `ours = (...)` expression becomes
  `ours = self._locks.is_mine(lock)`.
- [ ] **Step 4: Run** the new file plus `tests/test_sessions_page.py tests/test_sessions_mainwindow_seam.py tests/test_supabase_flows.py`,
  and every test file `rg -l "session_lock_manager|SessionLockManager" tests` lists. Expected: all pass, none of
  the supabase ones skipped.
- [ ] **Step 5: Commit** in P (`fix(packer): read the DB list lock through the store on resume`).

### Task 5 (P): Resume end to end in DB mode keeps progress

**Files:**
- Test: P `tests/test_supabase_resume.py` (create)

**Interfaces:**
- Consumes: Task 4's `SessionLockManager` (DB mode). `PackerLogic(client_id, profile_manager, work_dir)`,
  `load_packing_list_json(path)`, `start_order_packing(order_number)`, `process_sku_scan(sku)`, `close()`, and
  `session_packing_state` keys `in_progress` and `completed_orders` (`packing_tool/packer_logic.py`).
  The `make_packing_list(orders)` helper from `tests/conftest.py:170`.

- [ ] **Step 1: Write the tests**, with `pytestmark = pytest.mark.supabase`. Seed one session over a
  `SupabaseStore(supabase_client, pc="PC1", cache_dir=tmp_path / "cache")`. Write `session_info.json`
  (`{"status": "active"}`), then `packing_lists/DHL.json` = `make_packing_list(ORDERS)` with `list_name = "DHL"`,
  where `ORDERS = [("ORDER-001", "DHL", [{"sku": "SKU-AAA", "quantity": 4, "product_name": "Widget A"}])]`. This is
  the same order as `tests/test_offline_restart.py`. The work dir is `Path(store.local_path(LIST))` with
  `LIST = "<session>/packing/DHL"`, and the list path is `store.local_path("<session>/packing_lists/DHL.json")`.
  Write a helper `_pack(store, n)`: it builds a `ProfileManager(str(tmp_path / "config.ini"), store=store)` and a
  `SessionLockManager` on it (`hostname = "PC1"`), asserts `acquire_lock("Z", wd)[0]`, builds `PackerLogic`, loads
  the list, runs `start_order_packing("ORDER-001")` and then `process_sku_scan("SKU-AAA")` n times, and returns
  `(logic, lock_manager)`.
  - `test_resume_after_close_keeps_progress`: run `_pack(store, 2)`, then `logic.close()` and
    `lock_manager.release_lock(wd)`. A **new** store, ProfileManager and manager (a fresh process: new
    `SupabaseStore` on the same client, new cache dir) checks `is_locked(wd) == (False, None)` and that
    `acquire_lock` succeeds. A new `PackerLogic` loads the list, and
    `sum(i["packed"] for i in session_packing_state["in_progress"]["ORDER-001"]) == 2`. Two more scans put
    "ORDER-001" in `completed_orders` (match by order number if the entries are dicts).
  - `test_resume_after_crash_same_pc_keeps_progress`: same, but with no `release_lock` and a new manager with a
    different `process_id` (a new process on the same PC). `is_mine(lock)` is True, `acquire_lock` succeeds, and
    progress is 2, not 0 and not 4.
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_supabase_resume.py -q`.
  Expected: PASS (Task 4 is in). If one fails for a reason other than test setup (state not loaded, count wrong,
  a store error), **stop and report the traceback**. That's a second resume bug, and it goes back to the
  orchestrator.
- [ ] **Step 3: Run the P gate** `QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/coverage_check.py` and `ruff check .`.
  Expected: green, coverage gates met.
- [ ] **Step 4: Commit** in P (`test(packer): DB-mode resume keeps progress after close and after a crash`) and push the P branch.
