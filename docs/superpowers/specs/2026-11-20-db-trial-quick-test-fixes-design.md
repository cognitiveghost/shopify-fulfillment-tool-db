# DB trial quick-test fixes: barcode lists, Packer resume, tag-undo log errors

Date: 2026-11-20. Size: bounded (three bug fixes across both forks, no new architecture).
Repos: F = `shopify-fulfillment-tool-db` (worktree `.claude/worktrees/dr-56`), P = `packing-tool-db`
(worktree `.claude/worktrees/dr-56`), both on branch `dr/56-db-trial-fixes-from-quick-test-barcode-l`.
Background: `docs/superpowers/specs/2026-11-13-supabase-fork-design.md` (the store seam).

## Why

The owner's quick test of both DB builds on 2026-11-20 found three problems. Two of them block the trial:
no barcodes can be made, and a partly packed list can't be continued. The third is log noise when tag
operations are deleted and then undone.

## Goals

1. A packing list made in a Supabase session shows up in the barcode generator.
2. In DB mode, Resume on an incomplete list opens it with its progress. Progress is never lost or counted twice.
3. A Supabase regression test pins that tag edits and their undo log no errors or warnings (item 3 couldn't be
   reproduced; see §3).

## Non-goals

- Two Packer windows on one PC opening the same list in DB mode. The database lock is held per PC
  (`acquire_list_lock` is re-entrant for the same PC), and Packer has no single-instance guard. This was
  already true before this task, and the owner accepted it (see Decisions).
- Offline (BufferedStore) resume. That flow goes through the outbox and is pinned by `test_outbox_survives_reopen`.
- Any change to file mode. Every fix here must leave FileStore behaviour as it is today.

## Anti-goals

- Fixing the barcode tool by reading the share directly, or adding a DB-only branch in the barcode tool. The
  store contract is that `list_dir` returns what FileStore returns. Fix it in the store.
- A resume that "works" by skipping the lock check. The lock check is what stops two PCs packing one list.

## 1. Barcode generator sees no packing lists

**Cause (verified).** The generator reads through the store: `gui/barcode_tool.py:62-73` calls
`store.list_dir("<session>/packing_lists")` and keeps the names ending in `.xlsx`. In Supabase mode the
`.xlsx` is uploaded as a storage blob (`shopify_tool/packing_lists.py:300` → `write_bytes`, blob route),
and the `.json` sidecar becomes a `packing_lists` row (`shopify_tool/report_jobs.py:216`).
`SupabaseStore._list` answers `…/packing_lists` and `…/packing_lists/withdrawn` from the table only
(`shared/store_supabase.py:558-566`, `.json` names plus `withdrawn`) and never lists the storage blobs. Every
`.xlsx` is missing, so the generator shows nothing. FileStore lists both, so file mode works.

**Fix.** Add `self._storage_ls(rel)` to both branches, as the `…/packing` branch at `:567-570` already does.
`list_dir` de-duplicates and sorts (`:534-536`). This is the contract FileStore already meets, so other
callers that list `packing_lists` already handle `.xlsx` names in file mode.

**Test.** A store-contract test in `tests/shared/test_store_contract.py` (parametrised over FileStore and
SupabaseStore). It writes `packing_lists/DHL.json` (write_json) and `packing_lists/DHL.xlsx` (write_bytes),
and asserts `list_dir` contains both names. It does the same under `packing_lists/withdrawn/`. A barcode-tool
test is not needed: the tool's listing is already tested over FileStore, and this makes the two stores agree.

**Shared module.** The change is in F's `shared/`. P picks it up with `scripts/sync_shared.py` after F's commit
is pushed (P's CI checks P's copy against F at the recorded commit).

## 2. Packer: Resume fails on an incomplete list in DB mode

**Cause (verified).** Resume (`gui/sessions_page.py:308-323`) first calls `_stale_lock(work_dir)`, which calls
`SessionLockManager.is_locked` → `_read` (`packing_tool/session_lock_manager.py:93-138`). `_read` reads the
lock file `<work_dir>/.session.lock` with `store.read_json`. The database has no lock file. The path routes
as a blob (`shared/store_routes.py` has no rule for it), so `SupabaseStore.read_json` raises
`ValueError("not a JSON path")` (`shared/store_supabase.py:230`). `_read` catches only `StoreError`, and
`_stale_lock` catches only `OSError`, so Resume dies before anything opens. Start skips this check, which is
why new lists open and resumed ones don't.

The same `_read` sits behind three other DB-mode paths that are broken in the same way:
- `acquire_lock`'s refusal message (`session_lock_manager.py:182`): a refused acquire can't say who holds the list.
- `force_release_lock` (`:374-433`): takeover of a stale lock from another PC can't run.
- `owns_lock` (`:296-306`), used by `MainWindow._renew_lock` (`gui/main_window.py:1339-1349`). The exception is
  swallowed there, so in DB mode a takeover by another PC is never noticed.

Progress itself is fine. State is written and read under the same key, `packing/<list>/packing_state.json`
(`packing_tool/packer_logic.py:341-342`), which routes to the `packing_state` row.

**Fix: one seam, `SessionLockManager._read`.** When the store is not a `FileStore`, `_read` gets the lock
from the store's own `lock_info(list_dir)` (`shared/store.py:173`, implemented by SupabaseStore and passed
through by BufferedStore) instead of reading a file. It maps the result to the dict the callers already use:

| dict key | value |
|---|---|
| `locked_by` | `info.holder_pc` |
| `user_name` | `info.holder_user` |
| `lock_time` | `info.heartbeat_at.isoformat()` (the store has no acquired time; the heartbeat is the closest honest value) |
| `heartbeat` | `info.heartbeat_at.isoformat()` |
| `process_id`, `worker_id`, `worker_name` | `None` |

- `lock_info` returns `None` when the list is free, so `_read` returns `None`.
- A `StoreError` is retried like today (`LOCK_READ_ATTEMPTS`). When it keeps failing, `_read` returns the
  unreadable-lock dict, with `heartbeat` set to now. It must not call `local_path(...).stat()` in DB mode:
  that would try to download a blob that doesn't exist.

**Ownership in DB mode.** The database lock belongs to a PC. In DB mode, "ours" means `locked_by == hostname`,
whatever the process. `_mine` becomes public as `is_mine(data)` with that rule for non-FileStore stores and
today's rule (hostname and process id) for FileStore. `gui/sessions_page.py:339-342` duplicates the rule
inline. It calls `self._locks.is_mine(lock)` instead. So after a crash on this PC, Resume reopens the list
without a takeover prompt, which matches what `acquire_list_lock` already does.

**Takeover in DB mode.** `force_release_lock` with `expected=None` deletes the lock file today. In DB mode
there's no file, so it uses the lock it just read as `expected`. It then takes over through
`store.force_take_list_lock`, which only succeeds while the heartbeat is still the one shown. The `expected`
comparison (`locked_by`, `process_id`, `heartbeat`) works unchanged on the mapped dicts. The heartbeat string
round-trips at microsecond precision, which is what Postgres `timestamptz` stores.

**Tests (P, `@pytest.mark.supabase`, local stack).**
1. Lock manager over a real SupabaseStore, using P's fixtures (`supabase_client`, `supabase_user`,
   `make_user_client`, `tests/test_supabase_flows.py` `pcs`-style seeding). Free list → `is_locked` is
   `(False, None)`. PC "A" acquires → B's `is_locked` is `(True, {"locked_by": "A", …})`, and A's `is_mine` is
   True. The heartbeat is backdated past 120 s → B sees it as stale, and `force_release_lock(expected=lock)`
   gives B the lock.
2. Resume end to end, which is the owner's test. A PackerLogic over SupabaseStore starts the list, packs part
   of an order, saves and ends the list (summary written, lock released). Then it reopens: the lock manager
   check passes, and a new PackerLogic loads the state. The packed counts equal what was packed, not zero and
   not double. Packing the rest completes the order. It also covers reopening after a crash: the lock was
   never released and the same PC resumes.

## 3. Tag delete/undo log errors

**Not reproduced.** The owner saw errors in the logs "when you delete and undo tags operations". No log lines
reached this run, and the logs are on the Windows PCs. I tried two things:
- A code trace of internal-tag remove, bulk tag remove and undo, through `save_session_state`, the write queue,
  the undo history and fulfilment history in Supabase mode. On success, none of them logs a WARNING or ERROR.
- A throwaway repro, deleted afterwards. The real `MainWindow` ran over a `SupabaseStore` on the local stack.
  Each of the 12 undoable edits in `tests/test_undo_roundtrip.py` `EDITS` (all four tag ones included) ran
  through `actions_handler`, then `undo_last_operation`, and the write queue drained. It logged nothing at
  WARNING or above.

Lines that can fire on failure branches, to match against the owner's logs:
`Background write {kind} failed` (`gui/session_write_queue.py:45`),
`Could not record fulfillment history` / `Fulfillment history kept changing` (`shopify_tool/fulfillment_history.py:159-162`),
`Refused a stale save to …` (`gui/main_window_pyside.py:1016`),
`Undo blocked: operation from session …` (`shopify_tool/undo_manager.py:199`),
`Failed to recalculate statistics` (`gui/main_window_pyside.py:1397`, fires on every edit when the frame lacks
`System_note`). Supabase latency on the real PCs (not on the local stack) is the likeliest difference.

**Scope (owner, 2026-11-20): regression test only.** The repro becomes a permanent F test,
`@pytest.mark.supabase`: the real `MainWindow` over a `SupabaseStore`, the four tag edits from `EDITS`
(`bulk_add_tag`, `bulk_remove_tag`, `add_internal_tag`, `remove_internal_tag`), each followed by
`undo_last_operation`. After the write queue drains, the test asserts no log record at WARNING or above, and
that the frame is back to how it was before the edit. The frame must carry every column analysis requires
(`System_note`, `Shipping_Provider`); `FRAME` in `test_undo_roundtrip.py` doesn't, so add them in the new test.
No product code changes for item 3.

## Decisions

- **Owner:** item 3 is the errors seen when tag operations are deleted and undone (owner's answer to the log
  question, 2026-11-20). It couldn't be reproduced, so it becomes a regression test only (owner's answer to
  the second round).
- **Owner:** design approved, 2026-11-20.
- **Owner:** in DB mode, a lock left by this same PC counts as ours. Resume reopens it silently. Accepted risk:
  two Packer windows on one PC could both open the same list. That was already true at the database level.
- *Claude's call:* the barcode fix goes in the store (`SupabaseStore._list`), not in the barcode tool, because
  the store contract is "list_dir returns what FileStore returns".
- *Claude's call:* the Packer fix goes in `SessionLockManager._read` (one seam for all four broken callers),
  not in each caller.
- *Claude's call:* `lock_time` in DB mode shows the heartbeat time, because the store's `LockInfo` has no
  acquired time. It only appears in messages.

## Left to the builder

Test names, and how each test seeds its session (reuse the existing Supabase fixtures; add no new fixture
modules).
