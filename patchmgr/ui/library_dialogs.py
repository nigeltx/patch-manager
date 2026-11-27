"""Library dialogs (spec 4.4, 4.5, 4.11): Rebuild Patches, Reorganize library, and Sources changed or missing.
Each shows its plan first and does nothing until you press the button that says what will happen."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..library import layout, rebuild
from ..library.formats import SYNTH_LABEL
from .theme import paint
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
                paint(it, colour)
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
                       "Free, User), <b>Patches</b> and <b>Backups</b>. Folders are moved, never changed. Backups from "
                       "<i>_Patch Manager › Backups</i> move to each synth's own Backups folder; backups are dumps of the "
                       "synth, never sources. Before a factory read moves, its factory sounds are copied into "
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


# ---------------------------------------------------------------- Duplicates in Sources (spec 4.12)
class DuplicatesDialog(QDialog):
    def __init__(self, parent, db, root):
        from PySide6.QtWidgets import QComboBox, QTabWidget, QTreeWidget, QTreeWidgetItem
        from ..library import duplicates as dup
        self.dup, self.QTreeWidgetItem = dup, QTreeWidgetItem
        super().__init__(parent)
        self.db, self.root = db, root
        self.changed_anything = False
        self.setWindowTitle("Duplicates in Sources")
        self.resize(1000, 660)
        v = QVBoxLayout(self)
        intro = QLabel("These sounds are in more than one place in Sources. That is allowed, and nothing here changes a "
                       "file in Sources. For each one, <b>keep all</b> copies (the default), or <b>keep one</b>: that "
                       "copy becomes the source of truth and the others are set aside — left in their files but no "
                       "longer counted. Patches holds each sound once either way.")
        intro.setWordWrap(True); intro.setTextFormat(Qt.RichText); v.addWidget(intro)
        top = QHBoxLayout()
        top.addWidget(QLabel("Synth"))
        self.synth = QComboBox(); self.synth.addItem("All synths", None)
        for s in layout.SYNTH_ORDER:
            self.synth.addItem(SYNTH_LABEL[s], s)
        self.synth.currentIndexChanged.connect(self.fill)
        top.addWidget(self.synth); top.addStretch(1)
        self.summary = QLabel(); top.addWidget(self.summary)
        v.addLayout(top)
        self.tabs = QTabWidget(); v.addWidget(self.tabs, 1)
        # by sound
        w = QWidget(); wl = QVBoxLayout(w); wl.setContentsMargins(0, 4, 0, 0)
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["Sound and its copies", "Sounds in that file"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setColumnCount(2)
        self.tree.itemDoubleClicked.connect(lambda it, c: self.keep_selected())
        wl.addWidget(self.tree, 1)
        b = QHBoxLayout()
        k1 = QPushButton("Keep the selected copy"); k1.clicked.connect(self.keep_selected)
        k2 = QPushButton("Keep all copies"); k2.clicked.connect(self.keep_all_selected)
        b.addWidget(k1); b.addWidget(k2); b.addStretch(1); wl.addLayout(b)
        self.tabs.addTab(w, "By sound")
        # by pair of files
        w2 = QWidget(); wl2 = QVBoxLayout(w2); wl2.setContentsMargins(0, 4, 0, 0)
        self.ptable = _table(["File A", "File B", "Shared", "Now"], 0)
        self.ptable.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.ptable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        wl2.addWidget(self.ptable, 1)
        b2 = QHBoxLayout()
        for text, side in (("Keep A's copies", "a"), ("Keep B's copies", "b"), ("Keep both", "both")):
            btn = QPushButton(text); btn.clicked.connect(lambda _=False, sd=side: self.keep_pairs(sd)); b2.addWidget(btn)
        b2.addStretch(1); wl2.addLayout(b2)
        self.tabs.addTab(w2, "By pair of files")
        # files with nothing left
        w3 = QWidget(); wl3 = QVBoxLayout(w3); wl3.setContentsMargins(0, 4, 0, 0)
        l3 = QLabel("Every sound in these files is set aside, so they do nothing any more. <b>Move out of Sources</b> "
                    "puts them on the Studio Cleanup list; Clean up Studio.cmd moves them to Archive. Files only partly "
                    "set aside stay — the app never edits a pack file.")
        l3.setWordWrap(True); l3.setTextFormat(Qt.RichText); wl3.addWidget(l3)
        self.ftable = _table(["File"], 0); wl3.addWidget(self.ftable, 1)
        b3 = QHBoxLayout(); mv = QPushButton("Move out of Sources"); mv.clicked.connect(self.move_out)
        b3.addWidget(mv); b3.addStretch(1); wl3.addLayout(b3)
        self.tabs.addTab(w3, "Files with nothing left")
        btns = QHBoxLayout(); btns.addStretch(1)
        close = QPushButton("Close"); close.setDefault(True); close.clicked.connect(self.accept); btns.addWidget(close)
        v.addLayout(btns)
        self.fill()

    def fill(self):
        _busy(True)
        try:
            synth = self.synth.currentData()
            self.groups = self.dup.groups(self.db, synth)
            self.pairs = self.dup.pairs(self.groups)
            self.files = [f for f in self.dup.set_aside_files(self.db)
                          if not synth or _rel(self.root, f).split(os.sep)[0] == SYNTH_LABEL[synth]]
        finally:
            _busy(False)
        self.tree.clear()
        for gi, g in enumerate(self.groups):
            it = self.QTreeWidgetItem([f"{g.name}  —  {SYNTH_LABEL.get(g.synth, g.synth)} · {len(g.copies)} copies · "
                                       f"{g.state()}", ""])
            it.setData(0, Qt.UserRole, (gi, None))
            f = it.font(0); f.setBold(True); it.setFont(0, f)
            for ci, c in enumerate(g.copies):
                ch = self.QTreeWidgetItem([("✓ " if c.kept else "set aside — ") + c.label(), str(c.n_in_file)])
                ch.setData(0, Qt.UserRole, (gi, ci))
                if not c.kept:
                    ch.setForeground(0, QColor("#9ca3af"))
                it.addChild(ch)
            self.tree.addTopLevelItem(it)
        _fill(self.ptable, [([p.label(p.a), p.label(p.b), str(len(p.shared)), self._pair_state(p)], None)
                            for p in self.pairs])
        _fill(self.ftable, [([_rel(self.root, f)], GREY) for f in self.files])
        chosen = sum(1 for g in self.groups if g.keep)
        self.summary.setText(f"{len(self.groups):,} sounds in more than one place · {chosen:,} with one copy kept · "
                             f"{len(self.files)} files with nothing left")
        self.tabs.setTabText(2, f"Files with nothing left ({len(self.files)})")

    def _pair_state(self, p):
        ka = sum(1 for g, a, b in p.shared if a.kept and not b.kept)
        kb = sum(1 for g, a, b in p.shared if b.kept and not a.kept)
        if not ka and not kb:
            return "both kept"
        return f"A kept for {ka}" if ka and not kb else f"B kept for {kb}" if kb and not ka else f"A {ka} · B {kb}"

    def _changed(self):
        self.changed_anything = True
        self.dup.save(self.db, self.root)
        self.fill()

    def keep_selected(self):
        it = self.tree.currentItem()
        d = it.data(0, Qt.UserRole) if it else None
        if not d or d[1] is None:
            QMessageBox.information(self, self.windowTitle(), "Select one copy (a line under a sound) first."); return
        g = self.groups[d[0]]; c = g.copies[d[1]]
        self.dup.keep_one(self.db, g.hash, c.rel, c.idx)
        self._changed()

    def keep_all_selected(self):
        it = self.tree.currentItem()
        d = it.data(0, Qt.UserRole) if it else None
        if not d:
            return
        self.dup.keep_all(self.db, self.groups[d[0]].hash)
        self._changed()

    def keep_pairs(self, side):
        rows = sorted({ix.row() for ix in self.ptable.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, self.windowTitle(), "Select one or more pairs of files first."); return
        for r in rows:
            self.dup.keep_side(self.db, self.pairs[r], side)
        self._changed()

    def move_out(self):
        if not self.files:
            return
        if QMessageBox.question(self, self.windowTitle(), f"Put {len(self.files)} file(s) on the Studio Cleanup list? "
                                "Clean up Studio.cmd then moves them to Archive.") != QMessageBox.Yes:
            return
        if not self.dup.move_out(self.root, self.files):
            QMessageBox.warning(self, self.windowTitle(), "The Studio Cleanup list was not found in Studio › Software › "
                                                          "Studio Cleanup."); return
        QMessageBox.information(self, self.windowTitle(), "Added. Run Clean up Studio.cmd when convenient.")
