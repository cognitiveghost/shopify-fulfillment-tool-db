"""Supabase-backed Store: JSON paths live in tables, everything else in Storage bucket `files`.

`store_routes.route()` decides where a rel path lives. JSON rows are written with an
optimistic `version` compare-and-set; list locks are the Task 2 RPCs. A failed call
raises StoreError, never an empty result.
"""
import csv
import functools
import io
import json
import mimetypes
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from postgrest.exceptions import APIError
from pydantic import ValidationError
from storage3.exceptions import StorageException

from shared.store import LockInfo, Store, StoreConflict, StoreError, StoreNotFound
from shared.store_routes import route

BUCKET = "files"
CAS_ATTEMPTS = 8
PAGE = 1000  # rows per select page; PostgREST caps a response at max_rows
DUPLICATE = "23505"  # unique_violation
VERSION_CLASH = "40001"  # replace_fulfillment_history: version mismatch
_PLACEHOLDER = ".emptyFolderPlaceholder"

_CLIENT_DIR = re.compile(r"(Clients|Sessions)/(_archive/)?CLIENT_([^/]+)")
_LOCK_DIR = re.compile(r"Sessions/CLIENT_([^/]+)/([^/]+)/packing/([^/]+)")
_SESSION_SUB = re.compile(r"Sessions/CLIENT_([^/]+)/([^/]+)(?:/(.*))?")


@dataclass(frozen=True)
class _Row:
    """One JSON value: column `col` of the row of `table` matching `match`."""
    table: str
    match: dict[str, Any]
    col: str


def _row_for(rel: str) -> _Row | None:
    """The table cell a JSON rel maps to; None for kinds that are not a single cell."""
    r = route(rel)
    k = r.key
    sess = {"client_id": k.get("client_id"), "session_name": k.get("session_name")}
    match r.kind:
        case "clients_field":
            return _Row("clients", {"client_id": k["client_id"]}, k["field"])
        case "groups" | "workers" | "stats":
            return _Row(r.kind, {"id": 1}, "body")
        case "session_info":
            return _Row("sessions", {"client_id": k["client_id"], "name": k["session_name"]}, "info")
        case "session_doc":
            return _Row("session_documents", {**sess, "path": k["path"]}, "body")
        case "packing_list":
            return _Row("packing_lists", {**sess, "name": k["name"], "withdrawn": k["withdrawn"] == "1"}, "body")
        case "packing_state":
            return _Row("packing_state", {**sess, "list_name": k["list_name"]}, "state")
        case "packing_summary":
            return _Row("packing_state", {**sess, "list_name": k["list_name"]}, "summary")
    return None


def _guard(fn):
    """Turn transport/DB failures into StoreError (a failed read is never 'empty')."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except StoreError:
            raise
        # JSONDecodeError and pydantic's ValidationError are ValueErrors from a cut-off
        # response; route()'s own ValueError for a bad path passes through unwrapped.
        except (APIError, StorageException, httpx.HTTPError, OSError,
                json.JSONDecodeError, ValidationError) as e:
            raise StoreError(f"{fn.__name__}{args[:1]}: {e}") from e
    return wrapper


def _parse_ts(v: str | None) -> datetime | None:
    # Python 3.11 fromisoformat takes postgres' variable-length fractions and +00:00.
    return datetime.fromisoformat(v) if v else None


class SupabaseStore(Store):
    def __init__(self, client, *, pc: str, cache_dir: Path):
        self.client = client
        self.pc = pc
        self.cache_dir = Path(cache_dir)

    # --- small helpers ---------------------------------------------------
    def _t(self, table: str):
        return self.client.table(table)

    @property
    def _bucket(self):
        return self.client.storage.from_(BUCKET)

    def _ensure_client(self, client_id: str) -> None:
        self._t("clients").upsert({"client_id": client_id}, on_conflict="client_id", ignore_duplicates=True).execute()

    def _ensure_parents(self, row: _Row) -> None:
        cid = row.match.get("client_id")
        if cid is None:
            return
        self._ensure_client(cid)
        sname = row.match.get("session_name")
        if sname is not None:
            self._t("sessions").upsert(
                {"client_id": cid, "name": sname}, on_conflict="client_id,name", ignore_duplicates=True
            ).execute()

    def _read_row(self, row: _Row) -> tuple[Any, int] | None:
        """(value, version) of the cell's row, or None when there is no row."""
        res = self._t(row.table).select(f"{row.col},version").match(row.match).limit(1).execute()
        if not res.data:
            return None
        return res.data[0][row.col], res.data[0]["version"]

    def _put(self, row: _Row, value: Any, version: int | None) -> int:
        """Write at `version` (None: insert). Raises StoreConflict if the row moved."""
        if version is None:
            self._ensure_parents(row)
            try:
                self._t(row.table).insert({**row.match, row.col: value, "updated_by": self.pc}).execute()
            except APIError as e:
                if e.code == DUPLICATE:
                    raise StoreConflict(row.table) from e
                raise
            return 1
        res = (self._t(row.table).update({row.col: value, "version": version + 1, "updated_by": self.pc})
               .match(row.match).eq("version", version).execute())
        if not res.data:
            raise StoreConflict(row.table)
        return version + 1

    @staticmethod
    def _all(query: Callable[[], Any]) -> list[dict]:
        """Every row of `query()`: a fresh ordered select(..., count="exact"), paged past
        max_rows, which otherwise truncates the result without an error."""
        out: list[dict] = []
        while True:
            res = query().range(len(out), len(out) + PAGE - 1).execute()
            out += res.data
            if not res.data or len(out) >= (res.count or 0):
                return out

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(random.uniform(0, 0.03 * (attempt + 1)))

    # --- JSON ------------------------------------------------------------
    @_guard
    def read_json(self, rel):
        row = _row_for(rel)
        if row is not None:
            cur = self._read_row(row)
            if cur is None or cur[0] is None:
                raise StoreNotFound(rel)
            return cur[0]
        r = route(rel)
        if r.kind == "client_history":
            res = (self._t("client_config_history").select("body").eq("client_id", r.key["client_id"])
                   .eq("kind", r.key["name"]).order("id", desc=True).limit(1).execute())
            if not res.data:
                raise StoreNotFound(rel)
            return res.data[0]["body"]
        raise ValueError(f"not a JSON path: {rel!r}")

    @_guard
    def write_json(self, rel, data):
        r = route(rel)
        if r.kind == "client_history":
            self._ensure_client(r.key["client_id"])
            self._t("client_config_history").insert(
                {"client_id": r.key["client_id"], "kind": r.key["name"], "body": data, "updated_by": self.pc}
            ).execute()
            return
        self.update_json(rel, lambda _cur: data)

    @_guard
    def update_json(self, rel, fn: Callable[[Any | None], Any]):
        row = _row_for(rel)
        if row is None:
            raise ValueError(f"update_json needs a single-row JSON path: {rel!r}")
        for attempt in range(CAS_ATTEMPTS):
            cur = self._read_row(row)
            new = fn(None if cur is None else cur[0])
            try:
                self._put(row, new, None if cur is None else cur[1])
                return new
            except StoreConflict:
                self._backoff(attempt)
        raise StoreConflict(rel)

    # --- bytes -----------------------------------------------------------
    @_guard
    def read_bytes(self, rel):
        kind = route(rel).kind
        if kind == "blob":
            try:
                return self._bucket.download(rel)
            except StorageException as e:
                raise self._missing_or_error(rel, e) from e
        if kind == "fulfillment_history":
            return self._read_history(route(rel).key["client_id"], rel)
        return json.dumps(self.read_json(rel), ensure_ascii=False).encode("utf-8")

    @_guard
    def write_bytes(self, rel, data):
        kind = route(rel).kind
        if kind == "blob":
            self._upload(rel, data, upsert=True)
        elif kind == "fulfillment_history":
            cid = route(rel).key["client_id"]
            for attempt in range(CAS_ATTEMPTS):
                try:
                    self._write_history(cid, data, self._history_version(cid))
                    return
                except StoreConflict:
                    self._backoff(attempt)
            raise StoreConflict(rel)
        else:
            self.write_json(rel, self._parse_json(rel, data))

    @_guard
    def write_bytes_if(self, rel, data, expected):
        kind = route(rel).kind
        if kind == "blob":
            return self._blob_write_if(rel, data, expected)
        if kind == "fulfillment_history":
            cid = route(rel).key["client_id"]
            return self._write_history(cid, data, 0 if expected is None else expected)
        row = _row_for(rel)
        if row is None:
            raise ValueError(f"write_bytes_if: unsupported path {rel!r}")
        value = self._parse_json(rel, data)
        if expected is None:
            cur = self._read_row(row)
            if cur is not None and cur[0] is not None:
                raise StoreConflict(rel)
            return self._put(row, value, None if cur is None else cur[1])
        return self._put(row, value, expected)

    @staticmethod
    def _parse_json(rel: str, data: bytes):
        try:
            return json.loads(data)
        except ValueError as e:
            raise StoreError(f"not JSON: {rel}") from e

    # --- blobs (Storage) -------------------------------------------------
    @staticmethod
    def _missing_or_error(rel: str, e: StorageException) -> StoreError:
        # storage3 reports a missing object as a 400/404 StorageApiError.
        status = str(getattr(e, "status", "") or getattr(e, "statusCode", ""))
        return StoreNotFound(rel) if status in ("400", "404") else StoreError(rel)

    def _upload(self, rel: str, data: bytes, *, upsert: bool) -> None:
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        self._bucket.upload(rel, data, {"upsert": "true" if upsert else "false", "content-type": ctype})

    def _blob_stamp(self, rel: str):
        try:
            info = self._bucket.info(rel)
        except StorageException as e:
            raise self._missing_or_error(rel, e) from e
        return (info.get("etag"), info.get("updated_at"))

    def _blob_write_if(self, rel, data, expected):
        if expected is None:
            try:
                self._upload(rel, data, upsert=False)  # atomic create-only
            except StorageException as e:
                if str(getattr(e, "status", "")) == "409" or "exists" in str(e).lower():
                    raise StoreConflict(rel) from e
                raise
        else:
            # ponytail: Storage has no conditional PUT, so this is check-then-write: two
            # PCs saving the same Fulfilment current_state.pkl inside one upload can both
            # pass, and the returned stamp may be the other PC's. Nothing else guards it;
            # gate the upload on a version row (a blob_versions table) to close it.
            try:
                current = self._blob_stamp(rel)
            except StoreNotFound:
                current = None
            if current != expected:
                raise StoreConflict(rel)
            self._upload(rel, data, upsert=True)
        return self._blob_stamp(rel)

    def _storage_ls(self, prefix: str) -> list[str]:
        out, offset = [], 0
        while True:
            page = self._bucket.list(prefix, {"limit": 1000, "offset": offset})
            out += [p["name"] for p in page if p["name"] != _PLACEHOLDER]
            if len(page) < 1000:
                return out
            offset += 1000

    def _storage_tree(self, prefix: str) -> list[str]:
        """Every object path at or under `prefix`."""
        paths = []
        for name in self._storage_ls(prefix):
            child = f"{prefix}/{name}"
            sub = self._storage_tree(child)
            paths += sub if sub else [child]
        return paths

    # --- fulfillment history (csv <-> rows) -------------------------------
    # jsonb loses key order, so a row is {"order_number", "cells": [...]} and the
    # header is element 1 as {"header": [...]}.
    def _history_version(self, cid: str) -> int:
        res = self._t("fulfillment_history_meta").select("version").eq("client_id", cid).limit(1).execute()
        return res.data[0]["version"] if res.data else 0

    def _read_history(self, cid: str, rel: str) -> bytes:
        if self._history_version(cid) == 0:
            raise StoreNotFound(rel)
        rows = self._all(lambda: self._t("fulfillment_history").select("row", count="exact")
                         .eq("client_id", cid).order("row_no"))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for item in rows:
            writer.writerow(item["row"].get("header") or item["row"].get("cells"))
        return buf.getvalue().encode("utf-8")

    def _write_history(self, cid: str, data: bytes, expected: int) -> int:
        """Replace the client's history if it is still at `expected`; the new version."""
        records = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        order_col = records[0].index("Order_Number") if records and "Order_Number" in records[0] else None
        payload = []
        for i, rec in enumerate(records):
            if i == 0:
                payload.append({"header": rec})
            else:
                payload.append({"order_number": rec[order_col] if order_col is not None and order_col < len(rec) else None,
                                "cells": rec})
        self._ensure_client(cid)
        try:
            return self._rpc("replace_fulfillment_history",
                             {"p_client": cid, "p_rows": payload, "p_expected_version": expected})
        except APIError as e:
            if e.code == VERSION_CLASH:
                raise StoreConflict(cid) from e
            raise

    # --- queries ---------------------------------------------------------
    @_guard
    def exists(self, rel):
        m = _CLIENT_DIR.fullmatch(rel)
        if m:
            return self._dir_exists(*m.group(1, 2, 3))
        if rel in ("Clients", "Sessions"):
            return True
        sm = _SESSION_SUB.fullmatch(rel)
        if sm and sm.group(3) is None:
            res = self._t("sessions").select("name").match({"client_id": sm.group(1), "name": sm.group(2)}).limit(1).execute()
            return bool(res.data)
        r = route(rel)
        if r.kind == "blob":
            return bool(self._bucket.exists(rel)) or bool(self._storage_ls(rel))
        if r.kind == "client_history":
            return self._exists_history(r)
        if r.kind == "fulfillment_history":
            return self._history_version(r.key["client_id"]) > 0
        cur = self._read_row(_row_for(rel))
        return cur is not None and cur[0] is not None

    def _exists_history(self, r) -> bool:
        res = (self._t("client_config_history").select("id").eq("client_id", r.key["client_id"])
               .eq("kind", r.key["name"]).limit(1).execute())
        return bool(res.data)

    def _dir_exists(self, root: str, archive: str | None, cid: str) -> bool:
        table = "clients" if root == "Clients" else "sessions"
        res = self._t(table).select("client_id").eq("client_id", cid).eq("archived", bool(archive)).limit(1).execute()
        return bool(res.data)

    def _col(self, table: str, col: str, **eq) -> list[str]:
        """Column `col` of every matching row. Ordered by `col` itself, so paging over
        ties may shuffle rows but never changes the values returned."""
        def query():
            q = self._t(table).select(col, count="exact")
            for k, v in eq.items():
                q = q.eq(k, v)
            return q.order(col)
        return [r[col] for r in self._all(query)]

    @_guard
    def query_sessions(self, client_id):
        rows = self._all(lambda: self._t("sessions").select("name,info", count="exact")
                         .eq("client_id", client_id).eq("archived", False)
                         .not_.is_("info", "null").order("name"))
        return [{**r["info"], "session_name": r["name"]} for r in rows]

    @_guard
    def stamp(self, rel):
        r = route(rel)
        if r.kind == "blob":
            return self._blob_stamp(rel)
        if r.kind == "fulfillment_history":
            v = self._history_version(r.key["client_id"])
            if v == 0:
                raise StoreNotFound(rel)
            return v
        if r.kind == "client_history":
            res = (self._t("client_config_history").select("id").eq("client_id", r.key["client_id"])
                   .eq("kind", r.key["name"]).order("id", desc=True).limit(1).execute())
            if not res.data:
                raise StoreNotFound(rel)
            return res.data[0]["id"]
        row = _row_for(rel)
        if row is None:
            raise ValueError(f"stamp: not a file path: {rel!r}")
        cur = self._read_row(row)
        if cur is None or cur[0] is None:
            raise StoreNotFound(rel)
        return cur[1]

    @_guard
    def list_dir(self, rel):
        names = self._list(rel.strip("/"))
        return sorted(set(names))

    def _list(self, rel: str) -> list[str]:
        if rel == "Clients":
            return [f"CLIENT_{c}" for c in self._col("clients", "client_id", archived=False)] + ["_archive"] \
                + (["groups.json"] if self._t("groups").select("id").execute().data else []) \
                + [n for n in self._storage_ls(rel)]
        if rel == "Clients/_archive":
            return [f"CLIENT_{c}" for c in self._col("clients", "client_id", archived=True)]
        if rel == "Sessions":
            return [f"CLIENT_{c}" for c in sorted(set(self._col("sessions", "client_id", archived=False)))] + ["_archive"]
        if rel == "Sessions/_archive":
            return [f"CLIENT_{c}" for c in sorted(set(self._col("sessions", "client_id", archived=True)))]
        m = re.fullmatch(r"Clients/CLIENT_([^/]+)", rel)
        if m:
            return self._client_children(m.group(1), rel)
        m = re.fullmatch(r"Clients/CLIENT_([^/]+)/backups", rel)
        if m:
            return [f"{k}.json" for k in self._col("client_config_history", "kind", client_id=m.group(1))]
        m = re.fullmatch(r"Sessions/CLIENT_([^/]+)", rel)
        if m:
            return [n for n in self._col("sessions", "name", client_id=m.group(1), archived=False)]
        m = re.fullmatch(r"Sessions/CLIENT_([^/]+)/([^/]+)/packing_lists", rel)
        if m:
            names = self._col("packing_lists", "name", client_id=m.group(1), session_name=m.group(2), withdrawn=False)
            return [f"{n}.json" for n in names] + (["withdrawn"] if self._col(
                "packing_lists", "name", client_id=m.group(1), session_name=m.group(2), withdrawn=True) else [])
        m = re.fullmatch(r"Sessions/CLIENT_([^/]+)/([^/]+)/packing_lists/withdrawn", rel)
        if m:
            return [f"{n}.json" for n in self._col(
                "packing_lists", "name", client_id=m.group(1), session_name=m.group(2), withdrawn=True)]
        m = re.fullmatch(r"Sessions/CLIENT_([^/]+)/([^/]+)/packing", rel)
        if m:
            return self._col("packing_state", "list_name", client_id=m.group(1), session_name=m.group(2)) \
                + self._storage_ls(rel)
        m = _LOCK_DIR.fullmatch(rel)
        if m:
            return self._packing_list_children(*m.groups(), rel)
        m = _SESSION_SUB.fullmatch(rel)
        if m:
            return self._session_children(m.group(1), m.group(2), m.group(3) or "", rel)
        return self._storage_ls(rel)

    def _client_children(self, cid: str, rel: str) -> list[str]:
        out = self._storage_ls(rel)
        res = self._t("clients").select("config,shopify_config,packer_config").eq("client_id", cid).eq("archived", False).execute()
        for row in res.data:
            out += [f"{n}.json" for n, col in (("client_config", "config"), ("shopify_config", "shopify_config"),
                                              ("packer_config", "packer_config")) if row[col] is not None]
        if self._col("client_config_history", "kind", client_id=cid):
            out.append("backups")
        if self._history_version(cid) > 0:
            out.append("fulfillment_history.csv")
        return out

    def _session_children(self, cid: str, sname: str, sub: str, rel: str) -> list[str]:
        out = self._storage_ls(rel)
        # session docs are flat rows keyed by their path under the session; list one level of `sub`
        for p in self._col("session_documents", "path", client_id=cid, session_name=sname):
            if not sub or p.startswith(sub + "/"):
                out.append(p[len(sub) + 1:].split("/", 1)[0] if sub else p.split("/", 1)[0])
        if sub:
            return out
        res = self._t("sessions").select("info").match({"client_id": cid, "name": sname}).execute()
        if res.data and res.data[0]["info"] is not None:
            out.append("session_info.json")
        if self._col("packing_lists", "name", client_id=cid, session_name=sname):
            out.append("packing_lists")
        if self._col("packing_state", "list_name", client_id=cid, session_name=sname):
            out.append("packing")
        return out

    def _packing_list_children(self, cid: str, sname: str, lname: str, rel: str) -> list[str]:
        out = self._storage_ls(rel)
        res = (self._t("packing_state").select("state,summary")
               .match({"client_id": cid, "session_name": sname, "list_name": lname}).execute())
        for row in res.data:
            out += [n for n, col in (("packing_state.json", "state"), ("session_summary.json", "summary"))
                    if row[col] is not None]
        if self._lock_row(cid, sname, lname):
            out.append(".session.lock")
        return out

    def _lock_row(self, cid: str, sname: str, lname: str) -> bool:
        res = (self._t("list_locks").select("holder_pc")
               .match({"client_id": cid, "session_name": sname, "list_name": lname}).execute())
        return bool(res.data)

    # --- mutations -------------------------------------------------------
    @_guard
    def move(self, src, dst):
        sm, dm = _CLIENT_DIR.fullmatch(src), _CLIENT_DIR.fullmatch(dst)
        if sm and dm and sm.group(1) == dm.group(1) and sm.group(3) == dm.group(3) and sm.group(2) != dm.group(2):
            table = "clients" if sm.group(1) == "Clients" else "sessions"
            self._t(table).update({"archived": bool(dm.group(2)), "updated_by": self.pc}).eq(
                "client_id", sm.group(3)).execute()
            return
        s, d = route(src), route(dst)
        if s.kind == "packing_list" == d.kind and {k: v for k, v in s.key.items() if k != "withdrawn"} == {
                k: v for k, v in d.key.items() if k != "withdrawn"} and s.key["withdrawn"] != d.key["withdrawn"]:
            self._t("packing_lists").update({"withdrawn": d.key["withdrawn"] == "1", "updated_by": self.pc}).match(
                {"client_id": s.key["client_id"], "session_name": s.key["session_name"], "name": s.key["name"]}
            ).execute()
            return
        if s.kind == "blob" == d.kind:
            try:
                self._bucket.move(src, dst)
            except StorageException as e:
                raise self._missing_or_error(src, e) from e
            return
        raise ValueError(f"unsupported move: {src!r} -> {dst!r}")

    @_guard
    def delete(self, rel):
        m = _SESSION_SUB.fullmatch(rel)
        if m and m.group(3) is None:  # a whole session
            self._t("sessions").delete().match({"client_id": m.group(1), "name": m.group(2)}).execute()
            self._remove_blobs(rel)
            return
        cm = re.fullmatch(r"Clients/CLIENT_([^/]+)", rel)
        if cm:
            self._t("clients").delete().eq("client_id", cm.group(1)).execute()
            self._remove_blobs(rel)
            return
        r = route(rel)
        if r.kind == "blob":
            self._remove_blobs(rel)
        elif r.kind in ("session_doc", "packing_list"):
            row = _row_for(rel)
            self._t(row.table).delete().match(row.match).execute()
        elif r.kind == "fulfillment_history":
            self._t("fulfillment_history").delete().eq("client_id", r.key["client_id"]).execute()
            self._t("fulfillment_history_meta").delete().eq("client_id", r.key["client_id"]).execute()
        elif r.kind == "client_history":
            self._t("client_config_history").delete().eq("client_id", r.key["client_id"]).eq("kind", r.key["name"]).execute()
        else:
            row = _row_for(rel)
            self._t(row.table).update({row.col: None, "updated_by": self.pc}).match(row.match).execute()

    def _remove_blobs(self, rel: str) -> None:
        paths = self._storage_tree(rel)
        if self._bucket.exists(rel):
            paths.append(rel)
        if paths:
            self._bucket.remove(paths)

    @_guard
    def claim_dir(self, rel):
        m = _CLIENT_DIR.fullmatch(rel)
        if m and not m.group(2):
            if m.group(1) == "Clients":
                return self._insert_once("clients", {"client_id": m.group(3)})
            self._ensure_client(m.group(3))
            return True
        sm = _SESSION_SUB.fullmatch(rel)
        if sm and sm.group(3) is None:
            self._ensure_client(sm.group(1))
            return self._insert_once("sessions", {"client_id": sm.group(1), "name": sm.group(2)})
        return rel not in ("Clients", "Sessions")  # other dirs are implicit

    def _insert_once(self, table: str, row: dict) -> bool:
        try:
            self._t(table).insert({**row, "updated_by": self.pc}).execute()
        except APIError as e:
            if e.code == DUPLICATE:
                return False
            raise
        return True

    @_guard
    def local_path(self, rel):
        if rel == "":
            return self.cache_dir
        path = self.cache_dir / rel
        if route(rel).kind != "blob" or "." not in rel.rsplit("/", 1)[-1]:
            return path  # a folder (Clients, Sessions/..): nothing to fetch, and no network call
        # ponytail: re-downloaded on every call, no freshness check; add an etag
        # compare when a caller needs to open the same large file repeatedly.
        try:
            data = self._bucket.download(rel)
        except StorageException as e:
            if isinstance(self._missing_or_error(rel, e), StoreNotFound):
                return path
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    # --- list locks (RPCs from the locks migration) ----------------------
    @staticmethod
    def _lock_key(list_dir: str) -> dict[str, str]:
        m = _LOCK_DIR.fullmatch(list_dir)
        if not m:
            raise ValueError(f"not a packing list dir: {list_dir!r}")
        return {"p_client": m.group(1), "p_session": m.group(2), "p_list": m.group(3)}

    def _rpc(self, name: str, params: dict):
        return self.client.rpc(name, params).execute().data

    @_guard
    def acquire_list_lock(self, list_dir, pc, user):
        key = self._lock_key(list_dir)
        out: dict = {}
        for _ in range(3):
            out = self._rpc("acquire_list_lock", {**key, "p_pc": pc, "p_user": user})
            if out["acquired"] or out["holder_pc"] is not None:
                break
            # holder released between the insert and the select: try again
        return LockInfo(bool(out["acquired"]), out["holder_pc"], out["holder_user"], _parse_ts(out["heartbeat_at"]))

    @_guard
    def force_take_list_lock(self, list_dir, pc, user, expected_heartbeat):
        if expected_heartbeat.tzinfo is None:
            expected_heartbeat = expected_heartbeat.replace(tzinfo=UTC)
        return bool(self._rpc("force_take_list_lock", {
            **self._lock_key(list_dir), "p_pc": pc, "p_user": user,
            "p_expected_heartbeat": expected_heartbeat.isoformat()}))

    @_guard
    def heartbeat_list_lock(self, list_dir, pc):
        return bool(self._rpc("heartbeat_list_lock", {**self._lock_key(list_dir), "p_pc": pc}))

    @_guard
    def release_list_lock(self, list_dir, pc):
        self._rpc("release_list_lock", {**self._lock_key(list_dir), "p_pc": pc})
