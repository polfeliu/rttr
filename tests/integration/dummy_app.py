"""Minimal PyQt5 application for RTTR integration tests.

Launch as a subprocess — the window title ``RTTR Dummy App`` is used by
test fixtures to locate the process and connect via pywinauto.

All widgets have explicit ``objectName`` (→ UIA ``AutomationId``) so that
tests can find elements reliably.
"""

from __future__ import annotations

import sys

from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenuBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class TestDialog(QDialog):
    """Simple modal dialog with OK / Cancel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Test Dialog")
        self.setObjectName("dlg_test")
        layout = QVBoxLayout(self)

        self.lbl_dialog = QLabel("This is a test dialog")
        self.lbl_dialog.setObjectName("lbl_dialog")
        layout.addWidget(self.lbl_dialog)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.setObjectName("dialog_button_box")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)


class DummyMainWindow(QMainWindow):
    """Main window with counter, echo, toggle, and menu."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RTTR Dummy App")
        self.setObjectName("main_window")
        self.resize(420, 280)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        # --- Row 1: click button + counter label ---
        row1 = QHBoxLayout()
        self.btn_click = QPushButton("Click Me")
        self.btn_click.setObjectName("btn_click")
        self._counter = 0
        self.lbl_counter = QLabel("Counter: 0")
        self.lbl_counter.setObjectName("lbl_counter")
        self.btn_click.clicked.connect(self._increment)
        row1.addWidget(self.btn_click)
        row1.addWidget(self.lbl_counter)
        root_layout.addLayout(row1)

        # --- Row 2: text input + echo label ---
        row2 = QHBoxLayout()
        self.txt_input = QLineEdit()
        self.txt_input.setObjectName("txt_input")
        self.txt_input.setPlaceholderText("Type here…")
        self.lbl_echo = QLabel("Echo:")
        self.lbl_echo.setObjectName("lbl_echo")
        self.txt_input.textChanged.connect(self._echo)
        row2.addWidget(self.txt_input)
        row2.addWidget(self.lbl_echo)
        root_layout.addLayout(row2)

        # --- Row 3: checkbox + toggleable button ---
        row3 = QHBoxLayout()
        self.chk_enable = QCheckBox("Enable Button")
        self.chk_enable.setObjectName("chk_enable")
        self.chk_enable.setChecked(True)
        self.btn_toggleable = QPushButton("Toggleable")
        self.btn_toggleable.setObjectName("btn_toggleable")
        self.chk_enable.toggled.connect(self.btn_toggleable.setEnabled)
        row3.addWidget(self.chk_enable)
        row3.addWidget(self.btn_toggleable)
        root_layout.addLayout(row3)

        # --- Status label ---
        self.lbl_status = QLabel("Status: Ready")
        self.lbl_status.setObjectName("lbl_status")
        root_layout.addWidget(self.lbl_status)

        # --- Menu bar ---
        menu_bar = QMenuBar(self)
        self.setMenuBar(menu_bar)
        file_menu = menu_bar.addMenu("File")
        open_dialog_action = file_menu.addAction("Open Dialog...")
        open_dialog_action.triggered.connect(self._open_dialog)

    # -- slots --

    def _increment(self) -> None:
        self._counter += 1
        self.lbl_counter.setText(f"Counter: {self._counter}")

    def _echo(self, text: str) -> None:
        self.lbl_echo.setText(f"Echo: {text}")

    def _open_dialog(self) -> None:
        self.lbl_status.setText("Status: Dialog Open")
        dlg = TestDialog(self)
        dlg.exec_()
        self.lbl_status.setText("Status: Ready")


def main() -> None:
    """Entry point when run as a subprocess."""
    app = QApplication(sys.argv)
    window = DummyMainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
