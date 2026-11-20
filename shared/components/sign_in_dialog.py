"""Email and password for the server sign-in. Sign in stays off until both are filled.

Modal, like ConfirmDialog. Returns the typed pair plus the remember-me choice, or None when cancelled.
"""

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from shared.i18n import _
from shared.theme import apply_dialog_button_roles, font_css, on_theme_changed


class SignInDialog(QDialog):
    def __init__(self, parent=None, error: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Sign in"))
        self.setModal(True)
        self.setMinimumWidth(320)  # a full email address must fit unclipped

        self.error_label = QLabel(error or "", self)
        self.error_label.setObjectName("SignInError")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(bool(error))
        on_theme_changed(
            self.error_label,
            lambda tokens, w=self.error_label: w.setStyleSheet(
                f"{font_css('body')} color: {tokens.status_danger};"
            ),
        )

        self.email_edit = QLineEdit(self)
        self.password_edit = QLineEdit(self)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember_box = QCheckBox(_("Remember me on this PC"), self)

        form = QFormLayout()
        form.addRow(_("Email"), self.email_edit)
        form.addRow(_("Password"), self.password_edit)
        form.addRow(self.remember_box)

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
        layout.addWidget(self.error_label)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

        self.email_edit.textChanged.connect(self._update_ok)
        self.password_edit.textChanged.connect(self._update_ok)
        # Enter in either field submits, but only once the form is complete.
        self.email_edit.returnPressed.connect(self._submit)
        self.password_edit.returnPressed.connect(self._submit)

    def values(self) -> tuple[str, str, bool]:
        return (
            self.email_edit.text().strip(),
            self.password_edit.text(),
            self.remember_box.isChecked(),
        )

    def _update_ok(self) -> None:
        email, password, _remember = self.values()
        self.ok_button.setEnabled(bool(email) and bool(password))

    def _submit(self) -> None:
        if self.ok_button.isEnabled():
            self.accept()

    @classmethod
    def ask(cls, parent=None, error: str | None = None) -> tuple[str, str, bool] | None:
        """Show the dialog and return (email, password, remember) if accepted, else None."""
        dialog = cls(parent, error)
        if not dialog.exec():
            return None
        return dialog.values()
