"""Opens the user guide PDF from the server's Guides folder.

Files are `Guides/<app>-<lang>.pdf` on the server store. The app's language is tried
first, then English. A server that cannot be reached counts as missing, so the
Help action warns and never raises.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from shared import i18n
from shared.i18n import _
from shared.store import Store


def guide_pdf(store: Store, app: str) -> Path | None:
    for lang in dict.fromkeys((i18n.language(), "en")):
        path = store.local_path(f"Guides/{app}-{lang}.pdf")
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def open_guide(parent: QWidget | None, store: Store, app: str) -> None:
    path = guide_pdf(store, app)
    if path is None:
        folder = str(store.local_path("Guides"))
        QMessageBox.warning(parent, _("User guide"),
                            _("The user guide was not found in {folder}.", folder=folder))
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        QMessageBox.warning(parent, _("User guide"),
                            _("No program on this PC could open {path}.", path=str(path)))
