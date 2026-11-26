"""Library dialogs (spec 4.4, 4.5, 4.11): Rebuild Patches, Reorganize library, and Sources changed or missing.
Each shows its plan first and does nothing until you press the button that says what will happen."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..library import layout, rebuild
from ..library.formats import SYNTH_LABEL
from .models import AMBER, GREEN

GREY = "#f3f4f6"


def _table(headers, stretch_col):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setWordWrap(False)
    for c in range(len(headers)):
        t.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch if c == stretch_col
                                                  else QHeaderView.ResizeToContents)
    return t


def _fill(t, rows):
    """rows: (cells, colour)."""
    t.setRowCount(len(rows))
    for i, (cells, colour) in enumerate(rows):
        for c, v in enumerate(cells):
            it = QTableWidgetItem(v)
            if colour:
                it.setBackground(QColor(colour))
            t.setItem(i, c, it)


def _busy(on):
    if on:
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
    else:
        QGuiApplication.restoreOverrideCursor()


def _rel(root, p):
    try:
        return os.path.relpath(p, root)
    except ValueError:
        return p


# ---------------------------------------------------------------- Rebuild
class RebuildDialog(QDialog):
    def __init__(self, parent, db, root, synths=layout.SYNTH_ORDER, quiet_if_nothing=False):
        super().__init__(parent)
        self.db, self.root = db, root
        self.result_ = None
        self.setWindowTitle("Rebuild Patches")
        self.resize(900, 600)
        v = QVBoxLayout(self)
        intro = QLabel("Patches holds every sound in Sources once, in a folder for its type. This is what a rebuild "
                       "would change. Nothing happens until you press <b>Rebuild</b>. Files taken out go to "
                       "<i>_Patch Manager › Removed</i>; nothing is deleted.")
        intro.setWordWrap(True); intro.setTextFormat(Qt.RichText); v.addWidget(intro)
        _busy(True)
        try:
            self.plan = rebuild.plan(db, root, synths)
        finally:
            _busy(False)
        self.nothing = self.plan.empty
        self.summary = QLabel(); v.addWidget(self.summary)
        self.table = _table(["Change", "Synth", "File in Patches", "Why"], 2)
        v.addWidget(self.table, 1)
        rows = []
        for dst, w in self.plan.adds:
            rows.append((["Add", SYNTH_LABEL[w.synth], self._p(dst), "new in Sources"], GREEN))
        for src, dst, _ in self.plan.moves:
            rows.append((["Move", self._synth(dst), self._p(dst), "was " + self._p(src)], "#dbeafe"))
        for p, dst in self.plan.hand:
            rows.append((["Keep in Sources › User", self._synth(p), self._p(p), "you put it there; its sound is in no source"],
                         AMBER))
        for p, why in self.plan.removes:
            rows.append((["Take out", self._synth(p), self._p(p), why], GREY))
        _fill(self.table, rows)
        self.summary.setText("<b>Patches is up to date.</b>" if self.nothing else
                             f"<b>{self.plan.summary()}.</b> {len(self.plan.keep):,} files are already right.")
        btns = QHBoxLayout(); btns.addStretch(1)
        self.go = QPushButton("Rebuild"); self.go.setDefault(True); self.go.clicked.connect(self.apply)
        self.go.setEnabled(not self.nothing)
        close = QPushButton("Close" if self.nothing else "Cancel"); close.clicked.connect(self.reject)
        btns.addWidget(self.go); btns.addWidget(close); v.addLayout(btns)

    def _p(self, path):
        parts = _rel(self.root, path).split(os.sep)
        return "/".join(parts[2:]) if len(parts) > 2 and parts[1].lower() == "patches" else "/".join(parts)

    def _synth(self, path):
        return _rel(self.root, path).split(os.sep)[0]

    def apply(self):
        _busy(True)
        try:
            self.result_ = rebuild.apply(self.db, self.plan)
        finally:
            _busy(False)
        r = self.result_
        msg = f"Added {r['added']}, moved {r['moved']}, took out {r['removed']}, kept {r['hand']} in Sources › User."
        if r["errors"]:
            QMessageBox.warning(self, "Rebuild", msg + "\n\nThese could not be done:\n" + "\n".join(r["errors"][:20]))
        self.accept()


# ---------------------------------------------------------------- Reorganize
class ReorganizeDialog(QDialog):
    def __init__(self, parent, db, root):
        super().__init__(parent)
        self.db, self.root = db, root
        self.result_ = None
        self.setWindowTitle("Reorganize library")
        self.resize(940, 600)
        v = QVBoxLayout(self)
        intro = QLabel("Moves your patch folders into the new layout once: each synth gets <b>Sources</b> (Factory, Paid, "
                       "Free, User) and <b>Patches</b>. Folders are moved, never changed. The factory reads in "
                       "<i>_Patch Manager › Backups</i> stay where they are; their factory sounds are copied into "
                       "Sources › Factory and your own sounds into Sources › User. Folders left empty go on the Studio "
                       "Cleanup list. Nothing happens until you press <b>Reorganize</b>.")
        intro.setWordWrap(True); intro.setTextFormat(Qt.RichText); v.addWidget(intro)
        self.plan = layout.reorganize_plan(root)
        self.table = _table(["Change", "From", "To"], 2)
        v.addWidget(self.table, 1)
        rows = []
        for m in self.plan.moves:
            rows.append((["Move — " + m.why, _rel(root, m.src), _rel(root, m.dst)], "#dbeafe"))
        for synth, f in self.plan.splits:
            rows.append((["Split a factory read", _rel(root, f),
                          f"{SYNTH_LABEL[synth]} › Sources › Factory and › User"], GREEN))
        for d in self.plan.make:
            rows.append((["New folder", "", _rel(root, d)], None))
        for p, why in self.plan.skipped:
            rows.append((["Left alone — " + why, _rel(root, p), ""], GREY))
        _fill(self.table, rows)
        s = QLabel("<b>Already reorganized — nothing to do.</b>" if self.plan.empty else
                   f"<b>{len(self.plan.moves)} to move, {len(self.plan.splits)} factory reads to split, "
                   f"{len(self.plan.make)} folders to make.</b>")
        v.addWidget(s)
        btns = QHBoxLayout(); btns.addStretch(1)
        self.go = QPushButton("Reorganize"); self.go.setDefault(True); self.go.clicked.connect(self.apply)
        self.go.setEnabled(not self.plan.empty)
        close = QPushButton("Close" if self.plan.empty else "Cancel"); close.clicked.connect(self.reject)
        btns.addWidget(self.go); btns.addWidget(close); v.addLayout(btns)

    def apply(self):
        _busy(True)
        try:
            self.result_ = layout.reorganize_apply(self.root, self.plan, db=self.db)
        finally:
            _busy(False)
        moved, errors, listed = self.result_
        if errors:
            QMessageBox.warning(self, "Reorganize", f"{moved} done. These could not be done:\n" + "\n".join(errors[:20]))
        self.accept()


# ---------------------------------------------------------------- Sources changed or missing
class AlertsDialog(QDialog):
    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.changed_anything = False
        self.setWindowTitle("Sources changed or missing")
        self.resize(900, 520)
        v = QVBoxLayout(self)
        intro = QLabel("These files in Sources changed or disappeared outside the app. Until you decide, the app keeps "
                       "using the sounds it read before. <b>Changed:</b> restore the old version from Google Drive "
                       "(right-click the file on drive.google.com › File information › Manage versions), or use the new "
                       "one. <b>Missing:</b> put it back from Google Drive's trash (kept for 30 days), or accept that "
                       "it has gone.")
        intro.setWordWrap(True); intro.setTextFormat(Qt.RichText); v.addWidget(intro)
        self.table = _table(["", "State", "Synth", "File", "Sounds"], 3)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        v.addWidget(self.table, 1)
        btns = QHBoxLayout()
        self.use_new = QPushButton("Use the new version"); self.use_new.clicked.connect(lambda: self._accept("changed"))
        self.gone = QPushButton("Accept it has gone"); self.gone.clicked.connect(lambda: self._accept("missing"))
        btns.addWidget(self.use_new); btns.addWidget(self.gone); btns.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.accept); btns.addWidget(close)
        v.addLayout(btns)
        self.fill()

    def fill(self):
        self.rows = self.db.source_alerts()
        _fill(self.table, [(["", "Changed" if r["state"] == "changed" else "Missing", SYNTH_LABEL.get(r["synth"], ""),
                             r["rel"], str(r["n"])], AMBER if r["state"] == "changed" else GREY) for r in self.rows])

    def _accept(self, state):
        picked = {ix.row() for ix in self.table.selectionModel().selectedRows()}
        rows = [self.rows[i] for i in sorted(picked) if self.rows[i]["state"] == state]
        if not rows:
            QMessageBox.information(self, self.windowTitle(), f"Select one or more {state} files first.")
            return
        for r in rows:
            (self.db.accept_change if state == "changed" else self.db.accept_missing)(r["id"])
        self.changed_anything = True
        self.fill()
