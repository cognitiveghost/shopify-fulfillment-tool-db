"""Storage seam: the file-backed Store both apps use for server-side data.

`rel` paths are POSIX-style, relative to the server root. The `.session.lock`
file format matches the Packer's SessionLockManager so a production Packer PC
and a Store-based PC agree on a share.
"""
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from shared.atomic_write import atomic_write_json, replace_with_retry
from shared.file_lock import FileLockError, locked_file
from shared.metadata_utils import parse_timestamp

LOCK_FILENAME = ".session.lock"
STALE_SECONDS = 120
LOCK_READ_ATTEMPTS = 3
LOCK_READ_RETRY_SECONDS = 0.2
SESSION_INDEX = "session_index.json"  # per-client cache behind FileStore.query_sessions
SESSION_INFO = "session_info.json"

logger = logging.getLogger("ShopifyToolLogger")


def _now() -> datetime:
    """Current aware time; tests patch this."""
    return datetime.now().astimezone()


class StoreError(Exception):
    pass


class StoreNotFound(StoreError):
    pass


class StoreConflict(StoreError):
    """Optimistic-version clash after retries."""


@dataclass(frozen=True)
class LockInfo:
    acquired: bool
    holder_pc: str | None
    holder_user: str | None
    heartbeat_at: datetime | None


class Store:
    """Base class; backends override every method."""

    # True when session/registry indexes are queries on tables, not cache files (SupabaseStore).
    derived_indexes = False

    def read_json(self, rel: str) -> Any:
        raise NotImplementedError

    def write_json(self, rel: str, data: Any) -> None:
        raise NotImplementedError

    def update_json(self, rel: str, fn: Callable[[Any | None], Any]) -> Any:
        raise NotImplementedError

    def read_bytes(self, rel: str) -> bytes:
        raise NotImplementedError

    def write_bytes(self, rel: str, data: bytes) -> None:
        raise NotImplementedError

    def write_bytes_if(self, rel: str, data: bytes, expected: object) -> object:
        """Write `data` only if stamp(rel) == expected (None: must not exist); return the new stamp.

        Raises StoreConflict, writing nothing, on a mismatch.
        """
        raise NotImplementedError

    def exists(self, rel: str) -> bool:
        raise NotImplementedError

    def list_dir(self, rel: str) -> list[str]:
        raise NotImplementedError

    def move(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def delete(self, rel: str) -> None:
        raise NotImplementedError

    def claim_dir(self, rel: str) -> bool:
        raise NotImplementedError

    def acquire_list_lock(self, list_dir: str, pc: str, user: str) -> LockInfo:
        raise NotImplementedError

    def force_take_list_lock(self, list_dir: str, pc: str, user: str, expected_heartbeat: datetime) -> bool:
        raise NotImplementedError

    def heartbeat_list_lock(self, list_dir: str, pc: str) -> bool:
        raise NotImplementedError

    def release_list_lock(self, list_dir: str, pc: str) -> None:
        raise NotImplementedError

    def lock_info(self, list_dir: str) -> LockInfo | None:
        """The list's current lock without taking it (acquired=True means held); None when free.

        FileStore reads an unreadable lock file as held, aged by mtime, holder unknown;
        SupabaseStore raises StoreError when the lock table cannot be read.
        """
        raise NotImplementedError

    def local_path(self, rel: str) -> Path:
        raise NotImplementedError

    def stamp(self, rel: str) -> object:
        """Opaque change token (compare with ==); StoreNotFound if absent, StoreError on other OSErrors."""
        raise NotImplementedError

    def query_sessions(self, client_id: str) -> list[dict]:
        """One dict per non-archived session of the client: its session_info plus `session_name`.

        Sessions with no or unreadable session_info are skipped. Unordered. An unknown
        client gives []; a failed read raises StoreError, never [].
        """
        raise NotImplementedError


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path):
    """Blocking exclusive lock on a sidecar `.lock` file (the session index's only lock).

    Without this, two near-simultaneous read-modify-write cycles on the file
    `lock_path` guards each read the same on-disk snapshot before either writes
    back, and one update silently loses the other's change.
    """
    with open(lock_path, "a+") as lock_file:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            # Release only a lock this call took (AUDIT-07-L4).
            if locked and os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class FileStore(Store):
    def __init__(self, root: Path):
        self.root = Path(root)

    def _p(self, rel: str) -> Path:
        if "\\" in rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
            raise ValueError(f"bad store path: {rel!r}")
        return self.root / rel

    # --- data -----------------------------------------------------------
    def read_json(self, rel):
        path = self._p(rel)  # ValueError for a bad rel must not be wrapped below
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise StoreNotFound(rel) from None
        except (OSError, ValueError) as e:  # ValueError covers JSONDecodeError/UnicodeDecodeError
            raise StoreError(rel) from e

    def write_json(self, rel, data):
        atomic_write_json(self._p(rel), data)

    def update_json(self, rel, fn):
        path = self._p(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a sidecar, not the data file (its inode changes on every replace).
        # `<name>.lock` is the convention of today's callers, so old and new PCs share it.
        side = path.with_name(path.name + ".lock")
        try:
            with open(side, "a+b") as fh, locked_file(fh):
                # A transient SMB sharing violation must not fail the update (AUDIT-02-5).
                for attempt in range(LOCK_READ_ATTEMPTS):
                    try:
                        current = self.read_json(rel)
                        break
                    except StoreNotFound:
                        current = None
                        break
                    except StoreError:
                        if attempt == LOCK_READ_ATTEMPTS - 1:
                            raise
                        time.sleep(LOCK_READ_RETRY_SECONDS)
                new = fn(current)
                atomic_write_json(path, new)
                return new
        except (OSError, FileLockError) as e:
            raise StoreError(rel) from e

    def read_bytes(self, rel):
        try:
            return self._p(rel).read_bytes()
        except FileNotFoundError:
            raise StoreNotFound(rel) from None
        except OSError as e:
            raise StoreError(rel) from e

    def write_bytes(self, rel, data):
        path = self._p(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            replace_with_retry(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def exists(self, rel):
        # Not Path.exists(): it reads a dropped share or a denied folder as "absent".
        try:
            self._p(rel).stat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as e:
            raise StoreError(rel) from e
        return True

    def list_dir(self, rel):
        try:
            names = [c.name for c in self._p(rel).iterdir()]
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as e:
            raise StoreError(rel) from e
        # hide update_json's `<data>.lock` sidecars, but never .session.lock
        return sorted(n for n in names if not (n.endswith(".lock") and n != LOCK_FILENAME and n[:-5] in names))

    def move(self, src, dst):
        s, d = self._p(src), self._p(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))

    def delete(self, rel):
        p = self._p(rel)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)

    def claim_dir(self, rel):
        try:
            self._p(rel).mkdir(parents=True, exist_ok=False)
            return True
        except FileExistsError:
            return False

    def local_path(self, rel):
        return self._p(rel)

    def stamp(self, rel):
        try:
            st = self._p(rel).stat()
        except FileNotFoundError:
            raise StoreNotFound(rel) from None
        except OSError as e:
            raise StoreError(rel) from e
        return (st.st_mtime_ns, st.st_size)

    def write_bytes_if(self, rel, data, expected):
        path = self._p(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same sidecar as update_json, so the two lock each other.
        side = path.with_name(path.name + ".lock")
        try:
            with open(side, "a+b") as fh, locked_file(fh):
                try:
                    current = self.stamp(rel)
                except StoreNotFound:
                    current = None
                if current != expected:
                    raise StoreConflict(rel)
                self.write_bytes(rel, data)
                return self.stamp(rel)
        except (OSError, FileLockError) as e:
            raise StoreError(rel) from e

    # --- session index: a per-client cache of session_info.json for query_sessions ---
    def query_sessions(self, client_id):
        client_rel = f"Sessions/CLIENT_{client_id}"
        try:
            if not self.exists(client_rel):
                return []
            entries = self._read_index(client_rel)
            try:
                if entries is None:
                    entries = self._rebuild_index(client_rel)
                else:
                    entries = self._refresh_index(client_rel, entries)
            except OSError:
                # The index is a cache. A lock that can't be had costs one
                # slow listing, read from the folders and not written back.
                logger.warning("Session index is locked; listing the folders instead", exc_info=True)
                entries = self._scan_sessions(client_rel)
        except OSError as e:
            raise StoreError(client_rel) from e
        return [{k: v for k, v in e.items() if not k.startswith("_")}
                for e in entries if not e.get("_no_session_info")]

    def _index_lock(self, client_rel: str) -> Path:
        return self._p(f"{client_rel}/{SESSION_INDEX}.lock")

    def _read_index(self, client_rel: str) -> list[dict] | None:
        """The raw index, or None if it doesn't exist / is unreadable."""
        try:
            entries = self.read_json(f"{client_rel}/{SESSION_INDEX}")
        except StoreNotFound:
            return None
        except StoreError:
            logger.exception("Failed to read session index, treating as missing")
            return None
        if not isinstance(entries, list):
            logger.warning("Session index is not a list, treating as missing")
            return None
        return [e for e in entries if isinstance(e, dict)]

    def _write_index(self, client_rel: str, entries: list[dict]) -> None:
        self.write_json(f"{client_rel}/{SESSION_INDEX}", entries)

    def _listing(self, client_rel: str) -> dict[str, float]:
        """{folder name: st_mtime} for every session folder, from one listing.

        scandir, not iterdir: on Windows the directory listing already
        carries the timestamps, so DirEntry.stat() costs no extra network
        round trip on the share.
        """
        listing = {}
        with os.scandir(self._p(client_rel)) as it:
            for item in it:
                try:
                    if item.is_dir():
                        listing[item.name] = item.stat().st_mtime
                except OSError:
                    continue
        return listing

    def _index_entry(self, client_rel: str, name: str, dir_mtime: float) -> dict:
        """One index entry, the raw session_info.json of folder `name`.

        `_dir_mtime` is the folder's mtime from the listing taken before the
        read, so a write that lands during the read shows up as a change on
        the next listing. A folder with no session_info.json gets a marker
        entry with its mtime, so it is read again only when that changes. A
        session_info.json that is there but unreadable gets a marker with no
        mtime, so the next listing tries again rather than hiding a real
        session until it next changes.
        """
        def marker(mtime):
            return {"session_name": name, "_dir_mtime": mtime, "_no_session_info": True}

        try:
            info = self.read_json(f"{client_rel}/{name}/{SESSION_INFO}")
        except StoreNotFound:
            return marker(dir_mtime)
        except StoreError:
            logger.warning("Session info unreadable, will retry: %s", name, exc_info=True)
            return marker(None)
        if not isinstance(info, dict):
            return marker(dir_mtime)
        info["session_name"] = name
        info["_dir_mtime"] = dir_mtime
        return info

    @staticmethod
    def _index_matches(listing: dict[str, float], entries: list[dict]) -> bool:
        """True if every folder has exactly one entry with its current mtime."""
        if len(entries) != len(listing):
            return False
        for entry in entries:
            name = entry.get("session_name")
            if name not in listing or entry.get("_dir_mtime") != listing[name]:
                return False
        return True

    def _refresh_index(self, client_rel: str, entries: list[dict]) -> list[dict]:
        """Reread only the session folders that changed since the index saw them.

        Each entry records its folder's mtime (`_dir_mtime`). Packing Tool
        writes session_info.json as temp-file + rename (see atomic_write),
        which bumps the folder's mtime, so its writes show up here as a
        changed entry. The comparison is per entry and by equality: clocks
        that disagree between PCs no longer matter, and a mismatch costs one
        reread, never wrong data. A legacy entry without `_dir_mtime` counts
        as changed, so the first listing after the upgrade rereads each
        session once.

        Nothing changed means no lock and no write. Changed sessions are read
        before the lock is taken.
        """
        listing = self._listing(client_rel)
        if self._index_matches(listing, entries):
            return entries

        # The slow part -- one session_info.json per changed folder -- runs
        # without the lock. Under it, an entry read here is used only when its
        # folder's mtime has not moved since.
        known = {e.get("session_name"): e for e in entries}
        pre = {}
        for name, dir_mtime in listing.items():
            entry = known.get(name)
            if entry is None or entry.get("_dir_mtime") != dir_mtime:
                pre[name] = (dir_mtime, self._index_entry(client_rel, name, dir_mtime))

        with _exclusive_lock(self._index_lock(client_rel)):
            current = self._read_index(client_rel) or []
            listing = self._listing(client_rel)
            by_name = {e.get("session_name"): e for e in current}
            refreshed = []
            for name, dir_mtime in listing.items():
                entry = by_name.get(name)
                if entry is None or entry.get("_dir_mtime") != dir_mtime:
                    early = pre.get(name)
                    if early is not None and early[0] == dir_mtime:
                        entry = early[1]
                    else:
                        entry = self._index_entry(client_rel, name, dir_mtime)
                refreshed.append(entry)
            # A session_info.json that still can't be read is retried on every
            # listing and rereads the same marker: no write for that.
            if refreshed != current:
                self._write_index(client_rel, refreshed)
        return refreshed

    def _scan_sessions(self, client_rel: str) -> list[dict]:
        """Full folder scan, used when the index can't be read or written."""
        return [self._index_entry(client_rel, name, dir_mtime)
                for name, dir_mtime in self._listing(client_rel).items()]

    def _rebuild_index(self, client_rel: str) -> list[dict]:
        """Full scan + persist, when no readable index exists yet.

        Scan and write both happen under the index lock, so a concurrent
        rebuild or refresh cannot be overwritten by a stale snapshot.
        """
        with _exclusive_lock(self._index_lock(client_rel)):
            entries = self._scan_sessions(client_rel)
            self._write_index(client_rel, entries)
        return entries

    # --- list locks (.session.lock, same JSON as the Packer's SessionLockManager) ---
    def _lock_path(self, list_dir):
        return self._p(list_dir) / LOCK_FILENAME

    def _read_lock(self, lock_path: Path) -> dict | None:
        """Lock dict, or None if absent/invalid. An unreadable lock counts as held (aged by mtime)."""
        data = None
        for attempt in range(LOCK_READ_ATTEMPTS):
            try:
                data = json.loads(lock_path.read_text(encoding="utf-8"))
                break
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                if attempt < LOCK_READ_ATTEMPTS - 1:
                    time.sleep(LOCK_READ_RETRY_SECONDS)
        if data is None:
            try:
                hb = datetime.fromtimestamp(lock_path.stat().st_mtime).astimezone().isoformat()
            except FileNotFoundError:
                return None
            except OSError:
                hb = _now().isoformat()  # still held: an outage is not a free lock
            return {"locked_by": None, "user_name": None, "heartbeat": hb}
        if not isinstance(data, dict) or not all(
            k in data for k in ("locked_by", "user_name", "lock_time", "heartbeat")
        ):
            return None
        return data

    @staticmethod
    def _mine(data: dict, pc: str) -> bool:
        return data.get("locked_by") == pc and data.get("process_id") == os.getpid()

    @staticmethod
    def _heartbeat(data: dict) -> datetime | None:
        # Naive stamps (old files) are read as UTC, as the Packer's parse_timestamp does.
        return parse_timestamp(data.get("heartbeat"))

    def _is_stale(self, data: dict) -> bool:
        hb = self._heartbeat(data)
        return hb is None or (_now() - hb).total_seconds() > STALE_SECONDS

    def _lock_body(self, pc, user) -> dict:
        now = _now().isoformat()
        return {
            "locked_by": pc, "user_name": user, "lock_time": now, "process_id": os.getpid(),
            "app_version": None, "heartbeat": now, "worker_id": None, "worker_name": None,
        }

    def _create_lock(self, lock_path: Path, body: dict) -> bool:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2)
        return True

    def lock_info(self, list_dir):
        data = self._read_lock(self._lock_path(list_dir))
        if data is None:
            return None
        return LockInfo(True, data.get("locked_by"), data.get("user_name"), self._heartbeat(data))

    def acquire_list_lock(self, list_dir, pc, user):
        lp = self._lock_path(list_dir)
        body = self._lock_body(pc, user)
        # Create first: clearing an absent lock before creating could delete one
        # another PC made in between, and both would hold the list.
        for _ in range(2):
            if self._create_lock(lp, body):
                return LockInfo(True, pc, user, parse_timestamp(body["heartbeat"]))
            data = self._read_lock(lp)
            if data is not None:
                if self._mine(data, pc):
                    self.heartbeat_list_lock(list_dir, pc)
                    return LockInfo(True, pc, data.get("user_name"), self._heartbeat(data))
                return LockInfo(False, data.get("locked_by"), data.get("user_name"), self._heartbeat(data))
            # ponytail: an invalid lock is cleared and retried once; two PCs clearing
            # the same one at once could both hold. Rename it aside if that ever shows up.
            lp.unlink(missing_ok=True)
        other = self._read_lock(lp) or {}
        return LockInfo(False, other.get("locked_by"), other.get("user_name"), self._heartbeat(other))

    def force_take_list_lock(self, list_dir, pc, user, expected_heartbeat):
        lp = self._lock_path(list_dir)
        data = self._read_lock(lp)
        if data is None or self._heartbeat(data) != expected_heartbeat or not self._is_stale(data):
            return False
        lp.unlink(missing_ok=True)
        return self._create_lock(lp, self._lock_body(pc, user))

    def heartbeat_list_lock(self, list_dir, pc):
        lp = self._lock_path(list_dir)
        data = self._read_lock(lp)
        if data is None or not self._mine(data, pc):
            return False
        data["heartbeat"] = _now().isoformat()
        atomic_write_json(lp, data)
        return True

    def release_list_lock(self, list_dir, pc):
        lp = self._lock_path(list_dir)
        data = self._read_lock(lp)
        if data is not None and self._mine(data, pc):
            lp.unlink(missing_ok=True)
