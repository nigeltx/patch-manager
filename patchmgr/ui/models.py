"""Qt models for the patch list."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont

from ..library import ptypes
from ..library.formats import SYNTH_LABEL

COLUMNS = ["", "F", "Name", "Type", "Synth", "Pack", "Source", "Tags", "Rating", "Copies"]
C_TYPE, C_TAGS, C_RATING, C_COPIES = 3, 7, 8, 9
GREEN, AMBER, AMBER_TEXT = "#d8f0e4", "#fde7c7", "#7c2d12"
TYPE_LABEL = {"free": "Free", "factory": "Factory", "paid": "Paid", "unsorted": "Unsorted", "backup": "Backup",
              "card": "Deluge card"}
STAR = "★"


def stars(n):
    return STAR * (n or 0)


def type_text(r):
    if r["kind"] != "sound":
        return ""
    return ", ".join(ptypes.unpack(r.get("ptypes")) or [ptypes.UNKNOWN])


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
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole and section == 1:
            return "F = a factory preset. A green row is a sound that is on a synth now."
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = self.rows[index.row()], index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return "▶" if r["has_preview"] else ""
            if c == 1:
                return "F" if r.get("is_factory") else ""
            if c == 2:
                return r["name"]
            if c == C_TYPE:
                return type_text(r)
            if c == 4:
                return SYNTH_LABEL.get(r["synth"], r["synth"])
            if c == 5:
                return r["pack"] or ""
            if c == 6:
                return ", ".join(TYPE_LABEL.get(t, t) for t in sorted(set((r["types"] or "").split(",")) - {""}))
            if c == C_TAGS:
                return " ".join(t for t in (r["tags"] or "").split() if not t.endswith("?"))
            if c == C_RATING:
                return stars(r["rating"])
            if c == C_COPIES:
                return str(r["n_origins"]) if r["n_origins"] and r["n_origins"] > 1 else ""
        if role == Qt.UserRole:                     # sort key
            if c == C_RATING:
                return r["rating"] or 0
            if c == C_COPIES:
                return r["n_origins"] or 0
            return (self.data(index, Qt.DisplayRole) or "").lower()
        if role == Qt.BackgroundRole:
            if c == 1 and r.get("is_factory"):
                return QColor(AMBER)
            if r.get("on_synth"):
                return QColor(GREEN)
        if role == Qt.TextAlignmentRole and c == 1:
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole:
            if c == C_TYPE and r["kind"] == "sound" and not r.get("ptype_user"):
                return QColor("#4b5563")
            if c == C_RATING:
                return QColor("#b7791f")
            if c == 0:
                return QColor("#2563eb")
            if c == 1:
                return QColor(AMBER_TEXT)
            if r["kind"] != "sound":
                return QColor("#6b7280")
        if role == Qt.FontRole and c == 1:
            f = QFont(); f.setBold(True); return f
        if role == Qt.ToolTipRole:
            if r.get("on_synth") and c in (1, 2):
                return "On the synth now: " + (r.get("places") or "")
            if c == 1 and r.get("is_factory"):
                return "A factory preset"
            if c == C_TYPE and r["kind"] == "sound":
                return ptypes.how(r).capitalize() + " · right-click → Set type… to change it"
            if c == C_TAGS:
                sugg = [t[:-1] for t in (r["tags"] or "").split() if t.endswith("?")]
                return ("Suggested (not yet confirmed): " + ", ".join(sugg)) if sugg else None
            if c == 2 and r["kind"] != "sound":
                return {"project": "A Syntakt project — sent with Elektron Transfer",
                        "backup": "A whole-synth backup, kept as one item"}.get(r["kind"])
        if role == Qt.FontRole and c == C_TYPE and r["kind"] == "sound" and not r.get("ptype_user"):
            f = QFont(); f.setItalic(True); return f
        if role == Qt.FontRole and c == 2 and r["kind"] != "sound":
            f = QFont(); f.setItalic(True); return f
        return None

    def row(self, i):
        return self.rows[i]


class SortProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
