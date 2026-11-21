"""Patch Manager — start here."""
from __future__ import annotations

import os
import sys
import traceback


def main():
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .library.db import DB, default_path
    from .ui.main_window import APP, MainWindow

    app = QApplication(sys.argv)
    if sys.platform == "win32":                    # one copy only: two would split the synths' replies (spec 3.3)
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "PatchManager_single_instance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            QMessageBox.warning(None, APP, "Patch Manager is already running. Use that window.")
            return 0
    app.setApplicationName(APP)
    app.setStyle("Fusion")
    # The app is drawn in light colours. Windows dark mode would otherwise give white text on light panels,
    # so every colour role is set explicitly rather than taken from the system.
    try:
        from PySide6.QtCore import Qt
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except Exception:                              # noqa: BLE001 — Qt older than 6.8
        pass
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    for role, colour in ((QPalette.Window, "#f5f5f5"), (QPalette.WindowText, "#111827"), (QPalette.Base, "#ffffff"),
                         (QPalette.AlternateBase, "#f3f4f6"), (QPalette.Text, "#111827"), (QPalette.Button, "#f3f4f6"),
                         (QPalette.ButtonText, "#111827"), (QPalette.ToolTipBase, "#ffffdc"), (QPalette.ToolTipText, "#111827"),
                         (QPalette.Highlight, "#2f80c3"), (QPalette.HighlightedText, "#ffffff"),
                         (QPalette.PlaceholderText, "#9ca3af"), (QPalette.Link, "#2563eb"), (QPalette.BrightText, "#ffffff")):
        pal.setColor(role, QColor(colour))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#9ca3af"))
    app.setPalette(pal)
    log_dir = os.path.dirname(default_path())

    def excepthook(t, v, tb):
        text = "".join(traceback.format_exception(t, v, tb))
        with open(os.path.join(log_dir, "crash log.txt"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
        QMessageBox.critical(None, APP, f"Something went wrong:\n\n{v}\n\nDetails are in {log_dir}\\crash log.txt")
    sys.excepthook = excepthook

    win = MainWindow(DB())
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
