"""Qt models for the patch list."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont

from ..library.formats import SYNTH_LABEL

COLUMNS = ["", "Name", "Synth", "Pack", "Source", "Tags", "Rating", "Copies"]
TYPE_LABEL = {"free": "Free", "factory": "Factory", "paid": "Paid", "unsorted": "Unsorted", "backup": "Backup",
              "card": "Deluge card"}
STAR = "★"


def stars(n):
    return STAR * (n or 0)


class PatchModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = [dict(r) for r in rows]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole and section == 0:
            return "▶ = a preview recording is available (press Space to play it)"
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = self.rows[index.row()], index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return "▶" if r["has_preview"] else ""
            if c == 1:
                return r["name"]
            if c == 2:
                return SYNTH_LABEL.get(r["synth"], r["synth"])
            if c == 3:
                return r["pack"] or ""
            if c == 4:
                return ", ".join(TYPE_LABEL.get(t, t) for t in sorted(set((r["types"] or "").split(",")) - {""}))
            if c == 5:
                return " ".join(t for t in (r["tags"] or "").split() if not t.endswith("?"))
            if c == 6:
                return stars(r["rating"])
            if c == 7:
                return str(r["n_origins"]) if r["n_origins"] and r["n_origins"] > 1 else ""
        if role == Qt.UserRole:                     # sort key
            if c == 6:
                return r["rating"] or 0
            if c == 7:
                return r["n_origins"] or 0
            return (self.data(index, Qt.DisplayRole) or "").lower()
        if role == Qt.ForegroundRole:
            if c == 6:
                return QColor("#b7791f")
            if c == 0:
                return QColor("#2563eb")
            if r["kind"] != "sound":
                return QColor("#6b7280")
        if role == Qt.ToolTipRole:
            if c == 5:
                sugg = [t[:-1] for t in (r["tags"] or "").split() if t.endswith("?")]
                return ("Suggested (not yet confirmed): " + ", ".join(sugg)) if sugg else None
            if c == 1 and r["kind"] != "sound":
                return {"project": "A Syntakt project — sent with Elektron Transfer",
                        "backup": "A whole-synth backup, kept as one item"}.get(r["kind"])
        if role == Qt.FontRole and c == 1 and r["kind"] != "sound":
            f = QFont(); f.setItalic(True); return f
        return None

    def row(self, i):
        return self.rows[i]


class SortProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
