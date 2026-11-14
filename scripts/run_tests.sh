#!/usr/bin/env bash
# Runs the test suite against THIS worktree's .venv, ignoring whatever
# python/pytest is on PATH (avoids picking up the main repo's .venv).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -x .venv/bin/python ]; then
    echo "No .venv here — run scripts/setup_venv.sh first." >&2
    exit 1
fi

# The @pytest.mark.supabase tests need the local stack's keys (as CI exports them);
# without a running stack (`supabase start`) they skip and coverage falls short.
if [ -z "${SUPABASE_TEST_URL:-}" ] && command -v supabase >/dev/null \
    && env_out=$(supabase status -o env 2>/dev/null); then
    eval "$(sed -n -e 's/^API_URL=/export SUPABASE_TEST_URL=/p' \
        -e 's/^ANON_KEY=/export SUPABASE_TEST_ANON_KEY=/p' \
        -e 's/^SERVICE_ROLE_KEY=/export SUPABASE_TEST_SERVICE_KEY=/p' <<<"$env_out")"
fi

QT_QPA_PLATFORM=offscreen exec .venv/bin/python -m pytest "$@"
