"""One storage mode per run, chosen at startup (spec §7.3): an app never writes to two backends in one run."""
import os
import socket
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from shared import db_auth
from shared.store import FileStore, Store


class Mode(Enum):
    SUPABASE = "supabase"
    FILE = "file"
    OFFLINE_BUFFER = "offline_buffer"
    NONE = "none"


def choose_mode(*, outbox_pending: bool, supabase_ok: bool, fallback_ok: bool) -> Mode:
    if supabase_ok:
        return Mode.SUPABASE  # with a pending outbox the caller flushes first
    if outbox_pending:
        return Mode.OFFLINE_BUFFER  # never the file share: the outbox belongs to the database
    return Mode.FILE if fallback_ok else Mode.NONE


def open_store(
    app_name: str,
    *,
    fallback_root: str,
    cache_dir: Path,
    prompt: Callable[[], tuple[str, str] | None],
    outbox_pending: bool = False,
    pc: str | None = None,
) -> tuple[Mode, Store | None]:
    """OFFLINE_BUFFER returns no store: the Packer builds its BufferedStore over the cache itself."""
    client = db_auth.connect(app_name, prompt)
    fallback_ok = os.path.isdir(fallback_root)
    mode = choose_mode(outbox_pending=outbox_pending, supabase_ok=client is not None, fallback_ok=fallback_ok)
    if mode is Mode.SUPABASE:
        from shared.store_supabase import SupabaseStore

        return mode, SupabaseStore(client, pc=pc or socket.gethostname(),
                                   cache_dir=cache_dir)
    if mode is Mode.FILE:
        return mode, FileStore(Path(fallback_root))
    return mode, None
