"""Sign-in dialog: OK waits for both fields, Enter submits, ask() returns the pair or None."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from shared.components.sign_in_dialog import SignInDialog


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _fill(dialog, email="pack@example.com", password="secret"):
    dialog.email_edit.setText(email)
    dialog.password_edit.setText(password)


def test_ok_stays_disabled_until_both_fields_are_filled(qapp):
    dialog = SignInDialog()
    assert not dialog.ok_button.isEnabled()

    dialog.email_edit.setText("pack@example.com")
    assert not dialog.ok_button.isEnabled()

    dialog.password_edit.setText("secret")
    assert dialog.ok_button.isEnabled()

    dialog.password_edit.clear()
    assert not dialog.ok_button.isEnabled()


def test_a_blank_email_does_not_count_as_filled(qapp):
    dialog = SignInDialog()
    _fill(dialog, email="   ")
    assert not dialog.ok_button.isEnabled()


def test_values_return_the_typed_pair_with_the_email_trimmed(qapp):
    dialog = SignInDialog()
    _fill(dialog, email="  pack@example.com ", password="  pw ")
    assert dialog.values() == ("pack@example.com", "  pw ")


def test_enter_in_the_password_field_submits_when_ready(qapp):
    dialog = SignInDialog()
    dialog.show()
    _fill(dialog)
    QTest.keyClick(dialog.password_edit, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_enter_does_nothing_until_both_fields_are_filled(qapp):
    dialog = SignInDialog()
    dialog.show()
    dialog.email_edit.setText("pack@example.com")
    QTest.keyClick(dialog.email_edit, Qt.Key.Key_Return)
    assert dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_ask_returns_the_typed_pair_when_accepted(qapp, monkeypatch):
    def accepted_exec(self):
        _fill(self)
        self.accept()
        return int(self.result())

    monkeypatch.setattr(QDialog, "exec", accepted_exec)
    assert SignInDialog.ask() == ("pack@example.com", "secret")


def test_ask_returns_none_when_rejected(qapp, monkeypatch):
    def rejected_exec(self):
        _fill(self)
        self.reject()
        return int(self.result())

    monkeypatch.setattr(QDialog, "exec", rejected_exec)
    assert SignInDialog.ask() is None
