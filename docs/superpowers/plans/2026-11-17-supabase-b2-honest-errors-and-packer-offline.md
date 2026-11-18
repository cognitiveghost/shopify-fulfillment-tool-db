# Supabase B2: Honest Errors and the Full Packer Offline Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split store errors into network vs permanent, make sign-in honest, prune Fulfilment history through the Store, and let the Packer finish a list offline, restart offline, and sync without lost or double-counted data.

**Architecture:** `StoreUnreachable(StoreError)` marks network failures. Shared-document updates become named, serialisable `Delta`s that the Packer's `BufferedStore` can queue and replay through `update_json`'s compare-and-set, with the op id stored in the document it guards. One `BufferedStore` per Packer run in database mode, bound to the open list, with a snapshot of what a restart needs.

**Tech Stack:** Python 3, PySide6, supabase-py over httpx, pytest (`QT_QPA_PLATFORM=offscreen`).

**Spec:** `docs/superpowers/specs/2026-11-17-supabase-b2-honest-errors-and-packer-offline-design.md` (F repo). Read it before your task; section numbers below (§) refer to it.

## Repos and worktrees

- **F** = Fulfilment fork, worktree `/home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-49`
- **P** = Packer fork, worktree `/home/gloopy/Desktop/Projects/packing-tool-db/.claude/worktrees/dr-49`
- Both on branch `dr/49-supabase-b2-honest-errors-and-the-full-p`.
- Tasks F1–F6 run in F, then P1–P9 in P. P1 mirrors F's `shared/`, so all F tasks must be committed and pushed first.

## Global Constraints

- `shared/` is edited **only in F**. P gets it through `python scripts/sync_shared.py /home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-49` run in P. A hook blocks editing `P/shared/`.
- Every user-facing string goes in `_()` from `shared.i18n` (`N_()` for module-level constants), with a Bulgarian translation added to that repo's catalogue. The `tests/test_i18n_catalogue.py` guards fail otherwise.
- No hardcoded colours. Use theme tokens from `shared/theme.py`.
- No UI calls from background threads. Use Qt signals.
- Git: `/usr/bin/git`, one plain git command per Bash call, commit with `git commit -F <absolute path to a message file>`. Message files go in `/home/gloopy/.claude/jobs/b967d7dc/tmp/` (or the job tmp dir you were given). End each message with the `Co-Authored-By` trailer your session gives you.
- Tests: write test files with Write/Edit. Run them with the plain form `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest <path> -q` (the pytest-guard hook blocks other forms).
- F gate: `scripts/run_tests.sh` (sets the Supabase test env from the local stack already running in the F main checkout; do **not** `supabase start` a second one) plus `.venv/bin/ruff check .`.
- P gate: `.venv/bin/python scripts/coverage_check.py` plus `.venv/bin/ruff check .`.
- A worktree missing `.venv`: `ln -s <main checkout>/.venv .venv` (F main: `/home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db`, P main: `/home/gloopy/Desktop/Projects/packing-tool-db`).
- Exact strings (copy verbatim):
  - `Your saved sign-in has expired. Sign in again.`
  - `Wrong email or password. Try again.`
  - `Not signed in` / `The app needs a sign-in to start. Open it again to sign in.`
  - `Database unreachable: working from file server` / `Changes save to the file server. Restart the app to try the database again.` (already in F's catalogue)
  - `Offline` / `Progress is saved on this PC and syncs when the database is back.`
  - `Synced`
  - `Waiting to sync` / `List {list} is finished and waiting to sync. Retrying every minute. Closing the app is safe.` / `Sync now`
  - `Cannot sync offline progress: {reason}. Nothing is lost; tell the admin.`

## Review Focus

1. **A delta whose document is gone on the server** (session_info deleted while offline): flush must stop with `sync_error` set and the outbox intact, not loop silently or drop the op. Test in P5 (`test_missing_document_on_replay_stops_and_keeps_outbox`).
2. **Going offline mid-`update_json` with a `Delta`**: the delta must be queued once, not applied locally and also queued. Test in P5 (`test_delta_queued_when_inner_drops_mid_call`).
3. **Outbox written before B2** (no `list.json`, no snapshot): startup must show the waiting-to-sync screen named from the outbox, never crash or fall back to file mode. Test in P7 (`test_old_outbox_without_list_json_shows_waiting_screen`).
4. **Saved sign-in refused once back online in OFFLINE_BUFFER mode**: the sync tick must hand off to the UI thread for the dialog, never prompt from the worker thread. Test in P8 (`test_sync_tick_with_refused_token_asks_on_ui_thread`).
5. **A write to a doc outside the allow-list while offline** (e.g. `Clients/CLIENT_M/client_config.json`): raises `StoreUnreachable`, nothing queued. Test in P4 (`test_offline_write_outside_allow_list_raises_unreachable`).

---

## F tasks (Fulfilment fork)

### Task F1: `StoreUnreachable` and the error split in `SupabaseStore`

**Files:**
- Modify: `shared/store.py:47-58` (exceptions)
- Modify: `shared/store_supabase.py:80-93` (`_guard`)
- Test: `tests/shared/test_store_errors.py` (new), `tests/shared/test_store_supabase.py` (add one supabase-marked test)

**Interfaces:**
- Produces: `shared.store.StoreUnreachable(StoreError)`; `shared.store_supabase._is_unreachable(exc: BaseException) -> bool`.

- [ ] **Step 1: Write the failing tests** in `tests/shared/test_store_errors.py`:
  - `test_store_unreachable_is_a_store_error`: `issubclass(StoreUnreachable, StoreError)`.
  - `test_transport_errors_are_unreachable`: `_is_unreachable(httpx.ConnectError("x"))`, `httpx.ReadTimeout("x")`, `httpx.RemoteProtocolError("x")`, `OSError()` and `json.JSONDecodeError("x", "", 0)` are all True.
  - `test_server_5xx_is_unreachable`: a postgrest `APIError` built for HTTP 503, and a `StorageException` with `{"statusCode": 502}`, are True. Build the APIError the way postgrest does for a non-JSON gateway body; read `postgrest/exceptions.py` in `.venv` to see which field carries the status.
  - `test_permanent_errors_are_not_unreachable`: `APIError({"code": "42501", "message": "permission denied"})`, an APIError for HTTP 400, a `StorageException({"statusCode": 404})`, `ValueError()` and `StoreConflict("x")` are all False.
  - `test_guard_raises_unreachable_for_network_and_plain_error_otherwise`: a dummy class with one `@_guard` method raising `httpx.ConnectError` gives `StoreUnreachable`; raising `APIError({"code": "42501", ...})` gives `StoreError` that is **not** `StoreUnreachable`.
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/shared/test_store_errors.py -q`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement.** Add `class StoreUnreachable(StoreError)` with docstring "The backend could not be reached; retrying later may succeed." In `store_supabase.py`, add `_is_unreachable(exc)`: True for `httpx.TransportError`, `OSError`, `json.JSONDecodeError`, pydantic `ValidationError`, `httpx.HTTPStatusError` with status ≥ 500, and `APIError`/`StorageException` whose HTTP status (when readable) is ≥ 500. When the status cannot be read, return False (permanent). In `_guard`, raise `StoreUnreachable` when `_is_unreachable(e)`, else `StoreError`, keeping `from e` and the message format.
- [ ] **Step 4: Add the supabase-marked test** in `tests/shared/test_store_supabase.py`, following that file's existing fixtures: `test_unauthenticated_write_is_permanent_not_unreachable`. A `SupabaseStore` over an anon-key client that is not signed in calls `write_json("Workers/workers.json", {"workers": []})` and raises `StoreError`, and `not isinstance(e, StoreUnreachable)`.
- [ ] **Step 5: Run** the new file plus `tests/shared/test_store_supabase.py` through `scripts/run_tests.sh tests/shared/test_store_errors.py tests/shared/test_store_supabase.py`. Expected: PASS.
- [ ] **Step 6: Commit** `feat(store): StoreUnreachable splits network failures from permanent errors`.

### Task F2: `Delta`, the replayable named update

**Files:**
- Modify: `shared/store.py` (add after `LockInfo`)
- Test: `tests/shared/test_store_delta.py` (new)

**Interfaces:**
- Produces, in `shared.store`:
  - `APPLIED_OPS = "applied_ops"`, `APPLIED_OPS_KEEP = 200`
  - `DELTAS: dict[str, Callable[..., Any]]`
  - `def delta(kind: str)`: decorator registering `fn(current: Any | None, **args) -> Any` under `kind`. It returns `fn` unchanged and raises `ValueError` on a duplicate kind registered by a *different* function. Re-registering the same function (module reload) is fine.
  - `class Delta`: `__init__(self, kind: str, args: dict, op_id: str | None = None)`; `__call__(self, current)`; `to_op(self) -> dict` returning `{"kind", "args", "op_id"}`; `@classmethod from_op(cls, op: dict) -> Delta`. Attributes `kind`, `args`, `op_id`.

- [ ] **Step 1: Write failing tests:**
  - `test_delta_without_id_just_applies`: register `"test.add"` as `lambda cur, n: {"n": (cur or {}).get("n", 0) + n}`; `Delta("test.add", {"n": 2})({"n": 1}) == {"n": 3}`, and the result has no `applied_ops` key.
  - `test_delta_with_id_applies_once`: `d = Delta("test.add", {"n": 2}, op_id="a1")`; `once = d({"n": 1})`; `once["n"] == 3`, `once["applied_ops"] == ["a1"]`; `d(once)["n"] == 3` (skipped).
  - `test_applied_ops_ring_keeps_last_200`: start from a doc with `applied_ops = [str(i) for i in range(200)]`; apply id `"new"`; `len == 200`, `[-1] == "new"`, `"0" not in`.
  - `test_round_trip_through_json`: `Delta.from_op(json.loads(json.dumps(d.to_op())))` applies the same as `d`.
  - `test_unknown_kind_raises_store_error`: `Delta.from_op({"kind": "nope", "args": {}, "op_id": "x"})(None)` raises `StoreError`, not KeyError, so a flush treats it as permanent.
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/shared/test_store_delta.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** as in spec §8.1. `__call__` with an `op_id` returns `current` unchanged when `op_id in (current or {}).get(APPLIED_OPS, [])`. Otherwise it applies the fn, sets `result[APPLIED_OPS] = (prev + [op_id])[-APPLIED_OPS_KEEP:]` (prev taken from `current`), and requires `result` to be a dict (`StoreError` if not). Add the spec's `# ponytail:` line about the ring's ceiling.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(store): Delta, a named update that can be queued and replayed once`.

### Task F3: `stats.record_packing` as a Delta

**Files:**
- Modify: `shared/stats_manager.py` (`_get_default_stats` ~106, `_with_defaults` ~121, `_atomic_update` ~160, `record_packing` ~226-275)
- Test: `tests/shared/test_stats_manager_delta.py` (new). The existing stats tests must still pass unchanged.

**Interfaces:**
- Consumes: `Delta`, `delta` (F2).
- Produces: registered kind `"stats.record_packing"` with args `client_id, session_id, worker_id, orders_count, items_count, metadata, timestamp` (all JSON-serialisable; `metadata` may be None). `StatsManager.record_packing(...)` keeps its signature.

- [ ] **Step 1: Write failing tests:**
  - `test_record_packing_uses_a_delta`: a `FileStore` spy whose `update_json(rel, fn)` records `fn`; after `StatsManager(store).record_packing("M", "s1", "001", 3, 5)`, the recorded fn is a `Delta` of kind `"stats.record_packing"` and `fn.args["timestamp"]` is set.
  - `test_replayed_record_packing_counts_once`: apply `Delta("stats.record_packing", args, op_id="x")` twice through `store.update_json(STATS_REL, ...)` on a FileStore; `total_orders_packed == 3`, `total_sessions == 1`, `len(packing_history) == 1`, and the history record's `timestamp == args["timestamp"]`.
  - `test_record_packing_online_matches_before`: `record_packing` on a fresh store gives the same totals, `by_client` and history fields as the pre-change behaviour (assert the literal expected dict apart from timestamps).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Move the default-stats dict and the with-defaults merge to module-level functions `_default_stats() -> dict` and `_stats_with_defaults(stats) -> dict`, and keep the two methods as one-line delegates (other code calls them). Add `@delta("stats.record_packing") def _record_packing_delta(current, *, client_id, session_id, worker_id, orders_count, items_count, metadata, timestamp) -> dict`. It applies `_stats_with_defaults`, then today's body with `record["timestamp"] = timestamp`, then sets `stats["last_updated"] = timestamp`. `record_packing` builds the args with `timestamp=get_current_timestamp()` and runs `store.update_json(STATS_REL, Delta(...))` inside today's `_with_retries` and unreadable-JSON recovery. Generalise `_atomic_update` to accept a ready doc-level fn, or add a sibling; don't duplicate the recovery block.
- [ ] **Step 4: Run** the new file and `tests/shared/ -q -k stats`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(stats): record_packing is a replayable Delta`.

### Task F4: Honest sign-in (shared)

**Files:**
- Modify: `shared/db_auth.py:16-75`, `shared/components/sign_in_dialog.py`, `shared/store_mode.py:27-44`
- Modify: F catalogue (the Bulgarian `.po`/catalogue file that `tests/test_i18n_catalogue.py` checks)
- Test: `tests/shared/test_db_auth.py`, `tests/shared/test_sign_in_dialog.py`, `tests/shared/test_store_mode.py`

**Interfaces:**
- Produces: `shared.db_auth.SignInCancelled(Exception)`; `connect(app_name, prompt: Callable[[str | None], tuple[str, str] | None], factory=...)` returns a client, returns `None` only when unreachable, and raises `SignInCancelled` on cancel. `SignInDialog.ask(parent, error: str | None = None) -> tuple[str, str] | None`. `open_store(..., prompt: Callable[[str | None], ...])` lets `SignInCancelled` propagate.

- [ ] **Step 1: Update and add tests.** In `test_db_auth.py`:
  - `test_cancel_returns_none` becomes `test_cancel_raises_sign_in_cancelled`.
  - `test_refused_saved_token_prompts_with_expired_message`: the prompt's first `error` arg is `"Your saved sign-in has expired. Sign in again."`.
  - `test_wrong_password_asks_again_with_message`: after an `AuthApiError`, the next prompt gets `"Wrong email or password. Try again."`.
  - `test_keeps_asking_past_five_wrong_passwords`: six wrong answers, then a right one, returns the client.
  - `test_unreachable_returns_none_quickly` stays.

  In `test_sign_in_dialog.py`: `test_error_line_hidden_without_error` and `test_error_line_shows_given_text`. In `test_store_mode.py`: `test_open_store_lets_sign_in_cancelled_through`.
- [ ] **Step 2: Run** those three files. Expected: FAIL.
- [ ] **Step 3: Implement** per spec §7. Remove `PROMPT_ATTEMPTS`; the loop keeps prompting until success, cancel or unreachable. The dialog gets a word-wrapped `QLabel` (objectName `SignInError`) above the buttons, coloured with the theme's error/danger token (find the token name in `shared/theme.py`), hidden when `error` is None. Add the Bulgarian for the two new strings.
- [ ] **Step 4: Run** the three files plus `tests/test_i18n_catalogue.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(auth): sign-in says why it asks again; cancel is not "unreachable"`.

### Task F5: Fulfilment startup quits on a cancelled sign-in

**Files:**
- Modify: `gui/main_window_pyside.py:~72-88` (`open_profile_manager`, prompt lambda now takes `error`), and the startup caller that builds the window (`gui_main.py:~66` or the window `__init__`; follow where `open_profile_manager` is called)
- Modify: F catalogue
- Test: `tests/test_store_startup.py`

**Interfaces:**
- Consumes: `SignInCancelled`, `SignInDialog.ask(parent, error)` (F4).

- [ ] **Step 1: Write the failing test** `test_cancelled_sign_in_quits_with_message`. Patch `open_store` to raise `SignInCancelled` and `QMessageBox.information` (or whichever box you use) to record calls. Starting the app path that calls `open_profile_manager` shows title `"Not signed in"` and text `"The app needs a sign-in to start. Open it again to sign in."`, builds no main window, and exits with code 0. Keep the existing FILE-banner tests green.
- [ ] **Step 2: Run** `tests/test_store_startup.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** Catch `SignInCancelled` at the startup call site, show the box, return exit code 0. Change the prompt to `lambda error: SignInDialog.ask(parent, error)`. Add the Bulgarian for the two strings.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(startup): a cancelled sign-in quits instead of falling back to the file server`.

### Task F6: History pruning through the Store, then the F gate

**Files:**
- Modify: `shopify_tool/fulfillment_history.py:104-120` (`session_folders`), `shopify_tool/core.py:~1340-1348`
- Test: `tests/test_fulfillment_history.py` (update the two existing `session_folders` tests), `tests/shared/test_store_supabase.py` or a new `tests/test_fulfillment_history_supabase.py` (supabase-marked)

**Interfaces:**
- Produces: `session_folders(store: Store, sessions_rel: str, current: str | None) -> set[str] | None`. `sessions_rel` is `f"Sessions/CLIENT_{client_id}"`.

- [ ] **Step 1: Update and add tests:**
  - `test_session_folders_lists_through_the_store`: FileStore with `Sessions/CLIENT_M/s1/session_info.json` and `s2/...` gives `{"s1", "s2"}` (it may also contain non-folder entries; assert `{"s1", "s2"} <= result`).
  - `test_session_folders_refuses_a_listing_without_the_current_session`: re-pointed at the store.
  - `test_without_a_listing_nothing_is_pruned`: a store whose `list_dir` raises `StoreError` gives `None`.
  - Supabase-marked `test_supabase_run_prunes_deleted_but_not_archived_session_rows`: on the local stack, history rows for sessions `a` (live), `b` (archived) and `c` (no row). `record_session(..., existing_sessions=session_folders(store, rel, "a"))` keeps `a` and `b`, drops `c`. If `list_dir` omits archived sessions on SupabaseStore (it does: `archived=False`), `b`'s rows are dropped. That is **today's FileStore behaviour too** (an archived folder moves to `_archive`). In that case assert parity with FileStore instead: write the same scenario against a FileStore with `b` under `Sessions/_archive/CLIENT_M/b`, and assert both backends keep and drop the same sessions. Name the test `test_pruning_matches_between_backends` and note which behaviour you found in the commit message.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** `session_folders` calls `set(store.list_dir(sessions_rel))` and returns `None` on `StoreError`, keeping the warning logs and the `current` check. `core.py` passes the history store (or `profile_manager.store`) and `f"Sessions/CLIENT_{client_id}"` instead of `Path(session_path).parent`; keep the `if session_path` guard.
- [ ] **Step 4: Run** the history tests, then the **F gate**: `scripts/run_tests.sh` and `.venv/bin/ruff check .`. Expected: all pass, ruff clean.
- [ ] **Step 5: Commit** `fix(history): prune through Store.list_dir so Supabase sees every session`, then `/usr/bin/git push`.

---

## P tasks (Packer fork)

### Task P1: Mirror shared/, adapt to the error split and the new sign-in

**Files:**
- Run: `.venv/bin/python scripts/sync_shared.py /home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-49` (updates `shared/` and `scripts/shared_synced_from.txt`)
- Modify: `packing_tool/offline_buffer.py:111,127-133,186` (`_lost`, own "offline" errors), `gui/main_window.py:~1002-1036` (prompt lambda, `SignInCancelled`, FILE banner)
- Modify: P catalogue (Bulgarian for the sign-in strings, unless the sync brings them)
- Test: `tests/test_offline_buffer.py` (FlakyStore raises `StoreUnreachable`), `tests/test_startup_store.py`

**Interfaces:**
- Consumes: `StoreUnreachable`, `Delta`, `SignInCancelled`, `SignInDialog.ask(parent, error)`.
- Produces: `BufferedStore` goes offline only on `StoreUnreachable`. Its own refusals raise `StoreUnreachable("offline")`.

- [ ] **Step 1: Sync** shared and confirm `scripts/shared_synced_from.txt` names F's HEAD.
- [ ] **Step 2: Write failing tests.** In `tests/test_offline_buffer.py`, `FlakyStore._check` raises `StoreUnreachable("network")`, and add `self.broken = None`: when set, writes raise `StoreError(self.broken)`.
  - `test_permanent_error_is_raised_not_buffered`: online, `inner.broken = "denied"`; `b.write_json(STATE, {...})` raises `StoreError` (not `StoreUnreachable`); `(buf / "outbox.jsonl")` does not exist; `b.online is True`.
  - `test_unreachable_is_buffered`: `inner.down = True`; the write succeeds, the outbox has 1 line, and `b.online is False`.

  In `tests/test_startup_store.py`:
  - `test_cancelled_sign_in_quits_with_message` (as F5, Packer startup).
  - `test_file_mode_shows_database_unreachable_banner`: `open_store` patched to `(Mode.FILE, None)`; the window shows a persistent banner with the two FILE strings. Reuse P's `ConnectionBanner` or the toast/banner component P already has for persistent messages; name the one you reused in the commit message.
- [ ] **Step 3: Run** both files. Expected: FAIL.
- [ ] **Step 4: Implement** per spec §6.2 and §7 (Packer half).
- [ ] **Step 5: Run** both files, then the whole P suite (`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`). Fix tests that relied on the old `StoreError("network")` offline semantics by switching them to `StoreUnreachable`, nothing else.
- [ ] **Step 6: Commit** `feat: sync shared (B2); buffer only on unreachable; honest sign-in`.

### Task P2: Worker stats and session progress as Deltas

**Files:**
- Modify: `packing_tool/worker_manager.py:~284-345` (`update_worker_stats`), `packing_tool/session_manager.py:94-148` (`update_session_metadata`)
- Test: `tests/test_worker_manager_delta.py`, `tests/test_session_manager_delta.py` (new)

**Interfaces:**
- Consumes: `Delta`, `delta` (shared).
- Produces registered kinds:
  - `"workers.update_stats"`, args `worker_id, sessions, orders, items, duration_seconds, session_id, timestamp`. The doc is `REGISTRY`. The fn includes today's `_update_registry` wrapper (`{"workers": []}` default, `data["version"] = REGISTRY_VERSION`) and the per-worker body, with `now` taken from `timestamp`.
  - `"session_info.packing_progress"`, args `packing_list_name, status, completed_orders, timestamp`. A `None` current raises `StoreNotFound("session_info.json")`. The body is today's `edit` with `get_current_timestamp()` replaced by `timestamp`.

- [ ] **Step 1: Write failing tests:**
  - `test_update_worker_stats_uses_delta_and_counts_once_on_replay`: the manager calls `update_json` with a `Delta` of the right kind. Applying it twice with an `op_id` through a FileStore adds the counts once, and the returned `WorkerProfile` (if the method returns one today) is still correct.
  - `test_update_session_metadata_uses_delta`: the same for session progress. `completed_orders` merge semantics are unchanged (existing tests stay green).
  - `test_session_progress_delta_on_missing_doc_raises_not_found`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the corrupt-registry recovery in `_update_registry` (it now receives the `Delta`). `update_session_metadata` drops the `store.exists` pre-check (§8.1) and still logs and swallows any exception, as today. `update_worker_stats` derives the returned worker (if any) from the doc `update_json` returns.
- [ ] **Step 4: Run** the new files plus the existing worker and session manager tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: worker stats and session progress are replayable Deltas`.

### Task P3: BufferedStore per run: list binding, list.json, snapshot, UnreachableStore

**Files:**
- Modify: `packing_tool/offline_buffer.py`
- Test: `tests/test_offline_buffer.py` (update `make()` and the existing tests to the new constructor), `tests/test_offline_buffer_binding.py` (new)

**Interfaces:**
- Produces, in `packing_tool.offline_buffer`:
  - `LIST_META = "list.json"`, `SNAPSHOT = "snapshot"`
  - `class UnreachableStore(Store)`: every Store method raises `StoreUnreachable("database not connected")`. `derived_indexes = True` (Supabase mode is derived).
  - `BufferedStore(inner: Store, buffer_dir: Path, *, pc: str, on_change: Callable[[bool], None] | None = None)`. On construction it rebuilds `state/` from the outbox (as today) and, when `list.json` exists, re-binds that list.
  - `begin_list(list_dir: str, meta: dict, snapshot_rels: list[str]) -> None`: raises `StoreError` when the outbox holds ops for another list. Writes `list.json` atomically (`meta` plus `"list_dir"`). Copies each snapshot rel from `inner` (`read_json`) into `buffer_dir/snapshot/<rel>`; a `StoreNotFound` is skipped and any other error raises.
  - `end_list() -> None`: unbinds and deletes `list.json` and `snapshot/` only when the outbox is empty. Otherwise it does nothing (the list stays bound until synced).
  - `bound_list() -> dict | None`: the `list.json` contents.
  - `@staticmethod pending_meta(buffer_dir) -> dict | None`: `list.json` when the outbox is non-empty, else None.
  - `attach(inner: Store) -> None`: swap the inner store (UnreachableStore to SupabaseStore).
  - `online` flips call `on_change(online)` only on change.
  - Reads (`read_json`, `read_bytes`, `exists`): buffered `state/` first when offline (today), then `inner`. On `StoreUnreachable` from `inner`, a snapshot copy of that rel is served if present, otherwise it re-raises.

- [ ] **Step 1: Write failing tests** in `tests/test_offline_buffer_binding.py`, with `PL = "Sessions/CLIENT_M/2026-01-01_1/packing_lists/DHL.json"` and `CFG = "Clients/CLIENT_M/packer_config.json"`:
  - `test_begin_list_writes_meta_and_snapshot`.
  - `test_snapshot_served_when_unreachable`: begin online, `inner.down = True`; `read_json(PL)` returns the snapshot.
  - `test_snapshot_not_served_when_online`: the server value wins.
  - `test_restart_rebinds_from_list_json`: a new `BufferedStore` over `UnreachableStore()` and the same dir; `bound_list()["list_dir"] == LIST`, `read_json(STATE)` gives the buffered state, `read_json(PL)` gives the snapshot.
  - `test_end_list_keeps_binding_while_outbox_pending`.
  - `test_begin_other_list_refused_while_pending` (moved from `test_second_list_refused_while_outbox_pending`).
  - `test_on_change_fires_on_flip_only`.
  - `test_unreachable_store_raises_unreachable_everywhere` (parametrised over the Store method names).
  - `test_attach_swaps_inner`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Write `list.json` with `FileStore(buffer_dir).write_json`, or tmp + `os.replace` + fsync.
- [ ] **Step 4: Run** both offline-buffer test files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(offline): one BufferedStore per run, bound to the open list, with a restart snapshot`.

### Task P4: Offline allow-list: deltas only on shared docs

**Files:**
- Modify: `packing_tool/offline_buffer.py` (`_allowed_exact`, `_check_allowed`, `update_json`, `write_json`, `release_list_lock`)
- Test: `tests/test_offline_buffer.py`

**Interfaces:**
- Consumes: `Delta` (shared), `REGISTRY` from `packing_tool.worker_manager` (or its literal `Workers/workers.json`, if importing it makes an import cycle), `STATS_REL`.
- Produces outbox op shapes:
  - `{"op": "delta", "rel", "list_dir", "kind", "args", "op_id"}`, with `op_id = uuid.uuid4().hex` made when queued.
  - `{"op": "release_lock", "list_dir"}`.
  - `BufferedStore.finished() -> bool`: a queued `release_lock` exists.

- [ ] **Step 1: Write failing tests:**
  - `test_offline_delta_on_shared_doc_is_queued`: for each of STATS, `Workers/workers.json` and INFO, offline `update_json(rel, Delta(...))` returns None (or the input doc; pick one and document it), the outbox gains one `delta` op with an `op_id`, and `state/` does not gain the rel.
  - `test_offline_plain_write_to_shared_doc_raises_unreachable`: `write_json(STATS, {...})`, and `update_json(STATS, lambda c: c)`, offline both raise `StoreUnreachable`, nothing queued.
  - `test_offline_write_outside_allow_list_raises_unreachable` (Review Focus 5).
  - `test_delta_queued_when_inner_drops_mid_call` (Review Focus 2): inner `update_json` raises `StoreUnreachable`; exactly one delta op is queued and inner's doc is unchanged.
  - `test_offline_release_of_bound_list_is_queued`: `release_list_lock(LIST, "PC1")` offline queues `release_lock`, and `finished()` is True.
  - Rewrite `test_finish_list_offline_queues_summary_and_stats` to use a stats `Delta` and a session-progress `Delta` instead of plain writes to STATS and INFO.
  - Delete `test_update_json_offline_applies_to_buffer` / `..._without_known_state_refuses` if they now contradict the rule. Keep their list-folder variants: `update_json` on `STATE` offline still applies to `state/` and queues `update_json_result`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** per spec §8.2 bullets "Allow-list" and "Outbox ops". Remove the old `# ponytail:` whole-document comment in `update_json`; it no longer applies.
- [ ] **Step 4: Run** the offline-buffer tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(offline): queue shared-doc updates as deltas, never whole documents`.

### Task P5: Flush replays deltas once and stops on a permanent error

**Files:**
- Modify: `packing_tool/offline_buffer.py` (`FlushResult`, `flush`, `_write_conflict`)
- Test: `tests/test_offline_flush.py` (new)

**Interfaces:**
- Produces: `FlushResult(pushed: int, conflict_rel: str | None = None, error: str | None = None)`; `BufferedStore.sync_error: str | None`.

- [ ] **Step 1: Write failing tests** with a `FlakyStore` (from `tests/test_offline_buffer.py`; move it to `tests/offline_fakes.py` and import it in both files) extended with `kill_after_commit: int | None`. That hook counts committed `update_json` calls and raises `KeyboardInterrupt` right after the Nth commit. Use the real managers: `StatsManager(b)`, `WorkerManager(b)` and `SessionManager(client_id="M", store=b)` over the BufferedStore, seeded with a worker `001` and a session_info at INFO.
  - `test_finish_offline_then_flush_matches_online` (spec test 3). Server S1: managers over S1 directly, then the three finish calls. Server S2: a BufferedStore over S2, `inner.down = True`, the same three calls, `inner.down = False`, `flush()`. The stats, workers registry and INFO docs are equal after dropping `applied_ops` and the timestamp fields both sides set from `args["timestamp"]` (freeze `get_current_timestamp` with monkeypatch so they match exactly; then only `applied_ops` differs).
  - `test_other_pc_edit_survives_flush` (spec test 4): finish offline; then `StatsManager(inner).record_packing("B", "s9", "002", 7, 9)` straight on the server; flush. `total_orders_packed == 3 + 7` and `by_client` has both clients.
  - `test_killed_flush_counts_once` (spec test 5): finish offline; `inner.kill_after_commit = 1`; `flush()` raises `KeyboardInterrupt`; then `inner.kill_after_commit = None`, a new `BufferedStore(inner, same_dir, pc="PC1")`, `flush()`. `total_sessions == 1` and the worker's `total_sessions == 1`.
  - `test_permanent_error_mid_flush_keeps_outbox` (spec test 7): finish offline; `inner.broken = "denied"`; `flush()` returns `error == "...denied..."`; the outbox bytes are identical before and after; `sync_error` is set; `online is False`. Then `inner.broken = None`, flush, and the totals are counted once.
  - `test_missing_document_on_replay_stops_and_keeps_outbox` (Review Focus 1): delete INFO on the server before flush; the result has `error` set and the outbox is kept.
  - `test_release_lock_replayed_last`: after a flush of a finished list, `inner.lock_info(LIST)` is None, and `list.json`/`snapshot/` are gone.
  - `test_conflict_parks_deltas_unapplied`: `inner.lock_ours = False`; flush writes the conflict copy with a `"deltas"` list, the stats on the server are unchanged, and the outbox is cleared (today's conflict behaviour).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** per spec §8.2 "flush()". First check `SupabaseStore.heartbeat_list_lock` (in `shared/store_supabase.py` and its RPC in F's `supabase/migrations/`): does it return True for our own lock whose heartbeat went stale? Write the answer in the commit message. If it returns False, stop and report BLOCKED with what you found. Don't work around it.
- [ ] **Step 4: Run** all offline test files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(offline): flush replays deltas once and stops, keeping the outbox, on a permanent error`.

### Task P6: Startup and list open on the per-run BufferedStore

**Files:**
- Modify: `gui/main_window.py`:
  - `_open_profile_manager` ~1002-1036: build the run's BufferedStore for `Mode.SUPABASE`; `Mode.OFFLINE_BUFFER` over `UnreachableStore`.
  - `_sync_offline_outbox` ~1038-1056: uses the run's store.
  - `_wrap_store_for_list` ~1068 / `_unwrap_store` ~1080: become `begin_list` / `end_list` calls.
  - The lock call ~1366: still before `begin_list`.
  - Lines 260, 300, 2409: managers already get `profile_manager.store`, now the BufferedStore. Verify.
- Modify: `packing_tool/packer_logic.py:~1485` (packing list read through `store.read_json`)
- Test: `tests/test_startup_store.py`, `tests/test_offline_list_open.py` (new)

**Interfaces:**
- Consumes: P3/P4/P5 BufferedStore API.
- Produces:
  - `MainWindow.buffered_store -> BufferedStore | None` (None in file mode).
  - The `begin_list` meta keys: `client_id, session_name, list_name, work_dir_rel, worker_id, worker_name, pc, user`.
  - The snapshot rels: the packing list JSON, `Clients/CLIENT_<id>/packer_config.json`, and the session's `session_info.json`.

- [ ] **Step 1: Check first** whether `packer_logic.py:~1485` already fails online on Supabase: does it `open()` a `local_path` for a table-kind rel? Note the answer for the commit message.
- [ ] **Step 2: Write failing tests:**
  - `test_supabase_mode_wraps_the_store_once` (`open_store` → `(Mode.SUPABASE, FileStore(...))`): `pm.store` is a `BufferedStore` whose `inner` is that store.
  - `test_pending_outbox_flushed_before_start`.
  - `test_pending_outbox_permanent_error_opens_waiting_screen_flag`: a flag or attribute the P8 screen reads, e.g. `self._startup_sync_error`.
  - `test_list_open_binds_list_with_meta_and_snapshot`: start a list through the window's open path with the fake store and assert `bound_list()` has the meta keys.
  - `test_packing_list_read_goes_through_store`.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** Remove `_wrap_store_for_list`/`_unwrap_store` and their callers' isinstance checks; the heartbeat no longer flushes (P8 adds the sync driver). Keep the flush-before-list-start check (~2354).
- [ ] **Step 5: Run** the new tests and the whole P suite. Expected: PASS.
- [ ] **Step 6: Commit** `feat(packer): one buffered store per run; the open list is bound with a restart snapshot`.

### Task P7: Restart offline reopens the list

**Files:**
- Modify: `gui/main_window.py` (`Mode.OFFLINE_BUFFER` startup path; the open-list path gains a "resume from buffer" entry that skips lock acquire and reads identity from `bound_list()`)
- Test: `tests/test_offline_restart.py` (new)

**Interfaces:**
- Consumes: `BufferedStore.pending_meta`, `bound_list`, `finished`, `UnreachableStore`.
- Produces: `MainWindow._resume_offline_list(meta: dict) -> None`, and `MainWindow._show_waiting_to_sync(list_name: str, error: str | None) -> None`. P8 implements the screen; here it may be a stub that sets `self.waiting_list` and that P8 replaces.

- [ ] **Step 1: Write failing tests:**
  - `test_restart_offline_reopens_list_with_progress` (spec test 2). Seed the buffer: a BufferedStore over a FlakyStore server, `begin_list` online with a real packing list fixture (reuse an existing packer fixture from `tests/`; find one with `rg -l "packing_lists" tests | head`), scan/pack 2 items through `PackerLogic`, then `inner.down = True` and pack 1 more. Then build a new window with `open_store` patched to `(Mode.OFFLINE_BUFFER, None)`. The packer screen is open on that list, packed count == 3, no lock acquire was called (spy), and `pm.store.inner` is an `UnreachableStore`.
  - `test_restart_offline_with_finished_list_shows_waiting_screen`.
  - `test_old_outbox_without_list_json_shows_waiting_screen` (Review Focus 3): an outbox line written in the old format, no `list.json`; `waiting_list` is the list name from `BufferedStore.pending`.
  - `test_offline_restart_never_opens_file_mode`: even with a reachable fallback folder, `pm.store` is not a FileStore over it.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** per spec §8.3.
- [ ] **Step 4: Run** the new file and the whole P suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(packer): restart offline reopens the buffered list, never the file share`.

### Task P8: Offline banner, waiting-to-sync screen, sync driver

**Files:**
- Modify: `gui/main_window.py`:
  - heartbeat ~975-995: drop the flush.
  - `check_connection` ~892: in database mode, report reachable without probing `base_path`.
  - End session ~1507 / `_leave_packer_mode` ~1857: show the waiting screen when the outbox is pending.
- Modify: `gui/components/connection_banner.py` (offline variant) or a sibling component if the banner cannot take a second tone cleanly
- Create: `gui/waiting_sync_page.py` (the page, built from existing `gui/components/` empty-state widgets)
- Modify: P catalogue (Bulgarian for every string listed in Global Constraints that P shows)
- Test: `tests/test_offline_ui.py` (new)

**Interfaces:**
- Consumes: `BufferedStore(on_change=...)`, `flush() -> FlushResult`, `sync_error`, `attach`, `db_auth.connect`.
- Produces: signals on MainWindow: `_offline_changed(bool)`, `_sync_done(object)` (FlushResult), `_sign_in_needed()`; `self._sync_timer: QTimer` (60 000 ms).

- [ ] **Step 1: Write failing tests:**
  - `test_offline_banner_shows_and_clears_with_synced_toast`: open a list on a BufferedStore over a FlakyStore; `inner.down = True` and a write. The banner is visible with `"Offline"` / `"Progress is saved on this PC and syncs when the database is back."`. Then `inner.down = False`, run one sync tick synchronously (expose `_sync_tick()` for tests), and the banner is hidden and a `"Synced"` toast is shown.
  - `test_end_session_enabled_while_offline` (database mode; `check_connection` does not disable it).
  - `test_end_session_offline_shows_waiting_screen`: finish the list offline. The waiting page is current with `"Waiting to sync"` and the list name, and navigation to the Sessions page is disabled.
  - `test_waiting_screen_shows_permanent_error`: with `sync_error = "denied"`, the line reads `"Cannot sync offline progress: denied. Nothing is lost; tell the admin."`.
  - `test_sync_now_flushes_and_returns_to_sessions`.
  - `test_sync_tick_with_refused_token_asks_on_ui_thread` (Review Focus 4): the store's inner is an `UnreachableStore` and `db_auth.connect` is patched to raise `SignInCancelled` when its prompt is called from a non-main thread. The tick emits `_sign_in_needed` instead, and the dialog opens on the main thread. Design the tick so the worker thread calls `connect` with a prompt that returns None immediately and signals.
  - `test_sync_timer_runs_while_outbox_pending_and_stops_after`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** per spec §8.4 and §8.5. Theme tokens only, no hardcoded colours. Name the reused component in the commit message.
- [ ] **Step 4: Render check.** Render the banner and the waiting page to PNG with `QT_QPA_PLATFORM=offscreen` and `widget.render(QImage)` in both themes, look at them, and delete the PNGs.
- [ ] **Step 5: Run** the new file, `tests/test_i18n_catalogue.py`, and the whole P suite. Expected: PASS.
- [ ] **Step 6: Commit** `feat(packer): offline banner, waiting-to-sync screen, and a sync timer`.

### Task P9: P gate and push

- [ ] **Step 1:** `.venv/bin/python scripts/coverage_check.py`. Expected: both gates OK, all tests pass.
- [ ] **Step 2:** `.venv/bin/ruff check .`. Expected: clean.
- [ ] **Step 3:** `/usr/bin/git push`. The P PR pins F's head (from `scripts/shared_synced_from.txt`), so merge F first.
