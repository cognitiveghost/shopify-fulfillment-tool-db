"""Storage seam: the file-backed Store both apps use for server-side data.

`rel` paths are POSIX-style, relative to the server root. The `.session.lock`
file format matches the Packer's SessionLockManager so a production Packer PC
and a Store-based PC agree on a share.
"""
import json
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

    def local_path(self, rel: str) -> Path:
        raise NotImplementedError

    def stamp(self, rel: str) -> object:
        """Opaque change token (compare with ==); StoreNotFound if absent, StoreError on other OSErrors."""
        raise NotImplementedError


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
                try:
                    current = self.read_json(rel)
                except StoreNotFound:
                    current = None
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
