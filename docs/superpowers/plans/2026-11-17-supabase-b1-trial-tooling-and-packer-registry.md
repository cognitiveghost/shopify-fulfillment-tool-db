# Supabase B1: Trial Tooling and Packer Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trial tooling in the Fulfilment fork (26-session fixture loader, real-data check, dev-store seeder,
fallback-share seeder, trial checklist) and a Packer registry built from one `Store.packing_overview` query.

**Architecture:** Scripts under F `scripts/` stay thin over existing code (`build_guide_server`, `SupabaseStore`,
`anonymise`, `order_sources.shopify.API_VERSION`). `packing_overview` is a new `Store` method in F `shared/store.py`
(FileStore walks files, SupabaseStore does five paged selects); the Packer gets it through `scripts/sync_shared.py`
and uses it only in derived (Supabase) mode.

**Tech Stack:** Python 3.11 floor, pandas, httpx (`MockTransport` in tests), supabase-py v2, pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-supabase-b1-trial-tooling-and-packer-registry-design.md` (F repo).
Read it before any task; section ids (A1…B3) below refer to it.

**Repos / worktrees (stay in them):**
- `F` = `/home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-48`
- `P` = `/home/gloopy/Desktop/Projects/packing-tool-db/.claude/worktrees/dr-48`
- Branch in both: `dr/48-supabase-b1-trial-tooling-and-packer-reg`.

## Global Constraints

- Anonymised data only. Nothing from `~/Desktop/Server.`, `~/Desktop/production info/`, the production share
  `\\192.168.88.101\...` or a real Shopify store. No token, password or service key in git, test output or logs.
- No migrations; nothing is applied to remote project `ntzqarcksthplcdzivep`. No live Shopify call.
- A failed read raises `StoreError`; never an empty or partial result.
- `shared/` is edited in F only. P gets it only through `python scripts/sync_shared.py <F path>` (a hook blocks
  hand edits to `P/shared/`).
- Python 3.11 syntax floor. No `pyproject.toml`. Keep imports clean (ruff).
- Git: `/usr/bin/git`, one plain git command per Bash call, commit with `git commit -F <absolute msg file>`.
  The pytest-guard hook blocks Bash text containing "pytest" except the plain run form; write test files with
  Write/Edit. Messages end with the `Co-Authored-By` trailer the session gives you.
- Tests that need Supabase: `@pytest.mark.supabase`, fixture `supabase_client` (tests/conftest.py). Run F tests
  through `scripts/run_tests.sh` (it exports the local stack's keys). The local stack runs from the F main checkout;
  never `supabase start` a second one. These tests wipe the local stack's data (expected).
- F run one test: `scripts/run_tests.sh tests/<file>.py -q -k <name>`. F gate: `scripts/run_tests.sh -q` (≈10 min,
  run it in the background) and `.venv/bin/ruff check .`.
- P run: `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/<file>.py`; P gate: the full `pytest -q` plus
  `.venv/bin/python scripts/coverage_check.py`. If `.venv` is missing in a worktree: `./scripts/setup_venv.sh`
  (F) or `ln -s <main checkout>/.venv .venv`.

## Review Focus

1. **`--fixtures` pointed at a symlink or `..` path that escapes `tests/replay/fixtures`** → refused (resolve first).
   Pinned in Task 1 (`test_fixtures_refuses_escaping_path`).
2. **`seed_fallback_share --reset` on a folder with real files and no marker** → refused, nothing deleted.
   Pinned in Task 4 (`test_reset_without_marker_refuses_and_keeps_files`).
3. **A Supabase read fails mid-overview** → `StoreError` reaches the Sessions page, not a shorter list.
   Pinned in Task 6 (`test_packing_overview_api_error_raises`) and Task 9
   (`test_derived_overview_error_propagates`).
4. **A withdrawn list that was already started, or an archived session, still has `packing_state` / `list_locks`
   rows** → it does not show in the registry. Pinned in Task 6 (`test_packing_overview_skips_withdrawn_and_archived`).
5. **A real name in lowercase `notes` of a packing list** → `check_no_real_data` fails. Pinned in Task 3
   (`test_walker_matches_keys_case_insensitively`).

---

## Fulfilment fork (`F`)

### Task 1: Fixture mode for the loader + stock CSV helper (spec A1, A4)

**Files:**
- Modify: `F/scripts/load_fixtures_to_db.py`
- Test: `F/tests/test_load_fixtures.py` (extend; keep the two existing tests)

**Interfaces:**
- Produces: `FIXTURES: Path` (= `REPO / "tests" / "replay" / "fixtures"`),
  `build_fixture_server(root: Path, fixtures: Path = FIXTURES, client: str | None = None) -> int`,
  `stock_csv_bytes(df: pd.DataFrame, config: dict) -> bytes`, `main(argv: list[str] | None = None) -> None`,
  existing `load(store, root) -> int` and `_client()` unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_load_fixtures.py`:
  - `test_fixture_server_has_26_sessions_in_3_clients(tmp_path)`: `n = build_fixture_server(tmp_path / "s")`;
    `n == 26`; `FileStore(tmp_path / "s")` `query_sessions` lengths for `ALMA, HERBAR, LIDA` sum to 26 and each is
    non-empty; every session has `input/orders_export.csv`, `input/inventory.csv`, `analysis/memory_baseline.json`;
    `session_info.json["pc_name"] == "fixture-loader"` and `session_name` equals the folder name.
  - `test_fixture_server_is_deterministic(tmp_path)`: two builds into two dirs → identical relative file sets and
    identical bytes for every file under `Sessions/` (client profile files may carry timestamps; compare only
    `Sessions/`).
  - `test_fixture_server_one_client(tmp_path)`: `client="HERBAR"` → `Sessions/` holds only `CLIENT_HERBAR`, 6
    sessions.
  - `test_stock_csv_uses_client_headers(tmp_path)`: with the ALMA config (`tests/replay/fixtures/ALMA/config/*.json`)
    and `core.baseline_stock_df` of an ALMA `memory_baseline.json`, the header row is `Артикул,Име,Наличност`, and
    `pd.read_csv(BytesIO(bytes)).rename(columns=inverse_of_mapping)` has the same SKUs and stocks as the frame.
  - `test_fixtures_refuses_outside_path(tmp_path, monkeypatch, capsys)`: monkeypatch
    `load_fixtures_to_db._client` to `pytest.fail`; `main(["--fixtures", str(tmp_path)])` raises `SystemExit` with
    code 2; stderr contains `refusing`.
  - `test_fixtures_refuses_escaping_path(...)`: same with `str(FIXTURES / ".." / "..")`, and with a symlink in
    `tmp_path` pointing at `FIXTURES.parent`.
  - `@pytest.mark.supabase test_fixture_load_into_supabase_is_idempotent(supabase_client, tmp_path)`: build into
    `tmp_path/"s"`, `load(SupabaseStore(supabase_client, pc="t", cache_dir=tmp_path/"c"), tmp_path/"s")` twice;
    `query_sessions` summed over the three clients is 26 after each run.

- [ ] **Step 2: Run** `scripts/run_tests.sh tests/test_load_fixtures.py -q` → the new tests FAIL (ImportError).

- [ ] **Step 3: Implement** in `scripts/load_fixtures_to_db.py`:
  - `stock_csv_bytes`: inverse of `config["column_mappings"]["stock"]` restricted to `SKU, Product_Name, Stock`,
    `df.rename(...).to_csv(index=False).encode("utf-8")`.
  - `build_fixture_server` per spec A1: client folders = sub-dirs of `fixtures` having `*/meta.json`
    (or `fixtures` itself if it has `*/meta.json`); profiles through `ProfileManager(base_path=str(root))`
    `create_client_profile(cid, cid)` + `save_shopify_config(cid, config, backup=False)` with
    `inventory_memory.enabled = True` (reuse `build_guide_server._latest_config` logic: import `_read`, or copy the
    two lines); session files written directly with `Path.write_text/write_bytes` (json `ensure_ascii=False,
    indent=2`), keys of `session_info.json` exactly as `shopify_tool/session_manager.py:144-163` with the spec's
    overrides.
  - `main(argv)`: `argparse` with `--fixtures` and `--client`. Without `--fixtures`: today's guide-server path.
    With it: `p = Path(arg).resolve()`; refuse unless `p == FIXTURES.resolve()` or (`p.is_relative_to(FIXTURES
    .resolve())` and `any(p.glob("*/meta.json"))`) → `print(f"refusing: {arg} is not under tests/replay/fixtures",
    file=sys.stderr); sys.exit(2)` before `_client()`. Then build into a temp dir and `load`.
  - Docstring: both modes, the default mode adds today's sessions on every run, a full dataset is both runs, and
    the local reset lives in `docs/trial-checklist.md`.

- [ ] **Step 4: Run** the same command → PASS (supabase test runs if the local stack is up).

- [ ] **Step 5: Commit** `feat(scripts): load the 26 replay fixture sessions into Supabase`.

### Task 2: Dev-store seeder (spec A3)

**Files:**
- Create: `F/scripts/seed_dev_store.py`
- Test: `F/tests/test_seed_dev_store.py`

**Interfaces:**
- Consumes: `stock_csv_bytes` (Task 1), `shopify_tool.order_sources.shopify.API_VERSION`.
- Produces: `FAKE_NAMES: tuple[str, ...]`, `@dataclass SeedPlan(client: str, products: list[dict],
  orders: list[dict], stock: dict[str, int])`, `build_seed(client: str, fixtures: Path, n_orders: int,
  rng_seed: int) -> SeedPlan`, `push_seed(plan: SeedPlan, shop: str, token: str,
  transport: httpx.BaseTransport | None = None) -> None`, `write_stock_csv(plan: SeedPlan, path: Path,
  config: dict) -> None`, `main(argv: list[str] | None = None) -> None`.
  Product dict: `{"sku": str, "title": str}`. Order dict: `{"name": str, "country": str, "shipping_method": str,
  "lines": [{"sku": str | None, "title": str, "quantity": int}]}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_fake_names_count_and_unique`: `len(FAKE_NAMES) >= 200` and `len(set(FAKE_NAMES)) == len(FAKE_NAMES)`.
  - `test_build_seed_is_deterministic`: two calls `("ALMA", FIXTURES, 50, 7)` equal; seed 8 differs.
  - `test_build_seed_uses_fixture_skus_and_fake_names`: every product SKU and every non-None line SKU is in the set
    of `Lineitem sku` values from `tests/replay/fixtures/ALMA/*/orders.csv`; every order `name` in `FAKE_NAMES`;
    every `country` / `shipping_method` occurs in those files' `Shipping Country` / `Shipping Method`.
  - `test_build_seed_no_sku_share`: 300 orders → share of lines with `sku is None` in `[0.01, 0.02]`.
  - `test_push_seed_sends_products_then_orders`: `httpx.MockTransport` handler records JSON bodies and returns
    `{"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1"}, "userErrors": []}}}` or the
    `orderCreate` equivalent by inspecting the query text; plan of 3 products/2 orders → 3 `productCreate` then 2
    `orderCreate` requests, every request has header `X-Shopify-Access-Token: tok` and URL
    `https://x.myshopify.com/admin/api/{API_VERSION}/graphql.json`.
  - `test_push_seed_raises_on_user_errors`: handler returns `userErrors: [{"field": ["title"], "message": "bad"}]`
    for the first product → `RuntimeError` naming that SKU; no further request is sent.
  - `test_write_stock_csv_headers(tmp_path)`: ALMA config → header line `Артикул,Име,Наличност`, one row per product.
  - `test_main_refuses_non_myshopify_shop(monkeypatch)`: `SHOPIFY_SEED_TOKEN=t`, `main(["--client", "ALMA",
    "--shop", "example.com"])` → `SystemExit` code 2; and without the env var → `SystemExit` code 2 mentioning
    `SHOPIFY_SEED_TOKEN`. Neither sends a request (pass a transport-free path by monkeypatching `push_seed` to fail).

- [ ] **Step 2: Run** `scripts/run_tests.sh tests/test_seed_dev_store.py -q` → FAIL.

- [ ] **Step 3: Implement** per spec A3. `FAKE_NAMES = tuple(f"{f} {l}" for f in _FIRST for l in _LAST)` with ≥ 20
  first and ≥ 10 last obviously fake words (e.g. `Testa`, `Proba`, `Fiktiva` / `Probova`, `Testova`). Mutation text:
  check `productCreate` and `orderCreate` input shapes for Admin API `2026-10` (context7 / web); orders are test
  orders with `sendReceipt: false`, `sendFulfillmentReceipt: false`; a line with `sku=None` is a custom line item
  (title + price, no variant). Stock per product: `rng.randint(0, 200)`. CLI flags per spec; token only from
  `os.environ["SHOPIFY_SEED_TOKEN"]`, never printed.

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(scripts): seed_dev_store builds and pushes fake orders`.

### Task 3: Real-data check (spec A2)

**Files:**
- Create: `F/scripts/check_no_real_data.py`
- Test: `F/tests/test_check_no_real_data.py`

**Interfaces:**
- Consumes: `FAKE_NAMES` (Task 2), `_client`, `build_fixture_server`, `load` (Task 1);
  `mask_text`, `rule_words` from `tests/replay/anonymise.py` (import with
  `sys.path.insert(0, str(REPO / "tests" / "replay"))` then `from anonymise import ...`, as
  `scripts/build_replay_fixtures.py` does); `SupabaseStore._all` for paging.
- Produces: `accepted(value, placeholder: str, words: list[str]) -> bool`,
  `person_values(doc) -> Iterator[tuple[str, object]]` (yields `(key, value)` for every key named `customer` or
  `notes`, case-insensitive, any depth; `system_note` is not matched), `offenders(client) -> list[str]`
  (`"<table or blob path>: <key>=<value>"`), `main() -> None` (exit 1 with up to 20 offenders, else print `ok` and
  exit 0).

- [ ] **Step 1: Write the failing tests:**
  - `test_accepted_rule`: with `words=["VIP"]`: `accepted("", "Customer", w)`, `accepted("nan", ...)`,
    `accepted("Customer", "Customer", w)`, `accepted("VIP", "Note", w)`, `accepted(FAKE_NAMES[0], "Customer", [])`
    are True; `accepted("Ivan Petrov", "Customer", w)` and `accepted("call Ivan VIP", "Note", w)` are False.
  - `test_walker_matches_keys_case_insensitively`: on `{"orders": [{"Customer": "a", "notes": "b",
    "system_note": "c", "x": {"NOTES": "d"}}]}` yields exactly `("Customer","a"), ("notes","b"), ("NOTES","d")`.
  - `@pytest.mark.supabase test_check_passes_after_fixture_load(supabase_client, tmp_path)`: build + load the
    26 sessions (Task 1 functions); `offenders(supabase_client) == []`.
  - `@pytest.mark.supabase test_check_fails_on_a_real_name(...)`: after the load, `SupabaseStore(...).write_json(
    "Sessions/CLIENT_ALMA/2026-09-30_6/packing_lists/X.json", {"orders": [{"order_number": "1",
    "Customer": "Ivan Petrov"}]})` → one offender containing `Ivan Petrov`.
  - `@pytest.mark.supabase test_check_reads_order_csvs(...)`: after the load, overwrite one session's
    `input/orders_export.csv` with a copy whose `Shipping Name` cell is `Ivan Petrov` → offender found.

- [ ] **Step 2: Run** `scripts/run_tests.sh tests/test_check_no_real_data.py -q` → FAIL.

- [ ] **Step 3: Implement** per spec A2. Rule words per client from `clients.shopify_config` (one select of
  `client_id, shopify_config`). Tables: `session_documents.body`, `packing_lists.body`, `fulfillment_history.row`,
  each through `SupabaseStore._all(lambda: client.table(t).select("client_id,<col>", count="exact")
  .order("client_id"))`. Blobs: for every `sessions` row, `SupabaseStore(client, pc="check", cache_dir=tmp)
  .read_bytes(f"Sessions/CLIENT_{cid}/{name}/input/orders_export.csv")` (skip `StoreNotFound`), `pd.read_csv` with
  `dtype=str, keep_default_na=False`, columns whose `column_mappings.orders` target is `Customer` / `Notes`.
  `main` uses `load_fixtures_to_db._client()`.

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `feat(scripts): check_no_real_data`.

### Task 4: Fallback-share seeder and the trial checklist (spec A5, A6)

**Files:**
- Create: `F/scripts/seed_fallback_share.py`, `F/docs/trial-checklist.md`
- Test: `F/tests/test_seed_fallback_share.py`

**Interfaces:**
- Consumes: `scripts.build_guide_server.build(dest: Path)`.
- Produces: `MARKER = ".fallback-share-seed"`, `refusal(dest: str, reset: bool) -> str | None` (the reason, or None),
  `seed(dest: str, reset: bool = False) -> None` (raises `ValueError(reason)`), `main(argv) -> None` (exit 2 on
  `ValueError`).

- [ ] **Step 1: Write the failing tests:**
  - `test_refuses_production_unc`: `refusal(r"\\192.168.88.101\_Fulfilment_\0UFulfilment", False)` and
    `refusal("//192.168.88.101/x", False)` and the upper-case variant are not None.
  - `test_refuses_server_dot(tmp_path, monkeypatch)`: `monkeypatch.setenv("HOME", str(tmp_path))`;
    `refusal(str(tmp_path / "Desktop" / "Server." / "x"), False)` not None (compute `Path.home()` at call time).
  - `test_refuses_non_empty_without_reset(tmp_path)`.
  - `test_reset_without_marker_refuses_and_keeps_files(tmp_path)`: a file in DEST, `seed(DEST, reset=True)` raises
    `ValueError`, the file is still there.
  - `test_seed_layout_reads_through_filestore(seeded)`: module-scoped fixture `seeded` runs `seed` once into a
    `tmp_path_factory` dir; `FileStore(dest).query_sessions("ALMA")` has 3 sessions, `"HERBAR"` 2; one
    `packing_lists/*.json` per session reads through `read_json`; `MARKER` exists.
  - `test_reset_with_marker_reseeds(tmp_path)`: seed, add `junk.txt`, `seed(dest, reset=True)` → `junk.txt` gone,
    `MARKER` present. (Second full build: keep this the only other build in the file.)

- [ ] **Step 2: Run** `scripts/run_tests.sh tests/test_seed_fallback_share.py -q` → FAIL.

- [ ] **Step 3: Implement** per spec A5. UNC check on the raw string: `s.replace("/", "\\").lower()
  .startswith("\\\\192.168.88.101\\")`. Server-dot check: `Path(dest).expanduser().resolve()
  .is_relative_to((Path.home() / "Desktop" / "Server.").resolve())`. Reset deletes the children of DEST (not DEST
  itself) with `shutil.rmtree` / `unlink`, then `build(DEST)`, then writes `MARKER`.

- [ ] **Step 4: Write `docs/trial-checklist.md`**: sections Setup (PC sign-in users; loader runs; fallback share
  seeding; dev-store seeding by the owner with the seed app token in env; `check_no_real_data.py`), **Reset (local
  stack)** with the exact commands and the reason from spec A6, then §9's five Windows checks as `- [ ]` items
  copied from `docs/superpowers/specs/2026-11-13-supabase-fork-design.md` §9, then "After each round: run
  `check_no_real_data.py`; note results in the vault notes".

- [ ] **Step 5: Run** → PASS. **Step 6: Commit** `feat(scripts): seed_fallback_share and the trial checklist`.

### Task 5: `Store.packing_overview` interface + FileStore (spec B1)

**Files:**
- Modify: `F/shared/store.py` (constants near the top, abstract method next to `query_sessions` ~line 129,
  FileStore implementation next to FileStore `query_sessions` ~line 306)
- Test: `F/tests/shared/test_store_contract.py` (append), `F/tests/shared/test_store_packing_overview.py` (new,
  FileStore-only cases)

**Interfaces:**
- Produces: `PACKING_LIST_KEYS`, `PACKING_STATE_KEYS` (values in spec B1), `Store.packing_overview(self,
  client_id: str) -> list[dict]` (row keys and order exactly as the spec B1 table). Base raises
  `NotImplementedError` like the other base methods.

- [ ] **Step 1: Write the failing contract tests** (both backends via the existing `store` fixture). Helper
  `_seed_overview(store)` under client `X`, using only store writes: session `s1`: `session_info.json`
  `{"pc_name": "PC-A"}`, list `packing_lists/U.json` `{"created_at": "t1", "total_orders": 2, "total_items": 3,
  "orders": []}` (unstarted); session `s2`: list `S.json` same shape, `packing/S/packing_state.json`
  `{"worker_id": "w1", "progress": {"total_orders": 2}, "skipped_orders": ["9"], "orders": [1]}`,
  `store.acquire_list_lock("Sessions/CLIENT_X/s2/packing/S", "PC-B", "u")`; session `s3`: list `F.json`,
  `packing/F/session_summary.json` `{"total_orders": 2, "completed_orders": 2}`; a withdrawn list
  `s1/packing_lists/withdrawn/W.json`.
  - `test_packing_overview_rows(store)`: rows' `(session_name, list_name)` == `[("s1","U"), ("s2","S"),
    ("s3","F")]`; `U`: `started False`, `state None`, `summary None`, `lock None`, `session_info == {"pc_name":
    "PC-A"}`, `packing_list == {"created_at": "t1", "total_orders": 2, "total_items": 3}` (no `orders`); `S`:
    `started True`, `state == {"worker_id": "w1", "progress": {"total_orders": 2}, "skipped_orders": ["9"]}`,
    `lock.holder_pc == "PC-B"`, `session_info is None`, `work_rel == "Sessions/CLIENT_X/s2/packing/S"`; `F`:
    `summary == {"total_orders": 2, "completed_orders": 2}`, `state is None`.
  - `test_packing_overview_unknown_client_empty(store)`: `store.packing_overview("NOPE") == []`.
- [ ] **Step 2: Write the failing FileStore-only tests** (`test_store_packing_overview.py`):
  `test_legacy_barcodes_row` (session with `barcodes/packing_state.json` `{"packing_list_name": "Old"}` and no
  `packing_lists/` → one row, `list_name "Old"`, `work_rel` ends `/barcodes`, `packing_list None`, `started True`);
  `test_total_orders_from_orders_length` (list without `total_orders` and 3 orders → `packing_list["total_orders"]
  == 3`); `test_corrupt_document_raises` (write `{` into a `packing_state.json` → `StoreError`);
  `test_failed_listing_raises` (monkeypatch `FileStore.list_dir` to raise `StoreError` → raises).
- [ ] **Step 3: Run** `scripts/run_tests.sh tests/shared/test_store_contract.py tests/shared/test_store_packing_overview.py -q -k overview` → FAIL.
- [ ] **Step 4: Implement** FileStore `packing_overview` through its own public methods (`exists`, `list_dir`,
  `read_json`, `lock_info`) mirroring P `packing_tool/session_registry_manager.py:219-330` (copy its walk order and
  conditions; skip names starting with `.` and `registry_index.json`/`session_index.json`). Trim with the
  constants; `total_orders` fallback before trimming. Sort rows by `(session_name, list_name)`.
- [ ] **Step 5: Run** → file params PASS (supabase params still FAIL: Task 6). **Step 6: Commit**
  `feat(store): packing_overview for FileStore`.

### Task 6: SupabaseStore `packing_overview` (spec B1)

**Files:**
- Modify: `F/shared/store_supabase.py` (next to `query_sessions` ~line 404)
- Test: `F/tests/shared/test_store_supabase.py` (append)

**Interfaces:**
- Consumes: Task 5's constants and row shape; `self._all`, `_guard`, `LockInfo`, `_parse_ts`.

- [ ] **Step 1: Write the failing Supabase tests** (`@pytest.mark.supabase`, store over `supabase_client` as the
  file's existing fixture does):
  - `test_packing_overview_skips_withdrawn_and_archived`: seed with the contract helper (import it from
    `tests/shared/test_store_contract.py` or copy it), also `packing/W/packing_state.json` and a lock on
    `packing/W` for the withdrawn list; then archive session `s3` with
    `supabase_client.table("sessions").update({"archived": True}).eq("client_id", "X").eq("name", "s3").execute()`
    → rows are `[("s1","U"), ("s2","S")]`.
  - `test_packing_overview_api_error_raises`: monkeypatch `store.client.table` to raise
    `httpx.ConnectError("down")` → `StoreError`.
  - `test_packing_overview_request_count`: wrap `store.client.table` to count calls; seeding 3 or 6 sessions gives
    the same count (fixed per client, ≤ 5 when nothing pages).
- [ ] **Step 2: Run** `scripts/run_tests.sh tests/shared/test_store_supabase.py tests/shared/test_store_contract.py -q -k overview` → FAIL.
- [ ] **Step 3: Implement** the five selects of spec B1 under `@_guard`, joined in Python; JSON projections with
  `"<key>:body-><key>"` / `"<key>:state-><key>"` and `pc_name:info->>pc_name`; drop null values; rows driven by
  the `packing_lists` result filtered to live sessions; `work_rel = f"Sessions/CLIENT_{cid}/{s}/packing/{l}"`.
- [ ] **Step 4: Run** → PASS, contract tests on both params PASS. **Step 5: Commit**
  `feat(store): packing_overview for SupabaseStore in five selects`.

### Task 7: `local_path` stops re-downloading unchanged blobs (spec B2)

**Files:**
- Modify: `F/shared/store_supabase.py` (`__init__` ~line 96, `local_path` ~line 607)
- Test: `F/tests/shared/test_store_supabase.py` (append)

- [ ] **Step 1: Write the failing tests** (`@pytest.mark.supabase`):
  `test_local_path_downloads_unchanged_blob_once`: `write_bytes(S + "input/a.csv", b"1")`; wrap
  `store._bucket` (property: monkeypatch the class property or wrap `client.storage.from_`) to count `download`;
  two `local_path` calls → 1 download, file content `b"1"`.
  `test_local_path_refetches_after_change`: then `write_bytes(..., b"2")` → `local_path` content `b"2"`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** per spec B2 (`self._fetched: dict[str, object] = {}`; remove
  the `ponytail:` comment). **Step 4: Run** → PASS.
- [ ] **Step 5: F gate** in the background: `scripts/run_tests.sh -q` and `.venv/bin/ruff check .` → all pass,
  coverage ≥ 90. Then restore the local trial data (the suite wiped it):
  `SUPABASE_SERVICE_KEY=<SERVICE_ROLE_KEY from supabase status> FULFILLMENT_DB_URL=http://127.0.0.1:54321
  .venv/bin/python scripts/load_fixtures_to_db.py` and again with `--fixtures tests/replay/fixtures` (check
  `shared/db_config.py` for the exact env var names the URL/key override uses). Do not commit or print the key.
- [ ] **Step 6: Commit** `perf(store): local_path skips the download when the blob is unchanged`, then
  `git push -u origin dr/48-supabase-b1-trial-tooling-and-packer-reg`.

---

## Packer fork (`P`)

### Task 8: Sync `shared/` and forward the new method

**Files:**
- Modify (via script only): `P/shared/*`, `P/scripts/shared_synced_from.txt`
- Modify: `P/packing_tool/offline_buffer.py` (~line 248, next to `query_sessions`)
- Test: `P/tests/test_offline_buffer.py` (append; find the existing forwarding test for `query_sessions` and copy it)

- [ ] **Step 1:** In P: `.venv/bin/python scripts/sync_shared.py /home/gloopy/Desktop/Projects/shopify-fulfillment-tool-db/.claude/worktrees/dr-48`
  (F's Task 7 commit must be pushed first; the pin must equal F `HEAD`).
- [ ] **Step 2: Failing test** `test_packing_overview_forwards_and_marks_lost_on_error`: like the
  `query_sessions` forwarding test — returns the inner rows, and an inner `StoreError` marks the store lost and
  re-raises.
- [ ] **Step 3:** Add `def packing_overview(self, client_id): return self._inner(self.inner.packing_overview,
  client_id)`. **Step 4: Run** `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/test_offline_buffer.py` → PASS.
- [ ] **Step 5: Commit** `chore: sync shared (packing_overview) and forward it through BufferedStore`.

### Task 9: Derived registry from `packing_overview` (spec B3)

**Files:**
- Modify: `P/packing_tool/session_registry_manager.py` (`build_from_scan` 219-259, `_register_from_work_dir`
  315-404, `_register_available_from_file` 417-449)
- Test: `P/tests/test_session_registry.py` (derived section ~line 365)

**Interfaces:**
- Consumes: `store.packing_overview(client_id) -> list[dict]` (row keys in spec B1).
- Produces (private): `_started_entry(session_id, list_name, *, summary, state, locked, has_info, info_pc,
  work_dir, session_dir) -> dict | None` and `_available_entry(session_id, list_name, doc, pl_file, session_dir,
  mtime) -> dict`, returning today's 17-key / 9-key dicts.

- [ ] **Step 1: Failing tests** (use the existing `derived` fixture, `_session_tree`, `_work_dir`, `STATE`):
  - `test_derived_read_uses_one_store_query`: wrap the store so every public method except `local_path` and
    `packing_overview` fails the test when called; `derived.get_all_entries("M")` still returns the entries.
  - `test_derived_overview_error_propagates`: monkeypatch `store.packing_overview` to raise `StoreError` →
    `derived.read_registry("M")` raises `StoreError`.
  - `test_derived_and_file_scan_agree`: same folder (one unstarted list, one started with state + lock, one with a
    summary); `derived.build_from_scan("M")` equals `SessionRegistryManager(<file-mode pm>).build_from_scan("M")`
    after dropping `mtime` from available entries and `last_updated` from the registry.
  - Keep `test_derived_mode_a_failed_listing_raises_rather_than_reading_empty` passing (FileStore's overview uses
    `list_dir`, so the existing monkeypatch still bites).
- [ ] **Step 2: Run** `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/test_session_registry.py` → new tests FAIL.
- [ ] **Step 3: Implement:** extract the two entry builders from the existing dict literals (file-mode methods
  call them; behaviour unchanged); in `build_from_scan`, `if store.derived_indexes:` build from
  `packing_overview` (paths: `session_dir = store.local_path(f"Sessions/CLIENT_{cid}/{s}")`, `work_dir =
  store.local_path(row["work_rel"])`, `pl_file = session_dir / "packing_lists" / f"{l}.json"`; legacy rows have
  `packing_list None` and go through `_started_entry`); `pc_name` default stays `os.environ.get("COMPUTERNAME",
  "Unknown")`; no `try/except` around the overview. Remove the `ponytail:` comment.
- [ ] **Step 4: Run** → PASS. **Step 5: P gate:** full `QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q` and
  `.venv/bin/python scripts/coverage_check.py` → pass; `ruff check .` clean.
- [ ] **Step 6: Commit** `perf(registry): derived registry from one packing_overview query`, push the branch.

## After both

Append Ruling/Carry lines to the ledger
(`~/Desktop/Projects/shopify-fulfillment-tool-db/.superpowers/sdd/supabase-fork-plan/progress.md`). PR text for P
says it pins F `HEAD` of this branch and must merge after the F PR.
