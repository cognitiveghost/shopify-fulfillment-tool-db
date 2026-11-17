"""Seed a folder for the file-fallback trial with the same data the loader's default mode puts in Supabase.

    .venv/bin/python scripts/seed_fallback_share.py DEST [--reset]

Refuses (exit 2, nothing written or deleted) the production share, a folder under ~/Desktop/Server., a non-empty
DEST without --reset, and --reset on a non-empty DEST that has no marker file (not seeded by this script).
"""

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.build_guide_server import build

MARKER = ".fallback-share-seed"
PRODUCTION_UNC = "\\\\192.168.88.101\\"


def refusal(dest: str, reset: bool) -> str | None:
    if dest.replace("/", "\\").lower().startswith(PRODUCTION_UNC.lower()):
        return "refusing: the production share"
    if Path(dest).expanduser().resolve().is_relative_to((Path.home() / "Desktop" / "Server.").resolve()):
        return "refusing: a folder under ~/Desktop/Server."
    path = Path(dest).expanduser()
    if path.is_dir() and any(path.iterdir()):
        if not reset:
            return f"{dest} is not empty; pass --reset to replace a folder this script seeded"
        if not (path / MARKER).exists():
            return f"{dest} has no {MARKER}; not seeded by this script, not wiping it"
    return None


def seed(dest: str, reset: bool = False) -> None:
    reason = refusal(dest, reset)
    if reason:
        raise ValueError(reason)
    path = Path(dest).expanduser()
    if reset and path.is_dir():
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    build(path)
    (path / MARKER).write_text("seeded by scripts/seed_fallback_share.py\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed a folder for the file-fallback trial.")
    parser.add_argument("dest", help="folder to seed")
    parser.add_argument("--reset", action="store_true", help="replace the contents of a folder this script seeded")
    args = parser.parse_args(argv)
    try:
        seed(args.dest, args.reset)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    print(f"seeded {args.dest}")


if __name__ == "__main__":
    main()
