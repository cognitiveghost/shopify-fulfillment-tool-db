"""Preview of the orders a pull fetched, with a tick per order.

One row per order, grouped from the line rows. Modal; returns the ticked
order names, or None on Cancel.
"""

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from shared.components.filterbar import FilterBar
from shared.i18n import _
from shared.theme import apply_dialog_button_roles, font_css

COLUMNS = ["Order", "Date", "Country", "Shipping", "Lines", "Units", "Tags"]
# The filter searches these; Date, Lines and Units are not text a user types.
_FILTER_KEYS = ("Order", "Country", "Shipping", "Tags")


def _text(value) -> str:
    return "" if pd.isna(value) else str(value)


def summarise_orders(orders_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, group in orders_df.groupby("Name", sort=False):
        head = group.iloc[0]
        rows.append({
            "Order": name,
            "Date": _text(head["Created at"])[:10],
            "Country": _text(head["Shipping Country"]),
            "Shipping": _text(head["Shipping Method"]),
            "Lines": len(group),
            "Units": int(group["Lineitem quantity"].sum()),
            "Tags": _text(head["Tags"]),
        })
    return pd.DataFrame(rows, columns=COLUMNS)


class PullPreviewDialog(QDialog):
    def __init__(self, parent, orders_df: pd.DataFrame, shop: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Pull orders from {shop}", shop=shop))
        self.setModal(True)
        self.setMinimumSize(760, 480)

        self._summary = summarise_orders(orders_df)
        self._total = len(self._summary)

        self.filter_bar = FilterBar(self)
        self.filter_bar.searchChanged.connect(self.set_filter)

        self.table = QTableWidget(self._total, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(
            [_("Order"), _("Date"), _("Country"), _("Shipping"), _("Lines"), _("Units"), _("Tags")]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self._fill_rows()
        self.table.itemChanged.connect(lambda _item: self._refresh())

        self.select_all_button = QPushButton(_("Select all"), self)
        self.select_all_button.clicked.connect(self.select_all)
        self.select_none_button = QPushButton(_("Select none"), self)
        self.select_none_button.clicked.connect(self.select_none)

        self.count_label = QLabel("", self)
        self.count_label.setStyleSheet(font_css("caption"))

        self.buttons = QDialogButtonBox(self)
        self.import_button = self.buttons.addButton(
            "", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.addButton(_("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        apply_dialog_button_roles(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        footer = QHBoxLayout()
        footer.addWidget(self.select_all_button)
        footer.addWidget(self.select_none_button)
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        footer.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self.filter_bar)
        layout.addWidget(self.table, 1)
        layout.addLayout(footer)

        self._refresh()

    def _fill_rows(self) -> None:
        for row, record in enumerate(self._summary.to_dict("records")):
            for col, key in enumerate(COLUMNS):
                item = QTableWidgetItem(str(record[key]))
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked)
                else:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, col, item)

    def selected(self) -> list[str]:
        return [
            self.table.item(row, 0).text()
            for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]

    def set_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row, record in enumerate(self._summary.to_dict("records")):
            matches = any(needle in str(record[key]).lower() for key in _FILTER_KEYS)
            self.table.setRowHidden(row, not matches)

    def _set_visible_checked(self, state: Qt.CheckState) -> None:
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.item(row, 0).setCheckState(state)

    def select_all(self) -> None:
        self._set_visible_checked(Qt.CheckState.Checked)

    def select_none(self) -> None:
        self._set_visible_checked(Qt.CheckState.Unchecked)

    def _refresh(self) -> None:
        count = len(self.selected())
        self.count_label.setText(
            _("{selected} of {total} orders selected", selected=count, total=self._total)
        )
        self.import_button.setText(_("Import {n} orders", n=count))
        self.import_button.setEnabled(count > 0)

    @classmethod
    def ask(cls, parent, orders_df: pd.DataFrame, shop: str) -> list[str] | None:
        """Show the dialog and return the ticked order names, or None on Cancel."""
        dialog = cls(parent, orders_df, shop)
        if not dialog.exec():
            return None
        return dialog.selected()
