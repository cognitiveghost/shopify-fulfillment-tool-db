"""Path router for the Store seam: maps a server-relative `rel` to a table/bucket key.

Pure: no I/O, no Supabase import. Each rule is a fullmatch regex over the whole
`rel`; the first matching rule wins, so specific paths sit above general ones.
"""
import re
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal[
    "clients_field", "client_history", "groups", "fulfillment_history",
    "session_info", "session_doc", "packing_list", "packing_state",
    "packing_summary", "workers", "stats", "blob", "session_dir",
]

# Derived from the tables; callers query instead of reading these.
_DERIVED_INDEXES = frozenset({"session_index.json", "registry_index.json"})

_CLIENT = r"Clients/CLIENT_(?P<client_id>[^/]+)"
_SESSION = r"Sessions/CLIENT_(?P<client_id>[^/]+)/(?P<session_name>[^/]+)"


@dataclass(frozen=True)
class Route:
    kind: Kind
    key: dict[str, str] = field(compare=True)


def _rule(pattern: str, kind: Kind, **extra: str) -> tuple[re.Pattern[str], Kind, dict[str, str]]:
    return re.compile(pattern), kind, extra


# Order matters: specific session paths before the generic session_doc catch-all.
_RULES = [
    _rule(rf"{_CLIENT}/client_config\.json", "clients_field", field="config"),
    _rule(rf"{_CLIENT}/shopify_config\.json", "clients_field", field="shopify_config"),
    _rule(rf"{_CLIENT}/packer_config\.json", "clients_field", field="packer_config"),
    _rule(rf"{_CLIENT}/backups/(?P<name>[^/]+)\.json", "client_history"),
    _rule(r"Clients/groups\.json", "groups"),
    _rule(rf"{_CLIENT}/fulfillment_history\.csv", "fulfillment_history"),
    _rule(rf"{_SESSION}/session_info\.json", "session_info"),
    _rule(rf"{_SESSION}/packing_lists/(?P<name>[^/]+)\.json", "packing_list", withdrawn="0"),
    _rule(rf"{_SESSION}/packing_lists/withdrawn/(?P<name>[^/]+)\.json", "packing_list", withdrawn="1"),
    _rule(rf"{_SESSION}/packing/(?P<list_name>[^/]+)/packing_state\.json", "packing_state"),
    _rule(rf"{_SESSION}/packing/(?P<list_name>[^/]+)/session_summary\.json", "packing_summary"),
    _rule(rf"{_SESSION}/(?P<path>.+\.json)", "session_doc"),
    _rule(rf"{_SESSION}", "session_dir"),
    _rule(r"Workers/workers\.json", "workers"),
    _rule(r"Stats/global_stats\.json", "stats"),
]


def _validate(rel: str) -> None:
    if not rel or rel.startswith("/") or "\\" in rel:
        raise ValueError(f"bad store path: {rel!r}")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise ValueError(f"bad store path: {rel!r}")


def route(rel: str) -> Route:
    _validate(rel)
    if rel.rsplit("/", 1)[-1] in _DERIVED_INDEXES:
        raise ValueError("derived: query instead")
    for pattern, kind, extra in _RULES:
        match = pattern.fullmatch(rel)
        if match:
            return Route(kind, {**match.groupdict(), **extra})
    if rel.endswith(".json"):
        raise ValueError(f"unknown json store path: {rel!r}")
    return Route("blob", {"path": rel})
