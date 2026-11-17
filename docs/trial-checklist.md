# Supabase trial checklist

Run by the owner, one round at a time. Commands run from the F main checkout (not a worktree) unless a line says
otherwise. Placeholders in angle brackets are filled in by hand; never paste real keys, shop names or customer data
into this file or into the vault.

## Reset (local stack), at the top of each round

Run this before any manual trial on the local stack. It rebuilds the same dataset every time.

```bash
supabase db reset
export FULFILLMENT_DB_URL=<API_URL from `supabase status`>
SUPABASE_SERVICE_KEY=<service_role key from `supabase status`> \
  .venv/bin/python scripts/load_fixtures_to_db.py --fixtures tests/replay/fixtures
SUPABASE_SERVICE_KEY=<service_role key from `supabase status`> \
  .venv/bin/python scripts/load_fixtures_to_db.py
```

Without `FULFILLMENT_DB_URL` the loader targets the hosted project (it prints `target <url>` first), which refuses
the local key. `supabase db reset` re-applies the migrations to an empty database. The first load writes the 26 replay sessions
(ALMA, HERBAR, LIDA). The second writes the guide server (two clients, sessions with packing lists, packer settings,
workers). Both are needed for a full trial dataset.

Why: the `@pytest.mark.supabase` tests wipe every table and the storage bucket of the local stack. Any test run
destroys the manual trial data. So run the reset after any test run, before a manual trial.

Remote (hosted project): there is no reset. Load once with the owner's sign-in (the loader asks for the email and
password when `SUPABASE_SERVICE_KEY` is unset), and do not run `db reset` there. A second load rewrites the
client configs and every `session_info.json` as unanalysed, so do not rerun it after a trial round.

## Setup

- [ ] Sign-in user per work PC, created in the hosted project. Each PC signs in with its own user.
- [ ] Loader run for the remote project (see Reset above; no `SUPABASE_SERVICE_KEY`).
- [ ] Fallback share seeded for the file-fallback trial:
  `.venv/bin/python scripts/seed_fallback_share.py <share folder> [--reset]`.
  The script refuses the production share and any folder under the production server folder.
- [ ] Dev store seeded by the owner, with the seeding app's token in the environment only:
  `SHOPIFY_SEED_TOKEN=<token> .venv/bin/python scripts/seed_dev_store.py --client ALMA --shop <shop>.myshopify.com --orders 300`.
  Never put the token in argv or in a file.
- [ ] `scripts/check_no_real_data.py` passes after loading (prints `ok`).

## Windows checks (spec §9)

- [ ] Both work PCs pack different lists of one session at the same time.
- [ ] A second PC trying to open a list that is in use is refused.
- [ ] Pulling the network cable mid-list on a Packer keeps it packing, and progress lands after reconnect.
- [ ] The home PC does a whole session: pull orders from the dev store, analyse, packing list, pack.
- [ ] With Supabase blocked, both work PCs start in file fallback on the shared test share and can pack different
  lists of one session.

## After each round

- [ ] Run `scripts/check_no_real_data.py` and note the result in the vault notes (`~/Obsidian/Claude/`), with the
  round date and the exit code. A failure means stop: do not share the database until it passes.
