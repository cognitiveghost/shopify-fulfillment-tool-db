"""Guard: server-side I/O goes through shared.store, not raw paths.

Flags lines in shopify_tool/, gui/ and shared/ that build or open a path from a server-root name,
and any atomic_write_json( / replace_with_retry( / locked_file( call outside the store seam.
Files that legitimately do this are listed in tests/store_guard_allow.txt (matched by file path).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW = Path(__file__).with_name("store_guard_allow.txt")
ROOTS = ("shopify_tool", "gui", "shared")
# The seam itself and the primitives it wraps.
EXEMPT = {"shared/atomic_write.py", "shared/file_lock.py"}

NAMES = r"(?:base_path|sessions_root|sessions_dir|clients_dir|stats_dir)"
PATH_USE = re.compile(
    rf"\b{NAMES}\b.*(?:\s/\s|os\.path\.join|\bopen\(|\bPath\()"
    rf"|(?:\s/\s|os\.path\.join|\bopen\(|\bPath\().*\b{NAMES}\b"
)
PRIMITIVE = re.compile(r"\b(?:atomic_write_json|replace_with_retry|locked_file)\(")


def _allowed() -> set[str]:
    out = set()
    for line in ALLOW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.split(":", 1)[0].strip())
    return out


def _hits() -> list[str]:
    hits = []
    for top in ROOTS:
        for f in sorted((ROOT / top).rglob("*.py")):
            rel = f.relative_to(ROOT).as_posix()
            if rel in EXEMPT or re.fullmatch(r"shared/store[^/]*\.py", rel):
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if code.lstrip().startswith(('"', "'")) or not code.strip():
                    continue
                if PATH_USE.search(code) or PRIMITIVE.search(code):
                    hits.append(f"{rel}:{n}: {line.strip()}")
    return hits


def test_no_unlisted_raw_server_io():
    allowed = _allowed()
    bad = [h for h in _hits() if h.split(":", 1)[0] not in allowed]
    assert not bad, "raw server I/O outside the Store (migrate, or add path:reason to store_guard_allow.txt):\n" + "\n".join(bad)


def test_allow_list_has_no_stale_entries():
    seen = {h.split(":", 1)[0] for h in _hits()}
    stale = _allowed() - seen
    assert not stale, f"allow-list entries with no hits: {sorted(stale)}"
