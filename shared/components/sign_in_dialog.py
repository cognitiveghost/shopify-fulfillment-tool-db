"""Email and password for the server sign-in. Sign in stays off until both are filled.

Modal, like ConfirmDialog. Returns the typed pair, or None when cancelled.
"""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
)

from shared.i18n import _
from shared.theme import apply_dialog_button_roles


class SignInDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Sign in"))
        self.setModal(True)
        self.setMinimumWidth(320)  # a full email address must fit unclipped

        self.email_edit = QLineEdit(self)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow(_("Email"), self.email_edit)
        form.addRow(_("Password"), self.password_edit)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(_("Sign in"))
        self.ok_button.setEnabled(False)
        apply_dialog_button_roles(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

        self.email_edit.textChanged.connect(self._update_ok)
        self.password_edit.textChanged.connect(self._update_ok)
        # Enter in either field submits, but only once the form is complete.
        self.email_edit.returnPressed.connect(self._submit)
        self.password_edit.returnPressed.connect(self._submit)

    def values(self) -> tuple[str, str]:
        return self.email_edit.text().strip(), self.password_edit.text()

    def _update_ok(self) -> None:
        email, password = self.values()
        self.ok_button.setEnabled(bool(email) and bool(password))

    def _submit(self) -> None:
        if self.ok_button.isEnabled():
            self.accept()

    @classmethod
    def ask(cls, parent=None) -> tuple[str, str] | None:
        """Show the dialog and return (email, password) if accepted, else None."""
        dialog = cls(parent)
        if not dialog.exec():
            return None
        return dialog.values()
