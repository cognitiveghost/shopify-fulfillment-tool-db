import pytest

from shared.store_routes import Route, route

S = "Sessions/CLIENT_ALMA/2026-10-09_1"
C = "Clients/CLIENT_ALMA"


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        (f"{C}/client_config.json", Route("clients_field", {"client_id": "ALMA", "field": "config"})),
        (f"{C}/shopify_config.json", Route("clients_field", {"client_id": "ALMA", "field": "shopify_config"})),
        (f"{C}/packer_config.json", Route("clients_field", {"client_id": "ALMA", "field": "packer_config"})),
        (f"{C}/backups/2026-01-01.json", Route("client_history", {"client_id": "ALMA", "name": "2026-01-01"})),
        ("Clients/groups.json", Route("groups", {})),
        (f"{C}/fulfillment_history.csv", Route("fulfillment_history", {"client_id": "ALMA"})),
        (f"{S}/session_info.json", Route("session_info", {"client_id": "ALMA", "session_name": "2026-10-09_1"})),
        (
            f"{S}/packing_lists/list1.json",
            Route("packing_list", {"client_id": "ALMA", "session_name": "2026-10-09_1", "name": "list1", "withdrawn": "0"}),
        ),
        (
            f"{S}/packing_lists/withdrawn/list1.json",
            Route("packing_list", {"client_id": "ALMA", "session_name": "2026-10-09_1", "name": "list1", "withdrawn": "1"}),
        ),
        (
            f"{S}/packing/list1/packing_state.json",
            Route("packing_state", {"client_id": "ALMA", "session_name": "2026-10-09_1", "list_name": "list1"}),
        ),
        (
            f"{S}/packing/list1/session_summary.json",
            Route("packing_summary", {"client_id": "ALMA", "session_name": "2026-10-09_1", "list_name": "list1"}),
        ),
        (
            f"{S}/analysis/analysis_data.json",
            Route("session_doc", {"client_id": "ALMA", "session_name": "2026-10-09_1", "path": "analysis/analysis_data.json"}),
        ),
        (f"{S}", Route("session_dir", {"client_id": "ALMA", "session_name": "2026-10-09_1"})),
        ("Workers/workers.json", Route("workers", {})),
        ("Stats/global_stats.json", Route("stats", {})),
        ("Packing/labels/a.pkl", Route("blob", {"path": "Packing/labels/a.pkl"})),
        (f"{S}/packing/list1/report.pdf", Route("blob", {"path": f"{S}/packing/list1/report.pdf"})),
        ("Clients/CLIENT_ALMA/other.csv", Route("blob", {"path": "Clients/CLIENT_ALMA/other.csv"})),
    ],
)
def test_route_table(rel, expected):
    assert route(rel) == expected


def test_unknown_json_path_raises():
    with pytest.raises(ValueError):
        route("Other/thing.json")


def test_index_files_refused():
    with pytest.raises(ValueError, match="derived"):
        route(f"{S}/session_index.json")
    with pytest.raises(ValueError, match="derived"):
        route("Clients/registry_index.json")


@pytest.mark.parametrize("rel", ["/Clients/groups.json", "Clients\\groups.json", "Clients/../groups.json", ""])
def test_bad_rel_raises(rel):
    with pytest.raises(ValueError):
        route(rel)
