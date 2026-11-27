"""Follow the Windows theme (Nigel, 27 Nov 2026): the app takes Windows' light or dark colours.

The app's own highlight colours (green "on the synth", amber "factory", the slot grids) are light, so wherever one is
painted the text on it is set dark, and the few coloured texts drawn on the normal background get a lighter shade in
dark mode. Everything else comes from the system palette.
"""
from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPalette

INK = "#111827"                       # text on the app's light highlight colours


def is_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    return app.palette().color(QPalette.Window).lightness() < 128


def tone(light: str, dark: str) -> QColor:
    """A text colour that reads on the normal background in either theme."""
    return QColor(dark if is_dark() else light)


def paint(item, colour: str | None, ink: str | None = None):
    """Give a table/tree item a highlight background with readable text — or, for None / white, the theme's own."""
    if not colour or colour.lower() in ("#ffffff", "#fff", "white"):
        item.setBackground(QBrush())
        item.setForeground(QBrush())
        return
    item.setBackground(QColor(colour))
    item.setForeground(QColor(ink or INK))
