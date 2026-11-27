"""Targets › Presets as the synth lays them out (Nigel, 27 Nov 2026): banks down, programs across.

  Take 5   U1–U16 then F1–F16, 16 programs each (32 rows)
  Sub 37   banks 1–16, 16 presets each
  Syntakt  one sound bank at a time (A–H, chosen above the grid), 128 slots as 8 rows of 16

What each slot holds comes from the last read of the synth (slot_state). Colours: green — a patch in your library;
amber — a sound in no source yet (Get from synth keeps it); grey text — a factory bank; blank — empty or not read.
Clicking a slot holding a library patch selects it, as if it were picked in the list. Right-click initializes the
selected slots (Nigel, 27 Nov 2026): Take 5 user slots to Basic Program, Syntakt bank H slots to empty. Factory banks,
H128 (audition) and the Sub 37 (until its writes arrive) are not offered.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMenu, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..library import factory
from .theme import is_dark, paint
from ..library.formats import SYNTAKT_BANKS, place

GREEN, AMBER, FACTORY_BG = "#d8f0e4", "#fde8c8", "#eef1f5"


def layout_for(synth: str, syn_bank: int = 1):
    """-> (row labels, column labels, [(row, col, bank, slot)])."""
    cells = []
    if synth == "take5":
        banks = list(range(1, 33))                   # 1–16 user, 17–32 factory
        rows = [place("take5", b, 1).split("-")[0] for b in banks]
        for r, b in enumerate(banks):
            for c in range(16):
                cells.append((r, c, b, c + 1))
        return rows, [f"{c + 1:02d}" for c in range(16)], cells
    if synth == "sub37":
        rows = [f"Bank {b}" for b in range(1, 17)]
        for r in range(16):
            for c in range(16):
                cells.append((r, c, r + 1, c + 1))
        return rows, [str(c + 1) for c in range(16)], cells
    letter = SYNTAKT_BANKS[syn_bank - 1]
    rows = [f"{letter}{r * 16 + 1:03d}" for r in range(8)]
    for s in range(1, 129):
        r, c = divmod(s - 1, 16)
        cells.append((r, c, syn_bank, s))
    return rows, [f"+{c + 1}" for c in range(16)], cells


class BankView(QWidget):
    picked = Signal(str)                             # the hash of a library patch clicked in the grid
    initialize = Signal(str, list)                   # synth, [(bank, slot)] to set to the empty program

    def __init__(self, db):
        super().__init__()
        self.db, self.synth = db, None
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
        top = QHBoxLayout()
        self.title = QLabel(); self.title.setTextFormat(Qt.RichText)
        top.addWidget(self.title, 1)
        self.bank_lbl = QLabel("Sound bank:")
        self.bank = QComboBox(); self.bank.addItems(list(SYNTAKT_BANKS))
        self.bank.currentIndexChanged.connect(lambda _: self.fill())
        top.addWidget(self.bank_lbl); top.addWidget(self.bank)
        v.addLayout(top)
        self.grid = QTableWidget()
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.grid.verticalHeader().setDefaultSectionSize(22)
        self.grid.setStyleSheet("QTableWidget { font-size: 8pt; } "
                                "QTableWidget::item:selected { background: #2f80c3; color: white; }")
        self.grid.cellClicked.connect(self._clicked)
        v.addWidget(self.grid, 1)
        self.legend = QLabel(
            f"<span style='background:{GREEN}; color:#111827'>&nbsp;in your library&nbsp;</span> &nbsp; "
            f"<span style='background:{AMBER}; color:#111827'>&nbsp;in no source yet — Get from synth keeps it&nbsp;</span> &nbsp; "
            f"<span style='background:{FACTORY_BG}; color:#8b919a'>&nbsp;factory bank&nbsp;</span> &nbsp; blank: empty or not read")
        self.legend.setTextFormat(Qt.RichText)
        v.addWidget(self.legend)
        self.cells = {}
        self.where = {}                              # (row, col) → (bank, slot)

    def show_synth(self, synth: str, label: str, library_hashes: set[str]):
        self.synth, self.label, self.library = synth, label, library_hashes
        is_st = synth == "syntakt"
        self.bank_lbl.setVisible(is_st); self.bank.setVisible(is_st)
        if is_st:
            first = self.db.one("SELECT MIN(bank) FROM slot_state WHERE synth='syntakt' AND bank=8")
            self.bank.blockSignals(True); self.bank.setCurrentIndex((first or 8) - 1); self.bank.blockSignals(False)
        self.fill()

    def fill(self):
        synth = self.synth
        if not synth:
            return
        rows, cols, cells = layout_for(synth, self.bank.currentIndex() + 1)
        state = {(r["bank"], r["slot"]): r for r in self.db.q(
            "SELECT bank, slot, name, hash, read_at FROM slot_state WHERE synth=?", (synth,))}
        try:
            fac = factory.parse_banks(synth, self.db.factory_banks(synth))
        except ValueError:
            fac = set()
        self.grid.clear()
        self.grid.setRowCount(len(rows)); self.grid.setColumnCount(len(cols))
        self.grid.setVerticalHeaderLabels(rows); self.grid.setHorizontalHeaderLabels(cols)
        self.cells = {}
        n_on = 0
        for r, c, b, s in cells:
            st = state.get((b, s))
            it = QTableWidgetItem(st["name"] if st else "")
            where = place(synth, b, s)
            if st:
                n_on += 1
                bg = GREEN if st["hash"] in self.library else AMBER
                tip = ("in your library — click to select it" if bg == GREEN
                       else "in no source yet — Get from synth keeps it")
                paint(it, bg, "#4b5563" if b in fac else None)
                if b in fac:
                    tip += " · factory bank"
                it.setToolTip(f"{where}  {st['name']}\n{tip}")
            else:
                it.setToolTip(f"{where}  empty or not read yet")
                if b in fac:
                    paint(it, "#2a2f38" if is_dark() else FACTORY_BG)
            self.grid.setItem(r, c, it)
            self.cells[(r, c)] = st
            self.where[(r, c)] = (b, s)
        when = self.db.one("SELECT MAX(read_at) FROM slot_state WHERE synth=?", (synth,))
        import time
        stamp = time.strftime("%d %b %Y %H:%M", time.localtime(when)) if when else "never"
        bank_note = f" · bank {SYNTAKT_BANKS[self.bank.currentIndex()]}" if synth == "syntakt" else ""
        self.title.setText(f"<b>{self.label} › Targets › Presets</b>{bank_note} — {n_on} slots hold a sound "
                           f"<span style='color:#8b919a'>(last read {stamp})</span>")

    def _clicked(self, r, c):
        st = self.cells.get((r, c))
        if st and st["hash"] in self.library:
            self.picked.emit(st["hash"])

    # ---------------------------------------------------------------- right-click: initialize
    def writable(self, bank, slot) -> bool:
        if self.synth == "take5":
            return 1 <= bank <= 16
        if self.synth == "syntakt":
            return bank == 8 and slot < 128
        return False

    def _menu(self, pos):
        ix = self.grid.indexAt(pos)
        if ix.isValid() and not self.grid.item(ix.row(), ix.column()).isSelected():
            self.grid.clearSelection()
            self.grid.item(ix.row(), ix.column()).setSelected(True)
        picked = sorted({self.where[(i.row(), i.column())] for i in self.grid.selectedIndexes()})
        ok = [p for p in picked if self.writable(*p)]
        m = QMenu(self)
        what = {"take5": "Basic Program", "syntakt": "empty"}.get(self.synth)
        if not what:
            a = m.addAction("Initialize… (Sub 37 slot writes come with milestone M5)")
            a.setEnabled(False)
        else:
            one = place(self.synth, *ok[0]) if len(ok) == 1 else f"{len(ok)} slots"
            a = m.addAction(f"Initialize {one} to {what}…" if ok else "Initialize… (factory or audition slots can't be)")
            a.setEnabled(bool(ok))
            a.triggered.connect(lambda: self.initialize.emit(self.synth, ok))
            if len(ok) < len(picked) and ok:
                skip = m.addAction(f"({len(picked) - len(ok)} factory or audition slot(s) left out)")
                skip.setEnabled(False)
        m.exec(self.grid.viewport().mapToGlobal(pos))
