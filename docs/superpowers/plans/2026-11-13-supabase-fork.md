---
tags: [project, plan]
updated: 2026-11-13
status: ready, not started
---
# Supabase + Shopify API Fork Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Private trial forks of Fulfilment and Packer that run on Supabase, fall back to a file share, survive a dropped connection while packing, and can pull orders from a Shopify dev store, using anonymised data only.

**Architecture:** One storage seam, `shared/store.py`, with `FileStore` (today's behaviour) and `SupabaseStore` (tables + Storage + RPC locks). Callers keep the relative server paths they use today, and the store decides where each path lives. Each app picks one mode at startup (`shared/store_mode.py`). The Packer wraps the store in a disk-backed `BufferedStore` for mid-run outages. Fulfilment gets `shopify_tool/order_sources/`, which yields a DataFrame with Shopify CSV export headers, so the analysis code is untouched.

**Tech Stack:** Python 3.11 (CI) / 3.14 (VM), PySide6, pandas, `supabase` (supabase-py v2, brings `httpx`), `keyring`, Supabase CLI (local stack for tests), Postgres SQL migrations, Shopify Admin GraphQL API.

**Spec:** `~/Obsidian/Claude/supabase-fork-design.md` (committed in Task 1 as `docs/superpowers/specs/2026-11-13-supabase-fork-design.md`). Read it before starting. Section numbers like §7.3 refer to it.

**Repos:** `F` = `~/Desktop/Projects/shopify-fulfillment-tool-db`, `P` = `~/Desktop/Projects/packing-tool-db` (both created in Task 1). Never edit the production checkouts `~/Desktop/Projects/shopify-fulfillment-tool` / `packing-tool`.

## Global Constraints

- Anonymised data only. Nothing from `~/Desktop/Server./`, the production share `\\192.168.88.101\_Fulfilment_\0UFulfilment`, or a real Shopify store enters the forks, Supabase or the dev store (§4).
- Supabase project: `ntzqarcksthplcdzivep` (URL `https://ntzqarcksthplcdzivep.supabase.co`). Only the publishable key ships in code. The service-role key is never in the repo or the app.
- RLS on every table, with policies for `authenticated` only and no `anon` access. Storage bucket `files` is private.
- A failed read raises `StoreError`. It never returns empty or default data (§4).
- One mode per run. An app never writes to both backends in one run (§7.3).
- Stale lock = heartbeat older than **120 s**. Heartbeat every **60 s** (today's values).
- Shopify: read-only. The fork's app token has scopes `read_orders`, `read_products` only (§7.4).
- `shared/` is canonical in F. P gets it only via `python scripts/sync_shared.py <F path>`, never by hand-editing (`shared/README.md`).
- Tests: F `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest` (full run ~10 min, run it in the background). P `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q`, gate `python scripts/coverage_check.py`. Gates: F `--cov-fail-under=90` over `shopify_tool gui shared`, P 85% app + 80% per page script.
- Tests that need Supabase use the local stack (`supabase start`) and the marker `@pytest.mark.supabase`. They are skipped when `SUPABASE_TEST_URL` is unset. CI sets it (Task 5).
- Python floor 3.11: no 3.12+ syntax.
- Window title and app name carry the suffix ` (DB trial)`.

## Review Focus

1. **Packer killed while offline with a non-empty outbox, restarted while still offline.** Expected: it reopens that list in offline-buffer mode with its progress, and does not offer file fallback. Pinned in Task 7 (`test_choose_mode_pending_outbox_never_file`) and Task 11 (`test_outbox_survives_reopen`).
2. **Two PCs race for the same list in Supabase mode.** Expected: exactly one gets it. Pinned in Task 6 (`test_acquire_race_one_winner`).
3. **A Supabase read fails (network or RLS) in the middle of building a registry or session list.** Expected: `StoreError` surfaces as an error in the UI, not an empty list. Pinned in Task 7 (`test_read_error_raises_not_empty`).
4. **The Shopify pull returns a line item with no SKU, or an order spanning several pages of line items.** Expected: the row is still emitted with an empty `Lineitem sku` (today's "no-SKU holds its order" rule applies downstream), and every line of a long order is present. Pinned in Task 13 (`test_missing_sku_kept`, `test_line_item_pagination`).
5. **Two clients' stores mixed up.** Expected: a client's pull uses only that client's `order_source`, and pulling for a client with none set says so instead of crashing. Pinned in Task 14 (`test_pull_without_order_source_errors`).

---

## Phase 1: Repos and identity

### Task 1: Create the fork repos and separate their identity

**Files (both repos):**
- Modify: F `gui/main_window_pyside.py:202` (`AppIdentity`), P `gui/main_window.py:471`
- Modify: every `QSettings("<org>", ...)` literal in production code and `tests/conftest.py` (F orgs: `ShopifyFulfillmentTool`, `ShopifyTool`. P org: `PackingTool`)
- Modify: window-title strings, `.github/workflows/build_release.yml` (F) / `build-release.yml` (P) artifact and exe names
- Create: F `docs/superpowers/specs/2026-11-13-supabase-fork-design.md`, `docs/superpowers/plans/2026-11-13-supabase-fork.md` (copies of the vault files)
- Test: F `tests/test_fork_identity.py`, P `tests/test_fork_identity.py`

**Interfaces:**
- Produces: repo slugs `cognitiveghost/shopify-fulfillment-tool-db`, `cognitiveghost/packing-tool-db`. QSettings orgs `ShopifyTool-DB` (F, replaces both F orgs) and `PackingTool-DB` (P). Exe names `FulfilmentTool-DB.exe`, `PackerAssistant-DB.exe`.

- [ ] **Step 1: Create the repos and clones**

```bash
cd ~/Desktop/Projects
for r in shopify-fulfillment-tool packing-tool; do
  gh repo create cognitiveghost/$r-db --private
  git clone https://github.com/cognitiveghost/$r.git $r-db
  git -C $r-db remote rename origin upstream
  git -C $r-db remote add origin https://github.com/cognitiveghost/$r-db.git
  git -C $r-db push -u origin main --tags
  (cd $r-db && bash scripts/setup_venv.sh)
done
```
Expected: both repos exist as private (`gh repo view cognitiveghost/<r>-db --json visibility` gives `PRIVATE`), and `git -C <r>-db remote -v` shows `origin` = `-db` and `upstream` = production. If P has no `scripts/setup_venv.sh`, create `.venv` with `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`. Copy `.git/hooks/post-commit` and `post-merge` from the production checkouts so graphify refreshes.

- [ ] **Step 2: Write the failing identity tests** (each repo)

```python
def test_updater_points_at_fork():
    # find the AppIdentity in the main window source
    src = Path("gui/main_window_pyside.py").read_text()   # P: gui/main_window.py
    assert '"cognitiveghost/shopify-fulfillment-tool-db"' in src  # P: packing-tool-db
    assert '"cognitiveghost/shopify-fulfillment-tool"' not in src

def test_no_production_qsettings_org():
    hits = [p for p in Path(".").rglob("*.py")
            if ".venv" not in p.parts and re.search(r'QSettings\("(ShopifyFulfillmentTool|ShopifyTool|PackingTool)"', p.read_text())]
    assert hits == []
```

- [ ] **Step 3: Run them.** Expected: FAIL.
- [ ] **Step 4: Change `AppIdentity` (repo, app folder name with `-DB`, exe name), every QSettings org literal, the window title suffix, and the CI artifact and exe names.** `shared/server_connection.py` takes `org` as a parameter, so change the callers (`resolve_server_path("ShopifyTool", ...)` → `"ShopifyTool-DB"`).
- [ ] **Step 5: Run the identity tests, then the full suite.** Expected: PASS, full suite green.
- [ ] **Step 6: Copy the spec and this plan into F `docs/superpowers/`. Commit in each repo (`chore: fork identity (DB trial)`) and push.** P: run `python scripts/sync_shared.py ../shopify-fulfillment-tool-db --force` first if F's `shared/` changed.

---

## Phase 2: Database

### Task 2: Schema, RLS and lock functions as migrations

**Files:**
- Create: F `supabase/config.toml` (`supabase init`), `supabase/migrations/20261113000001_core.sql`, `supabase/migrations/20261113000002_locks.sql`, `supabase/migrations/20261113000003_storage.sql`
- Test: F `supabase/tests/rls.sql` (pgTAP, `supabase test db`)

**Interfaces:**
- Produces tables. Every table has `version bigint not null default 1`, `updated_at timestamptz not null default now()`, `updated_by text`, and text keys taken from the path:
  - `clients(client_id text pk, name text, archived bool default false, config jsonb, shopify_config jsonb, packer_config jsonb)`
  - `client_config_history(id bigserial pk, client_id text fk, kind text, body jsonb, saved_at timestamptz default now())`
  - `groups(id int pk default 1 check (id = 1), body jsonb)`
  - `fulfillment_history(client_id text fk, row_no int, order_number text, row jsonb, pk (client_id,row_no))`, with a per-client version kept in `fulfillment_history_meta(client_id pk, version bigint)`
  - `sessions(client_id text, name text, status text, info jsonb, archived bool default false, pk (client_id,name))`
  - `session_documents(client_id text, session_name text, path text, body jsonb, pk (client_id,session_name,path))` (path relative to the session folder, e.g. `analysis/analysis_data.json`)
  - `packing_lists(client_id, session_name, name text, body jsonb, withdrawn bool default false, fingerprint text, pk (client_id,session_name,name))`
  - `packing_state(client_id, session_name, list_name text, state jsonb, summary jsonb, pk (client_id,session_name,list_name))`
  - `list_locks(client_id, session_name, list_name text, holder_pc text, holder_user text, heartbeat_at timestamptz, acquired_at timestamptz, pk (client_id,session_name,list_name))`
  - `workers(id int pk default 1 check (id = 1), body jsonb)`. `workers.json` is stored whole.
  - `stats(id int pk default 1 check (id = 1), body jsonb)`
- Produces RPCs (`security invoker`, callable by `authenticated`):
  - `acquire_list_lock(p_client text, p_session text, p_list text, p_pc text, p_user text, p_stale_seconds int default 120) returns jsonb`: `{"acquired": bool, "holder_pc", "holder_user", "heartbeat_at"}`. Takes the lock only if it's free or the same PC already holds it. A stale holder is **not** taken over here (that needs `force_take_list_lock`, after the packer agrees, as today). One statement with `insert ... on conflict do update ... where`.
  - `force_take_list_lock(p_client, p_session, p_list, p_pc, p_user, p_expected_heartbeat timestamptz, p_stale_seconds int default 120) returns bool`: takes over only if the heartbeat still equals `p_expected_heartbeat` and is stale (today's `force_release_lock` rule).
  - `heartbeat_list_lock(p_client, p_session, p_list, p_pc) returns bool`: false if this PC no longer holds the lock.
  - `release_list_lock(p_client, p_session, p_list, p_pc) returns void`
  - `replace_fulfillment_history(p_client text, p_rows jsonb, p_expected_version bigint) returns bigint`: the new version, or raises `serialization_failure` on a version mismatch.
- Storage: bucket `files` (private). Policies: `authenticated` may select, insert, update and delete in `files`.

- [ ] **Step 1: Install the Supabase CLI on the VM** (`npm i -g supabase` or the `.deb` from GitHub releases; pick an exact version and note it in F `supabase/README.md`), run `supabase init`, then `supabase start`. Expected: a local stack running, and `supabase status` prints an API URL and keys.
- [ ] **Step 2: Write the pgTAP test `supabase/tests/rls.sql`:** for every table above, `anon` select returns 0 rows or is denied, while `authenticated` can insert and select. Lock rules: second `acquire` by another PC → `acquired=false`. Same PC → true. `force_take` with a fresh heartbeat → false. With heartbeat `now()-121s` and the matching expected value → true. `heartbeat` by the old holder after a take-over → false. `replace_fulfillment_history` with the wrong version raises.
- [ ] **Step 3: `supabase test db`.** Expected: FAIL (no tables).
- [ ] **Step 4: Write the three migrations.** Load the `supabase-postgres-best-practices` skill first. Add an index on `list_locks(heartbeat_at)` and a `before update` trigger that bumps `updated_at`.
- [ ] **Step 5: `supabase db reset && supabase test db`.** Expected: PASS.
- [ ] **Step 6: Commit** (`feat(db): schema, RLS, lock RPCs`). The remote project is **not** touched yet. That happens in Task 10.

---

## Phase 3: The store seam with no behaviour change

### Task 3: `shared/store.py` with `FileStore`

**Files:**
- Create: F `shared/store.py`, `tests/shared/test_store_contract.py`
- Modify: F `shared/README.md` (table row)

**Interfaces:**
- Produces:

```python
class StoreError(Exception): ...
class StoreNotFound(StoreError): ...
class StoreConflict(StoreError): ...      # optimistic-version clash after retries

@dataclass(frozen=True)
class LockInfo:
    acquired: bool
    holder_pc: str | None
    holder_user: str | None
    heartbeat_at: datetime | None

class Store:                               # base class, both backends subclass it
    def read_json(self, rel: str) -> Any               # StoreNotFound if absent
    def write_json(self, rel: str, data: Any) -> None   # atomic
    def update_json(self, rel: str, fn: Callable[[Any | None], Any]) -> Any  # atomic read-modify-write, returns new value
    def read_bytes(self, rel: str) -> bytes
    def write_bytes(self, rel: str, data: bytes) -> None
    def exists(self, rel: str) -> bool
    def list_dir(self, rel: str) -> list[str]           # child names, sorted; [] if dir absent
    def move(self, src: str, dst: str) -> None          # withdrawn lists, archive
    def delete(self, rel: str) -> None
    def claim_dir(self, rel: str) -> bool               # True if this call created it (session claim)
    def acquire_list_lock(self, list_dir: str, pc: str, user: str) -> LockInfo
    def force_take_list_lock(self, list_dir: str, pc: str, user: str, expected_heartbeat: datetime) -> bool
    def heartbeat_list_lock(self, list_dir: str, pc: str) -> bool
    def release_list_lock(self, list_dir: str, pc: str) -> None
    def local_path(self, rel: str) -> Path              # a readable local file (download to cache for Supabase); for os.startfile / openpyxl / pd.read_*

class FileStore(Store):
    def __init__(self, root: Path)
```
`rel` is POSIX-style relative to the server root (`Sessions/CLIENT_ALMA/2026-10-09_1/session_info.json`). `list_dir` is the work folder `Sessions/CLIENT_X/<session>/packing/<list>`. Any `..`, absolute path or backslash in `rel` → `ValueError`.

- [ ] **Step 1: Write `tests/shared/test_store_contract.py`** as a test class parametrised by a `store` fixture (only `FileStore(tmp_path)` for now; Task 7 adds Supabase). Tests:
  - `test_json_roundtrip`: write then read gives equal data.
  - `test_read_missing_raises`: `pytest.raises(StoreNotFound)`.
  - `test_update_json_concurrent`: 2 threads × 50 `update_json(rel, lambda d: {"n": (d or {"n":0})["n"]+1})` → `n == 100`.
  - `test_list_dir_sorted_and_missing_empty`.
  - `test_move_then_old_missing`.
  - `test_claim_dir_once`: first True, second False.
  - `test_lock_second_pc_refused`, `test_lock_same_pc_reenters`, `test_force_take_only_when_stale` (patch the clock: heartbeat 121 s old → True, 30 s → False), `test_heartbeat_after_takeover_false`.
  - `test_rejects_bad_rel` for `"../x"`, `"/x"`, `"a\\b"`.
- [ ] **Step 2: Run.** Expected: FAIL (module missing).
- [ ] **Step 3: Implement `FileStore` on today's helpers:** `atomic_write_json`, `replace_with_retry` and `locked_file` for `update_json`. Locks keep today's `.session.lock` JSON file and O_EXCL create. Move the logic out of P's `session_lock_manager.py`, keeping its file format so a production PC and a fork PC agree on the share.
- [ ] **Step 4: Run.** Expected: PASS. Then the full suite.
- [ ] **Step 5: Commit** (`feat(shared): Store seam with FileStore`).

### Task 4: Move Fulfilment's server I/O onto the store

Split into four commits (a–d), each with the full suite green. **No behaviour change**: the existing tests are the spec. Paths below come from the 2026-11-13 scan, so verify each before editing.

**Files:**
- Modify: F `shopify_tool/profile_manager.py` (builds `self.store = FileStore(base_path)`, keeps `base_path` only for `FileStore` and display)
- (a) clients: `profile_manager.py` client config read/write `:539-544, :672, :742, :808-851, :1199-1234`, backups `:1101-1135`, archive `:330-370`, `groups_manager.py:56-57,123,168,217`
- (b) sessions: `session_manager.py:76,105-134,159-160,285-312,356-487,538-802,908-919`, `session_state.py:17-70`, `undo_manager.py:488,521-522`
- (c) outputs: `core.py:526-542,761-810,1353,1381`, `report_jobs.py:65,106-112,209`, `packing_lists.py:61-86`, `barcode_processor.py:334-335,387-388`, `stock_export.py:170-215`
- (d) shared: `fulfillment_history.py:29-95`, `shared/stats_manager.py:104-240`, `shared/guide/help.py:20-35`
- Create: F `tests/test_store_guard.py`

**Interfaces:**
- Consumes: `Store` (Task 3).
- Produces: `ProfileManager.store: Store`. `StatsManager(store: Store, ...)` replaces its path argument (P adopts it in Task 5). `SessionManager` and `GroupsManager` take `store`.

- [ ] **Step 1: Write `tests/test_store_guard.py`:** scan `shopify_tool/`, `gui/` and `shared/` (excluding `shared/store*.py`) for `base_path`, `sessions_root`, `clients_dir`, `stats_dir` used with `/`, `os.path.join`, `open(` or `Path(`. Also flag `atomic_write_json(`, `replace_with_retry(` and `locked_file(` outside `shared/store.py`. Allow-list file `tests/store_guard_allow.txt` (each line `path:reason`) for genuinely local files (user-chosen export targets, temp dirs, `get_persistent_data_path`). Assert no unlisted hits.
- [ ] **Step 2: Run.** Expected: FAIL, listing the call sites.
- [ ] **Step 3: Migrate (a), run the suite, commit `refactor: clients I/O via Store`.** Then (b), (c), (d) the same way. The session-index cache (`session_index.json`) stays as a file in `FileStore` mode. pandas: `pd.read_csv(io.BytesIO(store.read_bytes(rel)))`, and writes go through `to_csv` → bytes → `write_bytes`. openpyxl writes to a `BytesIO`. Label PDFs use `write_bytes` (now atomic). `help.py` opens `store.local_path(rel)`.
- [ ] **Step 4: Guard test PASS, full suite PASS, coverage ≥ 90.** Commit.

### Task 5: Move the Packer's server I/O onto the store; CI runs a Supabase stack

**Files:**
- Modify: P `shared/` (sync from F after Task 4), `packing_tool/profile_manager.py:60-137,354-574`, `worker_manager.py:95-160`, `session_registry_manager.py:45,78,118-170,321,666-701`, `session_manager.py:96-140`, `packer_logic.py:363,653-690,1439,1905-1942`, `progress_publisher.py:70-85`, `session_details.py:41-151`, `session_lock_manager.py` (becomes a thin wrapper over `Store` lock methods), `gui/main_window.py:930-937,1241,1293,1456,1598,1619,2317,2351-2384`
- Create: P `tests/test_store_guard.py` (same rules as F Task 4), `tests/store_guard_allow.txt`
- Modify: F and P CI test jobs: add `supabase/setup-cli@v1` (pin a version), `supabase start` (F's `supabase/` dir; P checks out F's migrations by path or a copy in `P/supabase/migrations` synced by `sync_shared.py`, your choice, but it must be one source), export `SUPABASE_TEST_URL`, `SUPABASE_TEST_ANON_KEY`, `SUPABASE_TEST_SERVICE_KEY` from `supabase status -o env`.

**Interfaces:**
- Consumes: `Store`, `StatsManager(store, ...)`.
- Produces: P `ProfileManager.store: Store`.

- [ ] **Step 1:** `python scripts/sync_shared.py ../shopify-fulfillment-tool-db`. Expected: `shared/store.py` is present in P.
- [ ] **Step 2:** Write the guard test. Run. Expected: FAIL with the list.
- [ ] **Step 3:** Migrate area by area (profile/SKU/workers → registry/session info/progress/details → packer state/summary/locks), suite green after each, one commit each. `read_registry`'s "empty on error" (`session_registry_manager.py:118-135`) becomes: `StoreNotFound` → empty registry, any other `StoreError` re-raised (anti-goal).
- [ ] **Step 4:** Add the CI Supabase steps to both workflows. Push a branch and confirm the test job is green on GitHub (`gh run watch`).
- [ ] **Step 5:** `scripts/coverage_check.py` PASS. Commit.

---

## Phase 4: Supabase backend

### Task 6: `SupabaseStore`, path router and locks

**Files:**
- Create: F `shared/store_supabase.py`, `shared/store_routes.py`, `tests/shared/test_store_routes.py`, `tests/shared/test_store_supabase_locks.py`
- Modify: F `requirements.txt` (`supabase==<exact 2.x>`, `keyring==<exact>`), `shared/README.md`

**Interfaces:**
- Consumes: `Store`, `LockInfo`, `StoreError*` (Task 3). Tables and RPCs (Task 2).
- Produces:

```python
# store_routes.py: pure, no network
@dataclass(frozen=True)
class Route:
    kind: Literal["clients_field", "client_history", "groups", "fulfillment_history",
                  "session_info", "session_doc", "packing_list", "packing_state",
                  "packing_summary", "workers", "stats", "blob", "session_dir"]
    key: dict[str, str]          # e.g. {"client_id": "ALMA", "session_name": "2026-10-09_1", "path": "analysis/analysis_data.json"}
def route(rel: str) -> Route     # ValueError for an unknown JSON path

class SupabaseStore(Store):
    def __init__(self, client: supabase.Client, *, pc: str, cache_dir: Path)
```
Routing rules (from spec §7.2):

| rel pattern | kind |
|---|---|
| `Clients/CLIENT_{c}/client_config.json` / `shopify_config.json` / `packer_config.json` | `clients_field` (`config` / `shopify_config` / `packer_config`) |
| `Clients/CLIENT_{c}/backups/{name}.json` | `client_history` |
| `Clients/groups.json` | `groups` |
| `Clients/CLIENT_{c}/fulfillment_history.csv` | `fulfillment_history` (bytes ⇄ rows via pandas; write uses `replace_fulfillment_history` with the version read) |
| `Sessions/CLIENT_{c}/{s}/session_info.json` | `session_info` |
| `Sessions/CLIENT_{c}/{s}/packing_lists/{n}.json` / `.../withdrawn/{n}.json` | `packing_list` (`withdrawn` flag) |
| `Sessions/CLIENT_{c}/{s}/packing/{l}/packing_state.json` | `packing_state` |
| `Sessions/CLIENT_{c}/{s}/packing/{l}/session_summary.json` | `packing_summary` |
| other `Sessions/CLIENT_{c}/{s}/**.json` | `session_doc` |
| `Sessions/CLIENT_{c}/{s}` (claim_dir / list_dir) | `session_dir` |
| `Workers/workers.json` | `workers` |
| `Stats/global_stats.json` | `stats` |
| any non-JSON (`.csv` except history, `.pkl`, `.xlsx`, `.pdf`, `.xls`) | `blob`: Storage `files/<rel>` |
| `session_index.json`, `registry_index.json` | `ValueError("derived: query instead")`. Tasks 8/9 replace their callers |

`update_json` = read `(body, version)` → `fn` → `update ... where version = v` (bump version), retry up to 5 times with jitter, then `StoreConflict`. `list_dir` answers from table queries (sessions of a client, lists of a session, `packing/` children) plus Storage `list` for blob folders. `move` covers withdraw/restore of packing lists and client/session archive (`archived` flag). Any other move → `ValueError`. Lock methods call the Task 2 RPCs. `local_path` downloads blobs to `cache_dir/<rel>`.

- [ ] **Step 1: `test_store_routes.py`:** one test per row of the table (rel → expected `Route`), plus `test_unknown_json_path_raises` and `test_index_files_refused`.
- [ ] **Step 2:** Run. FAIL. Implement `route()`. PASS.
- [ ] **Step 3: Extend the Task 3 contract fixture:** add `SupabaseStore` against the local stack (fixture signs up a throwaway user via the service key, truncates tables between tests; `@pytest.mark.supabase`). Add `test_store_supabase_locks.py::test_acquire_race_one_winner` (10 threads, distinct pcs, exactly one `acquired=True`).
- [ ] **Step 4:** Run with `SUPABASE_TEST_URL` set. Expected: FAIL. Implement `SupabaseStore`. Expected: the whole contract suite PASSes for both backends.
- [ ] **Step 5:** Commit (`feat(shared): SupabaseStore`).

### Task 7: Sign-in, config and mode selection

**Files:**
- Create: F `shared/db_config.py`, `shared/db_auth.py`, `shared/store_mode.py`, `shared/components/sign_in_dialog.py`, tests `tests/shared/test_store_mode.py`, `tests/shared/test_db_auth.py`
- Modify: F `shopify_tool/profile_manager.py` (store construction), `gui_main.py` (mode at startup, banner)

**Interfaces:**
- Produces:

```python
# db_config.py
SUPABASE_URL = "https://ntzqarcksthplcdzivep.supabase.co"   # env FULFILLMENT_DB_URL overrides
SUPABASE_PUBLISHABLE_KEY = "<from get_publishable_keys>"    # env FULFILLMENT_DB_KEY overrides

# db_auth.py
def connect(app_name: str, prompt: Callable[[], tuple[str, str] | None]) -> supabase.Client | None
    # restores the refresh token from keyring service f"{app_name}-db", else calls prompt() for email/password,
    # stores the new refresh token; None if the user cancels or the project is unreachable (timeout 5 s)

# store_mode.py
class Mode(Enum): SUPABASE = "supabase"; FILE = "file"; OFFLINE_BUFFER = "offline_buffer"; NONE = "none"
def choose_mode(*, outbox_pending: bool, supabase_ok: bool, fallback_ok: bool) -> Mode
def open_store(app_name: str, *, fallback_root: str, cache_dir: Path, prompt, outbox_pending: bool = False) -> tuple[Mode, Store | None]
```
`choose_mode` table: pending + supabase_ok → SUPABASE (the caller flushes first). Pending + not ok → OFFLINE_BUFFER. Not pending + ok → SUPABASE. Not pending + fallback_ok → FILE. Otherwise NONE.

- [ ] **Step 1: `test_store_mode.py`:** all 8 combinations of the three booleans, with exact expected modes, including `test_choose_mode_pending_outbox_never_file` (pending, not ok, fallback_ok → OFFLINE_BUFFER).
- [ ] **Step 2: `test_db_auth.py`:** with a fake keyring (`keyring.set_keyring` with an in-memory backend) and a fake client factory: stored token → no prompt. No token → prompt called once and token saved. Cancel → None. Unreachable → None within 5 s.
- [ ] **Step 3:** Run. FAIL. Implement. PASS.
- [ ] **Step 4:** Wire F startup: `open_store(...)`. FILE → persistent banner `Database unreachable: working from file server`. NONE → today's `prompt_for_recovery_path`. Also add `test_read_error_raises_not_empty`: a `SupabaseStore` whose client raises on select → the Sessions page payload builder raises `StoreError` (and the UI shows the error toast, as today for server errors).
- [ ] **Step 5:** Full suite + gate. Commit (`feat: sign-in and startup mode`).

### Task 8: Fulfilment on Supabase (derived indexes, session claim, archive)

**Files:**
- Modify: F `session_manager.py` (session index → `SupabaseStore` query when the store is Supabase; the claim via `claim_dir` maps to the unique insert), `profile_manager.py` archive and restore → `move` on `Clients/CLIENT_X` / `Sessions/CLIENT_X` (sets `archived`)
- Test: F `tests/test_supabase_flows.py` (`@pytest.mark.supabase`)

**Interfaces:**
- Consumes: `SupabaseStore` (Task 6), `open_store` (Task 7).
- Produces: `Store.query_sessions(client_id: str) -> list[dict]`, added to `Store`. `FileStore` implements it with today's index logic, so callers stop reading `session_index.json` directly.

- [ ] **Step 1:** `test_supabase_flows.py`: create client → create session (two concurrent creates of the same name → one wins) → run `core.analyse` on one ALMA replay fixture → `analysis_data.json` read back equals `expected/`. Withdraw a packing list. Archive the client, then the dropdown query excludes it.
- [ ] **Step 2:** Run. FAIL. Implement. PASS. Full suite + gate. Commit.

### Task 9: Packer on Supabase (registry from tables, locks, progress)

**Files:**
- Modify: P `shared/` (sync), `session_registry_manager.py` (registry from `Store.query_sessions` + `list_dir` + lock info; no `registry_index.json` in Supabase mode), `gui/main_window.py` (startup through `open_store`, heartbeat QTimer calls `store.heartbeat_list_lock`; on `False` → today's "lock lost" handling)
- Test: P `tests/test_supabase_flows.py`

- [ ] **Step 1:** Test: two `SupabaseStore`s (pc `A`, `B`) on one session. A acquires list 1 and B list 2, both write `packing_state`, and the registry shows both in progress. B on list 1 → refused with A's holder info. A's heartbeat stops and the clock moves 121 s → B can `force_take` → A's next heartbeat returns False.
- [ ] **Step 2:** Run. FAIL. Implement. PASS. `coverage_check.py` PASS. Commit.

### Task 10: Remote project, PC logins, fixture loader

**Files:**
- Create: F `scripts/load_fixtures_to_db.py`, `scripts/check_no_real_data.py`, `tests/test_load_fixtures.py`

**Interfaces:**
- `load_fixtures_to_db.py --fixtures tests/replay/fixtures [--client ALMA]`: refuses any path that is not `tests/replay/fixtures` or below it (resolve and compare). Per session: upserts `clients` (config from fixture `meta.json`), the `sessions` row, `input/orders_export.csv` blob, `analysis/memory_baseline.json`, and `input/inventory.csv` built with `core.baseline_stock_df(baseline)`. Idempotent.
- `check_no_real_data.py`: exit 1 if any `Customer` or `Notes` value in `session_documents`, `packing_lists` or `fulfillment_history` is neither the anonymiser's mask (import the mask constants from `tests/replay/anonymise.py`) nor in `scripts/seed_dev_store.py:FAKE_NAMES` (Task 12; until it exists, use an empty tuple). Never reads outside Supabase.

- [ ] **Step 1:** Test (local stack): loader on `tests/replay/fixtures` → 26 sessions, 3 clients. Second run → still 26. `--fixtures /tmp/x` → exit 2 with "refusing". `check_no_real_data` passes after the load and fails after inserting `{"Customer": "Ivan Petrov"}`.
- [ ] **Step 2:** Run. FAIL. Implement. PASS. Commit.
- [ ] **Step 3: Remote** (human-confirmed actions, one at a time): apply the three migrations to `ntzqarcksthplcdzivep` with the Supabase MCP `apply_migration`. Run `get_advisors` (security) and fix anything above "info". Fetch the publishable key into `db_config.py`. Create three Auth users (owner does it in the dashboard: `pc-warehouse-1`, `pc-warehouse-2`, `pc-home`). Run the loader against remote with the owner's sign-in. Run `check_no_real_data.py`. Expected: 26 sessions remote, check exit 0.

---

## Phase 5: Packer offline buffer

### Task 11: Durable `BufferedStore` with outbox and conflict copies

**Files:**
- Create: P `packing_tool/offline_buffer.py`, `tests/test_offline_buffer.py`
- Modify: P `gui/main_window.py` (wrap the store after opening a list; offline indicator through the existing status facts; block "start list" while offline), startup (`outbox_pending` = `BufferedStore.pending(buffer_dir)`)

**Interfaces:**
- Consumes: `Store`, `Mode` (Task 7), lock methods.
- Produces:

```python
class BufferedStore(Store):
    def __init__(self, inner: Store, buffer_dir: Path, *, list_dir: str, pc: str)
    online: bool                                   # flips False on a network StoreError, True after a successful flush
    def flush(self) -> FlushResult                 # pushes outbox in order; on a lost lock writes conflict copy, clears outbox
    @staticmethod
    def pending(buffer_dir: Path) -> str | None    # list_dir of a non-empty outbox, else None
@dataclass
class FlushResult: pushed: int; conflict_rel: str | None
```
`buffer_dir` = `%LOCALAPPDATA%/PackingTool-DB/buffer/` (`QStandardPaths.AppLocalDataLocation`). Reads go to the buffer first, then `inner`. Writes made while offline go to a buffer `FileStore` and are appended to `outbox.jsonl` (`{"op": "write_json"|"update_json_result"|"write_bytes", "rel", "data"|"blob_file"}`, `fsync` after each line). Only writes under `list_dir`, the session's `session_info.json` and `Stats/global_stats.json` are allowed while offline. Anything else raises `StoreError("offline")`. Flush: first `heartbeat_list_lock`. If it returns False, write all buffered state to `Sessions/.../packing/<list>/packing_state_conflict_<pc>_<YYYYmmddHHMMSS>.json` and return `conflict_rel`.

- [ ] **Step 1: Tests** (fake inner store that can be switched offline):
  - `test_offline_write_lands_after_flush`.
  - `test_outbox_survives_reopen`: write offline, drop the object, make a new `BufferedStore` on the same dir, `pending()` returns list_dir, flush pushes.
  - `test_order_preserved`.
  - `test_flush_after_takeover_writes_conflict_copy_and_never_overwrites` (inner `packing_state` unchanged, conflict doc present).
  - `test_offline_write_outside_list_refused`.
  - `test_finish_list_offline_queues_summary_and_stats`.
- [ ] **Step 2:** Run. FAIL. Implement. PASS.
- [ ] **Step 3:** Wire the UI. A flush is tried every heartbeat tick while offline. A conflict shows a persistent message naming the conflict file. At startup with `pending()`: if `choose_mode` → SUPABASE, flush then open normally. If OFFLINE_BUFFER, reopen only that list.
- [ ] **Step 4:** Suite + `coverage_check.py`. Commit (`feat: Packer offline buffer`).

---

## Phase 6: Shopify

### Task 12: Shopify dev stores and seeding (with the owner)

**Files:**
- Create: F `scripts/seed_dev_store.py`, `tests/test_seed_dev_store.py`

**Interfaces:**
- Produces: `FAKE_NAMES: tuple[str, ...]` (≥ 200 entries, generated once and committed, obviously fake e.g. `"Testa Probova"`). `build_seed(client: str, fixtures: Path, n_orders: int, rng_seed: int) -> SeedPlan` (pure: products from the client's fixture SKUs, orders using fixture shipping methods and countries, customers from `FAKE_NAMES`). `push_seed(plan, shop: str, token: str)` uses GraphQL `productCreate` and `orderCreate` (test orders). `write_stock_csv(plan, path)` uses the client's stock headers (`Артикул, Име, Наличност`).

- [ ] **Step 1: Owner, guided:** create a Shopify Partner account → a development store `fulfil-trial-alma` (then `-herbar`) → in each store a custom app **seed** (`write_products, write_orders, read_*`) and a custom app **trial** (`read_orders, read_products`). The tokens go into Proton Pass, never into git or chat logs.
- [ ] **Step 2:** Tests for `build_seed`: deterministic for a seed. Every SKU comes from fixtures. Every customer is in `FAKE_NAMES`. 1–2% of lines have no SKU (exercises the no-SKU rule).
- [ ] **Step 3:** Run. FAIL. Implement. PASS. Run against `fulfil-trial-alma` with 300 orders. Commit.

### Task 13: `order_sources` and the Shopify fetcher

**Files:**
- Create: F `shopify_tool/order_sources/__init__.py`, `shopify_tool/order_sources/shopify.py`, `tests/order_sources/test_shopify.py`, `tests/order_sources/fixtures/*.json` (recorded and anonymised GraphQL pages from the dev store)

**Interfaces:**
- Produces:

```python
# order_sources/__init__.py
SHOPIFY_CSV_HEADERS = ("Name", "Lineitem sku", "Lineitem quantity", "Lineitem name", "Shipping Method",
                       "Shipping Country", "Tags", "Notes", "Total", "Subtotal", "Shipping Name", "Created at")
class OrderSourceError(Exception): ...
def fetch_orders(order_source: dict | None) -> pd.DataFrame   # dispatch on order_source["platform"]; None/unknown → OrderSourceError

# order_sources/shopify.py
API_VERSION = "<pin the current stable, e.g. 2026-10>"
def fetch_shopify_orders(shop: str, token: str, *, transport: httpx.BaseTransport | None = None) -> pd.DataFrame
```
Query `orders(first: 100, query: "fulfillment_status:unfulfilled status:open")`, with cursor pagination on orders **and** on `lineItems` per order. One row per line item. Order-level fields (Name, Shipping Method, Country, Tags, Notes, Total, Subtotal, Shipping Name, Created at) are repeated on every row, matching the CSV export. Throttling: on `THROTTLED`, sleep for the reported restore time and retry (max 5).

- [ ] **Step 1: Tests with `httpx.MockTransport` serving the fixture pages:**
  - `test_headers_exact` (columns == `SHOPIFY_CSV_HEADERS`).
  - `test_one_row_per_line_item`.
  - `test_order_pagination`.
  - `test_line_item_pagination` (an order with 120 lines → 120 rows).
  - `test_missing_sku_kept` (empty string).
  - `test_throttled_retries`.
  - `test_dataframe_passes_validation`: feed it into `core._validate_dataframes` with the default Shopify mapping and expect no error.
- [ ] **Step 2:** Run. FAIL. Implement. PASS. Commit.

### Task 14: "Pull orders" in Fulfilment

**Files:**
- Modify: F the widget and handler that load the orders CSV for a new session (find with `graphify query "load orders csv session start" --budget 1500`), and `profile_manager.py` (`order_source` read and write inside `shopify_config`). Add a field group in Client settings for platform, shop and token.
- Test: F `tests/test_pull_orders.py`

**Interfaces:**
- Consumes: `fetch_orders` (Task 13), `Store` (writes `input/orders_export.csv`).
- Behaviour: "Pull orders" runs `fetch_orders` in a worker, writes the result as CSV to the session's `input/orders_export.csv`, then continues exactly as if that CSV had been chosen. A toast reads `Pulled {orders} orders ({lines} lines) from {shop}`. Pulling again replaces the input.

- [ ] **Step 1: Tests:**
  - `test_pull_writes_input_and_runs_analysis` (fake `fetch_orders`).
  - `test_pull_replaces_previous_input`.
  - `test_pull_without_order_source_errors` (message `No store connected for this client. Set it in Client settings.`, no crash).
  - `test_token_not_logged` (caplog does not contain the token).
- [ ] **Step 2:** Run. FAIL. Implement. PASS. Full suite + gate. Commit.

---

## Phase 7: Trial

### Task 15: Builds, fallback share seeding and the trial checklist

**Files:**
- Create: F `scripts/seed_fallback_share.py` (from `scripts/build_guide_server.py`: fills a given folder from `tests/replay/fixtures`. It refuses the production UNC path and anything under `~/Desktop/Server.`), F `docs/trial-checklist.md` (the §9 Windows checklist as checkboxes, EN)
- Test: F `tests/test_seed_fallback_share.py` (refusal cases + seeded layout matches what `FileStore` reads)

- [ ] **Step 1:** Test, FAIL, implement, PASS, commit.
- [ ] **Step 2:** Tag `v0.1.0-db` in both forks. Confirm the Windows build job produces `FulfilmentTool-DB.exe` and `PackerAssistant-DB.exe` and that the release is on the fork repos only (`gh release list -R cognitiveghost/<r>-db`).
- [ ] **Step 3:** Owner sets up the fallback share and runs `seed_fallback_share.py` on it. Owner installs both builds on the two work PCs and the home PC and works through `docs/trial-checklist.md`. Results go into the vault notes [[shopify-fulfillment-tool]] and [[packing-tool]].

---

## Before any production use (not in this plan)

- Shopify tokens move from `clients.shopify_config` to Supabase Vault or an Edge Function.
- `analysis/current_state.pkl` in Storage is unpickled on every PC. Any signed-in PC could plant code. Replace it with parquet or JSON before real data.
- Per-worker logins and narrower RLS.
