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
    # The colours follow Windows' light or dark mode (Nigel, 27 Nov 2026); ui/theme.py keeps text readable on the
    # app's own highlight colours.
    log_dir = os.path.dirname(default_path())

    def excepthook(t, v, tb):
        text = "".join(traceback.format_exception(t, v, tb))
        with open(os.path.join(log_dir, "crash log.txt"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
        QMessageBox.critical(None, APP, f"Something went wrong:\n\n{v}\n\nDetails are in {log_dir}\\crash log.txt")
    sys.excepthook = excepthook

    win = MainWindow(DB())
    win.show()
    win.updates.start()                            # checks GitHub for a new version, as set in Help ▾ › Update settings
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
