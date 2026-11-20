import numpy as np
import pandas as pd

from gui.pull_preview_dialog import PullPreviewDialog, summarise_orders

FRAME = pd.DataFrame(
    [
        {"Name": "#1001", "Created at": "2026-11-18 10:00:00 +0200", "Shipping Country": "BG",
         "Shipping Method": "DHL", "Tags": "vip", "Lineitem quantity": 1},
        {"Name": "#1001", "Created at": "2026-11-18 10:00:00 +0200", "Shipping Country": "BG",
         "Shipping Method": "DHL", "Tags": "vip", "Lineitem quantity": 2},
        {"Name": "#1002", "Created at": "2026-11-19 09:00:00 +0200", "Shipping Country": np.nan,
         "Shipping Method": "Econt", "Tags": np.nan, "Lineitem quantity": 3},
        {"Name": "#1003", "Created at": "2026-11-19 11:00:00 +0200", "Shipping Country": "DE",
         "Shipping Method": "DHL", "Tags": "", "Lineitem quantity": 1},
    ]
)


def test_summary_groups_lines():
    s = summarise_orders(FRAME)
    assert list(s["Order"]) == ["#1001", "#1002", "#1003"]
    assert list(s["Lines"]) == [2, 1, 1] and list(s["Units"]) == [3, 3, 1]
    assert s.loc[0, "Date"] == "2026-11-18"


def test_summary_handles_missing_fields():
    s = summarise_orders(FRAME)
    assert s.loc[1, "Country"] == "" and s.loc[1, "Tags"] == ""


def test_all_ticked_by_default(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    assert d.selected() == ["#1001", "#1002", "#1003"]
    assert d.import_button.isEnabled()


def test_filter_hides_and_select_none_acts_on_visible(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    d.set_filter("dhl")
    d.select_none()
    d.set_filter("")
    assert d.selected() == ["#1002"]


def test_filter_matches_tags_case_insensitive(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    d.select_none()
    d.set_filter("VIP")
    d.select_all()
    d.set_filter("")
    assert d.selected() == ["#1001"]


def test_import_disabled_at_zero(qapp):
    d = PullPreviewDialog(None, FRAME, "alma")
    d.select_none()
    assert not d.import_button.isEnabled()
    assert "0 of 3" in d.count_label.text()
