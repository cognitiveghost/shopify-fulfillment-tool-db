# Supabase B2: honest errors and the full Packer offline flow

Task: dev-runner run 131 (Todoist 6j2vcGwRMX3rCxPM). Target: §7.3 of the fork design
(`~/Obsidian/Claude/supabase-fork-design.md`, mirrored in `docs/superpowers/specs/2026-11-13-supabase-fork-design.md`).
Both forks: `cognitiveghost/shopify-fulfillment-tool-db` (F) and `cognitiveghost/packing-tool-db` (P).
`shared/` changes land in F first; P mirrors them with `python scripts/sync_shared.py <F checkout>`.
Classification: **bounded** (new behaviour inside the B1 architecture; no new tables, no migration).

## 1. Intent

The trial must never lose packing progress and must never tell the user something untrue.
Three things break that today:

1. Every Supabase failure is a plain `StoreError`, so the Packer's offline buffer cannot tell
   "the network is down" (buffer it) from "the database refused this" (an RLS denial, a bad
   payload: buffering it only hides a bug and stalls the outbox).
2. A cancelled or failed sign-in starts the app on the file server under "Database
   unreachable". That is false, and it lets one PC write to the share while the others use the
   database (two sources of truth across PCs).
3. The Packer's offline story stops halfway: finishing a list offline drops stats, worker
   stats and session progress (they are written through the raw store and only logged); a
   restart offline exits with an error; nothing shows the packer they are offline; and the
   "server down" state, which in database mode probes the local cache, disables End session.

Fulfilment also prunes fulfillment history from a local-disk scan, which on Supabase sees only
the local cache: either nothing (pruning skipped) or a partial folder (rows of live sessions
dropped).

## 2. Goals

- `Store` errors split into **unreachable** (network) and everything else (**permanent**).
- In `BufferedStore` only unreachable means offline. A permanent error raises to the caller and
  is never buffered.
- Sign-in says what happened, and never falls into file mode.
- Fulfilment history pruning works the same on both backends.
- Packer: an offline banner; End session works offline; finishing a list offline queues every
  write (summary, packing state, global stats, worker stats, session progress, lock release);
  a restart offline reopens that list with its progress; sync replays the queue without
  overwriting other PCs' edits and without double-counting after a kill.

## 3. Non-goals

- Fulfilment offline buffering (spec §7.3: Fulfilment blocks writes, no buffer).
- Starting a new list offline (spec §7.3: not allowed).
- Any schema/migration change. Deltas reuse `update_json`'s existing compare-and-set.
- Paging, the `check_connection` rework beyond what End session needs, the registry-walk task.

## 4. Anti-goals

- **Silent data loss.** A queued op is never dropped, skipped or reordered. A sync that cannot
  apply an op stops there and keeps the outbox.
- **Whole-document replay of a shared document** (global stats, workers registry,
  session_info). It overwrites other PCs' edits. After B2 the only whole-document replays are
  docs under the open list's folder, which the list lock makes ours.
- **Double counting.** A sync killed after the server committed a delta, then rerun, must
  count it once.
- **File fallback with a pending outbox**, or after a cancelled/failed sign-in.

## 5. Decided by the owner (run 131, question 96)

| Question | Answer |
|---|---|
| Sign-in cancelled or failed | **Never file mode.** The dialog shows the reason inline and lets them retry; Cancel quits the app with a one-line message. File fallback only when the database is unreachable. |
| Offline indicator while a list is open | **Persistent banner** over the packing screen: "Offline: progress is saved on this PC and syncs when the database is back". End session stays enabled; opening other lists is blocked; a "Synced" toast when it clears. |
| After a list is finished offline (or a restart offline finds a finished list waiting) | **Waiting-to-sync screen**: "List {list} is finished and waiting to sync. Retrying every minute. Closing the app is safe." Nothing else opens until it syncs. |
| Sync hits a permanent error | **Stop, keep everything.** Sync stops at that op, the outbox stays as it is, the packer sees "Cannot sync offline progress: {reason}. Nothing is lost; tell the admin." The timer keeps retrying; the op is never skipped. |

The task description fixes the rest: per-manager delta ops applied with read-modify-write on
flush, each with an id the flush can skip; restart offline reopens only that list, never file
fallback; the five tests in §9.

## 6. Design: errors (shared/, F)

### 6.1 `StoreUnreachable`

`shared/store.py`: `class StoreUnreachable(StoreError)`: "the backend could not be reached;
retrying later may succeed". Subclass, so every existing `except StoreError` keeps working.

`shared/store_supabase.py` `_guard` raises `StoreUnreachable` when the caught exception is:
- `httpx.TransportError` (connect/read/write errors, timeouts, protocol errors) or `OSError`;
- `json.JSONDecodeError` / pydantic `ValidationError` (a cut-off response; `_guard` already
  calls these that);
- `postgrest.APIError` or `storage3` `StorageException` carrying an HTTP status ≥ 500, or
  `httpx.HTTPStatusError` with status ≥ 500.

Everything else stays `StoreError` (permanent): 4xx, RLS denial (`APIError` code `42501`, or
an empty-result RLS refusal surfaced as an error), bad payloads, `StoreConflict`,
`StoreNotFound`. Put the test in one helper `_is_unreachable(exc) -> bool` next to `_guard`.
*Builder's call:* how to read the status from `APIError` (its `code`/`details`/`json()`) and
`StorageException` (its dict arg's `statusCode`). Verify against the installed versions; when
the status cannot be read, the error is **permanent**.

`StoreConflict` raised after `CAS_ATTEMPTS` in `update_json` stays permanent (it means
contention, not network).

FileStore: unchanged. `BufferedStore` only ever wraps `SupabaseStore` (or the
`UnreachableStore` stub, §8.2).

### 6.2 `BufferedStore._lost`

`packing_tool/offline_buffer.py` (P): `_lost(e)` returns True (and sets offline) **only** for
`StoreUnreachable`. Any other `StoreError` re-raises from the write and the outbox is not
touched. Offline refusals that `BufferedStore` raises itself (`"offline"`: a write outside the
open list, an `update_json` with no known state) become `StoreUnreachable("offline")`, since
they mean "can't do this while offline".

## 7. Design: sign-in (shared/, F; both apps)

`shared/db_auth.py` `connect(app_name, prompt, factory)`:
- returns a signed-in client;
- returns `None` **only** when the project is unreachable (`_UNREACHABLE`), unchanged;
- raises a new `SignInCancelled(Exception)` when the user cancels the dialog.
- `prompt` becomes `prompt(error: str | None) -> tuple[str, str] | None`. `error` is shown
  inline in the dialog:
  - saved refresh token refused: `_("Your saved sign-in has expired. Sign in again.")`
  - wrong email or password (`AuthApiError`): `_("Wrong email or password. Try again.")`
  - first prompt: `None`.
- The `PROMPT_ATTEMPTS` cap goes: the dialog keeps asking until success, cancel, or
  unreachable. Cancel is the way out.

`shared/components/sign_in_dialog.py`: a word-wrapped error `QLabel` above the buttons, hidden
when `error` is None, coloured with the theme's error token (no hardcoded colour).
`SignInDialog.ask(parent, error=None)`.

`shared/store_mode.py` `open_store`: lets `SignInCancelled` propagate (it is not "unreachable",
so it never reaches `choose_mode`).

Apps: catch `SignInCancelled` at startup, show one message box
(`_("Not signed in")` / `_("The app needs a sign-in to start. Open it again to sign in.")`) and
quit with exit code 0. F: around `open_profile_manager` (`gui/main_window_pyside.py:~72`) /
its caller in `gui_main.py`. P: `_open_profile_manager` (`gui/main_window.py:~1002`) / its
caller. With a pending Packer outbox the outbox simply stays on disk for the next start.

P gets F's file-fallback banner (it shows nothing today): in `Mode.FILE`, a persistent
banner "Database unreachable: working from file server" / "Changes save to the file server.
Restart the app to try the database again." (the same two msgids as F, so the catalogue
already has the Bulgarian). *Claude's call*, from §7.3's "persistent banner".

## 8. Design: Packer offline (P, plus shared/ deltas in F)

### 8.1 Deltas (shared/store.py, F)

A delta is a named, JSON-serialisable read-modify-write:

```python
class Delta:
    """A named update for Store.update_json that can be queued offline and replayed."""
    def __init__(self, kind: str, args: dict, op_id: str | None = None): ...
    def __call__(self, current): ...      # applies DELTAS[kind](current, **args)
    def to_op(self) -> dict: ...          # {"kind", "args", "op_id"}
    @classmethod
    def from_op(cls, op: dict) -> "Delta": ...

DELTAS: dict[str, Callable] = {}
def delta(kind: str): ...                 # decorator registering fn(current, **args) -> new doc
APPLIED_OPS = "applied_ops"; APPLIED_OPS_KEEP = 200
```

`__call__` with an `op_id`: if `op_id` is in `current[APPLIED_OPS]`, return `current`
unchanged (already applied); otherwise apply, then append `op_id` and keep the last
`APPLIED_OPS_KEEP`. Without an `op_id` (every online call) it just applies, and the document
never gains the key. The id lives **in the document it guards**, so the server commits the
change and the id in one compare-and-set: that is what makes a killed-and-rerun flush count
once. `# ponytail:` the ring holds the last 200 replayed ids per document; only offline
replays add to it, so a rerun within 200 offline deltas on that doc is safe.

Registered deltas, each the body of today's update closure moved to a module-level function
(behaviour identical online):

| kind | module | doc (rel) | today's code |
|---|---|---|---|
| `stats.record_packing` | `shared/stats_manager.py` (F) | `STATS_REL` | `StatsManager.record_packing` → `_atomic_update` (defaults + `last_updated` included) |
| `workers.update_stats` | `packing_tool/worker_manager.py` (P) | `Workers/workers.json` (`REGISTRY`) | `WorkerManager.update_worker_stats` → `_update_registry` (incl. `version`) |
| `session_info.packing_progress` | `packing_tool/session_manager.py` (P) | `<session>/session_info.json` | `SessionManager.update_session_metadata`'s `edit` |

Each manager now calls `store.update_json(rel, Delta(kind, args))`. Timestamps
(`last_updated`, `updated_at`, `started_at`, the stats record's time) are computed **when the
delta is created** and passed in `args`, so a replay records when the packer finished, not when
the sync ran. `update_session_metadata`'s `store.exists` pre-check is replaced by the delta
raising `StoreNotFound` on a missing doc (online: logged as today).

Registration happens at import; P imports the three modules at startup, before any flush.

### 8.2 `BufferedStore` (P)

One `BufferedStore` per run in database mode (`Mode.SUPABASE` and `Mode.OFFLINE_BUFFER`),
handed to `ProfileManager` at startup, so `StatsManager`, `WorkerManager` and `SessionManager`
all write through it. This replaces `_wrap_store_for_list` / `_unwrap_store`. File mode has no
`BufferedStore` (today's behaviour).

- `BufferedStore(inner, buffer_dir, *, pc)`; `begin_list(list_dir, meta, snapshot_rels)`
  binds the open list; `end_list()` unbinds it when the outbox is empty. With no list bound,
  offline writes raise `StoreUnreachable("offline")`. `begin_list` for another list while the
  outbox holds ops raises `StoreError` (today's check in `__init__`, moved).
- **`list.json`** (in `buffer_dir`, written atomically by `begin_list`): `list_dir`, `client_id`,
  `session_name`, `list_name`, `work_dir_rel`, `worker_id`, `worker_name`, `pc`, `user`. What a
  restart needs to reopen the list with no database.
- **Snapshot**: `begin_list` (online) copies `snapshot_rels` from `inner` into
  `buffer_dir/snapshot/`: the packing list JSON (`<session>/packing_lists/<list>.json`),
  `Clients/CLIENT_X/packer_config.json` and `<session>/session_info.json`. Reads that get
  `StoreUnreachable` from `inner` fall back to the snapshot copy when one exists. The snapshot
  is read-only and never queued.
- The packing list is read with a plain `open()` today (`packing_tool/packer_logic.py:~1485`)
  over `local_path`, which never downloads a table-kind rel in Supabase mode. It must go
  through `store.read_json` so the snapshot fallback applies. *Builder verifies* first whether
  online Supabase mode already fails there; if so, say so in the PR (it is a B1 bug fixed here).
- **Allow-list** for offline writes: the open list's folder, plus exactly `STATS_REL`,
  `REGISTRY` and the session's `session_info.json`, and on those three only a `Delta`
  `update_json` is buffered. A plain `write_json`/closure `update_json` on them offline raises
  `StoreUnreachable("offline")`: no whole-document replay of a shared doc. Under the list
  folder, whole-document ops stay as today (the lock makes them ours).
- **Outbox ops** (one JSON line each, fsync, as today):
  `write_json` / `update_json_result` / `write_bytes` (unchanged), plus
  `{"op":"delta","rel","list_dir","kind","args","op_id"}` (op_id = `uuid4().hex`, made when
  queued) and `{"op":"release_lock","list_dir"}`. Offline `release_list_lock` for the bound list
  queues `release_lock` instead of raising. A queued `release_lock` is what "this list is
  finished" means.
- Deltas do not update `state/`: nothing reads stats or the workers registry offline, and the
  packing screen shows progress from `packing_state`.
- **`flush()`**, under the mutex as today:
  1. no ops: online, done;
  2. `inner.heartbeat_list_lock(list_dir, pc)`. Unreachable: stay offline. Not ours: conflict
     copy (as today; deltas go into the copy under `"deltas"`, not applied). *Builder verifies*
     that `SupabaseStore.heartbeat_list_lock` returns True for a lock we hold whose heartbeat
     went stale while offline and that nobody took (the rule is "holder == pc"). If it does not,
     that is a bug to fix in the RPC's caller, not by skipping the check.
  3. replay in order: writes as today; `delta` → `inner.update_json(rel, Delta.from_op(op))`;
     `release_lock` → `inner.release_list_lock`.
  4. On `StoreUnreachable`: offline, `_rewrite_outbox(ops[pushed:])` (today).
     On any other `StoreError`: stop, **leave the outbox file as it is**, set
     `self.sync_error = str(e)`, stay offline, return `FlushResult(pushed, error=str(e))`.
     A rerun replays from the top: whole-doc writes are idempotent and deltas skip by `op_id`.
  5. Success: clear outbox, `state/`, `snapshot/` and `list.json`; online; `sync_error = None`.
- `on_change` callback (optional ctor arg) called with `online: bool` whenever the flag flips;
  the UI wraps it in a queued Qt signal (it can fire on the sync thread).
- `UnreachableStore(Store)`: every method raises `StoreUnreachable("database not connected")`.
  `BufferedStore.attach(inner)` swaps it for a real `SupabaseStore` once a sign-in succeeds.

### 8.3 Packer startup (P, `gui/main_window.py` `_open_profile_manager`)

- `Mode.SUPABASE`: build the run's `BufferedStore` over the `SupabaseStore`. Pending outbox:
  flush first. Conflict: today's message box. Permanent error: open the waiting-to-sync screen
  with the error. Success: normal start.
- `Mode.OFFLINE_BUFFER` (outbox pending, database unreachable): `BufferedStore` over
  `UnreachableStore`, `ProfileManager(config_path, store=that)`. Read `list.json`:
  - queued `release_lock` → the **waiting-to-sync screen**;
  - otherwise → **reopen the packing screen** for that list from `list.json` (client, session,
    list, work dir, worker) and the buffer (`packing_state` from `state/`, packing list and
    packer config from `snapshot/`), **skipping lock acquire** (flush checks ownership), with
    the offline banner.
  Never file mode.
- Missing or unreadable `list.json` with a pending outbox (an outbox written before B2): show
  the waiting-to-sync screen with the list name from the outbox (`BufferedStore.pending`).
- The sync timer (§8.5) starts in both modes whenever the outbox is pending.

### 8.4 Packer UI (P)

- **Offline banner**: reuse `gui/components/connection_banner.py` `ConnectionBanner` with new
  text and the warning tone (title `_("Offline")`, message
  `_("Progress is saved on this PC and syncs when the database is back.")`, no Retry button, or
  a "Sync now" that triggers a flush). Shown while `BufferedStore.online` is False and a list
  is open; hidden on the flip back, with a toast `_("Synced")`. Says which component was
  reused in the PR.
- **End session** stays enabled offline. In database mode `check_connection` no longer probes
  `base_path` (it is the local cache there); it reports reachable, so the "server down" state
  never disables End session in database mode. File mode: unchanged.
- **Opening other lists is blocked** while the outbox is pending: today's flush-before-start
  check (`~2354`, toast "This PC has offline progress that is not synced yet.") stays.
- **Waiting-to-sync screen**: a page in the shell's stack. Title `_("Waiting to sync")`; body
  `_("List {list} is finished and waiting to sync. Retrying every minute. Closing the app is safe.")`;
  a `_("Sync now")` button; when `sync_error` is set, an error line
  `_("Cannot sync offline progress: {reason}. Nothing is lost; tell the admin.")`. Navigation
  away is disabled while it shows. On a successful flush: toast `_("Synced")`, go to the
  sessions page. *Builder's call*: layout, from existing empty-state components in `gui/components/`.
- **End session offline**: the existing `_do_slow_writes` runs unchanged against the buffered
  store (summary, packing_state, stats/worker/session deltas, registry no-op in derived mode,
  lock release queued). Then, if the outbox is pending, show the waiting-to-sync screen
  instead of the shell.
- All new strings in `_()`, Bulgarian added to P's catalogue (P's i18n guard tests).

### 8.5 Sync driver (P)

One 60 s `QTimer` that runs while the outbox is pending, whether or not a list is open. It
replaces the flush inside the lock heartbeat. Each tick runs on a worker thread (no UI calls
there): if the inner store is `UnreachableStore`, try `db_auth.connect` with the saved token
first (no prompt on the thread; if the saved token is refused, emit a signal and the UI thread
shows the sign-in dialog) and `attach` the new `SupabaseStore`; then `flush()`. Results reach
the UI through signals: synced, conflict (today's message box), error (waiting screen line /
banner text). One flush at a time (today's `_heartbeat_busy` guard pattern).

## 9. Design: Fulfilment history pruning (F)

`shopify_tool/fulfillment_history.py` `session_folders(store, client_rel, current)` lists
`store.list_dir(f"Sessions/CLIENT_{client_id}")` instead of `os.scandir` on a local path.
Same rules: `None` when the listing fails (any `StoreError`) or lacks `current`.
`shopify_tool/core.py:~1342` passes the store and the client's sessions rel.

*Claude's call, departing from the task text* ("route it through `Store.query_sessions`"):
`query_sessions` drops archived sessions and sessions without `session_info`, which still have
folders, so pruning by it would delete their history rows. `list_dir` returns exactly what the
file listing returns on both backends (SupabaseStore: non-archived `sessions` rows; FileStore:
the folder's entries; non-session entries like `session_index.json` only make pruning keep
more). The intent, pruning through the Store, is kept.

## 10. Tests to pin

All BufferedStore tests use a `FileStore` as the server, wrapped in a small switchable fake
(`online` toggle raising `StoreUnreachable`, an injectable permanent error, a "kill after
commit" hook raising `KeyboardInterrupt` after a given op commits). That is enough: deltas run
through the same `update_json` CAS contract both backends implement.

1. **Permanent error not buffered**: inner raises `StoreError` on `write_json`, so the call
   raises, the outbox is empty and `online` stays True. Same with `StoreUnreachable`: buffered.
   Plus `_is_unreachable` classification: `httpx.ConnectError`, `ReadTimeout`, APIError/
   StorageException 5xx → unreachable; APIError `42501`, 400, 404 → not. One
   `@pytest.mark.supabase` test in F: an anon/unauthenticated write to a table raises
   `StoreError` that is **not** `StoreUnreachable`.
2. **Restart offline reopens the list with its progress**: P window test. Buffer seeded by
   a `BufferedStore` session (`begin_list` online, pack some, go offline, pack more); new
   window with `open_store` patched to `(Mode.OFFLINE_BUFFER, None)`; the packing screen is open
   on that list with the offline banner and the packed counts from before the "restart".
3. **Finish offline then flush gives the same totals as online**: finish list A online on
   server S1 and offline-then-flush on server S2 with identical inputs; global stats, workers
   registry and session_info equal, ignoring `applied_ops`.
4. **Another PC's edit survives**: finish offline; then on the server directly
   `record_packing` for another client/worker (the other PC); flush; both contributions present.
5. **Killed flush counts once**: kill after the stats delta commits; new `BufferedStore` over
   the same buffer dir; flush; stats `total_sessions` and worker counts incremented once.
6. Sign-in: `connect` raises `SignInCancelled` on cancel; passes the right `error` text after a
   refused token and after a wrong password; `None` on unreachable. Startup quits with the
   message on `SignInCancelled` (one test per app).
7. Permanent error mid-flush: the outbox file is byte-identical afterwards, `sync_error` set,
   rerun after the cause is removed succeeds and counts once.
8. History: `session_folders` over a `SupabaseStore` (supabase-marked) and over a FileStore,
   and a run on Supabase prunes a deleted session's rows but not an archived one's.

## 11. Order of work and PRs

1. F PR: §6.1, §7 (shared + F startup), §8.1 (`Delta` + `stats.record_packing`), §9, tests.
2. P PR (pins F's head; merge F first): `sync_shared.py`, §6.2, §7 P startup + file banner,
   §8.1 P deltas, §8.2–8.5, tests.

## 12. Left to the builder

The exact status reading in `_is_unreachable`; the waiting-screen layout; whether the banner's
action is "Sync now" or none; the names of new private helpers; the fake-store test helper's
shape. Each choice goes in the PR description.
