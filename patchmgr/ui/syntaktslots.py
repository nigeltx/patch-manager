"""The Syntakt's bank H (spec 7.4, 7.8): Send to slot, Sync with the "For Syntakt" crate, Undo the last write."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QTableWidget, QTableWidgetItem

from ..library import factory, syntaktslots as ss
from ..library.scanner import Scanner
from .userslots import STATE_COLOUR, SlotsDialog, UndoDialog


class _Syntakt:
    SYNTH, LABEL, OPS = "syntakt", "Syntakt", ss


def make_grid():
    """Bank H as 8 rows of 16: H001–H016 … H113–H128. H128 is the audition slot and can't be picked."""
    g = QTableWidget(8, 16)
    g.setHorizontalHeaderLabels([f"+{c + 1}" for c in range(16)])
    g.setVerticalHeaderLabels([f"H{r * 16 + 1:03d}" for r in range(8)])
    g.setEditTriggers(QAbstractItemView.NoEditTriggers)
    g.setSelectionMode(QAbstractItemView.SingleSelection)
    g.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    g.verticalHeader().setDefaultSectionSize(22)
    g.setMinimumHeight(8 * 22 + 30)
    g.setStyleSheet("QTableWidget { font-size: 8pt; } QTableWidget::item:selected { background: #2f80c3; color: white; }")
    return g


def fill_grid(grid, db):
    sm = ss.slot_map(db)
    for s in range(1, 129):
        r, c = divmod(s - 1, 16)
        if s == ss.SCRATCH:
            it = QTableWidgetItem("audition")
            it.setToolTip("H128 is the audition slot — not used for keeping sounds")
            it.setBackground(QColor("#f3f4f6")); it.setForeground(QColor("#9ca3af"))
            it.setFlags(Qt.NoItemFlags)
        else:
            v = sm.get(s)
            it = QTableWidgetItem(v["name"] if v else "")
            it.setToolTip(f"H:{s:03d}: " + ((v["name"] or "empty") + f" — {v['state']}" if v else "not read yet"))
            it.setBackground(QColor(STATE_COLOUR.get(v["state"], "#ffffff") if v else "#ffffff"))
        grid.setItem(r, c, it)


class SyntaktSendDialog(_Syntakt, SlotsDialog):
    """Pick the first H slot; the chosen sounds go to it and the slots after it."""

    def __init__(self, parent, session, db, root, patches):
        SlotsDialog.__init__(self, parent, session, db, root, "Send to a Syntakt slot", "Now holds")
        self.patches = patches
        self.resize(900, 640)
        n = len(patches)
        self.intro.setText(
            f"Sends <b>{n} sound{'s' if n != 1 else ''}</b> to bank H of the Syntakt's sound pool"
            + (" — the first to the slot you pick, the rest to the slots after it" if n > 1 else "")
            + ". Green slots are empty, grey ones hold a sound in your library, amber ones a sound in no source — it is "
              "kept in Sources › User before it is written over. Banks A–G (factory) and H128 (audition) are never "
              "written. Each slot is checked first, kept in Backups, and read back after.")
        self.grid = make_grid()
        self.grid.cellClicked.connect(lambda r, c: self.plan(r * 16 + c + 1))
        self.top.addWidget(self.grid)
        row = QHBoxLayout()
        self.read_btn = QPushButton("Read bank H")
        self.read_btn.setToolTip("Refresh the grid from the Syntakt; otherwise it shows the last read")
        self.read_btn.clicked.connect(self.read_h)
        row.addWidget(self.read_btn); row.addStretch(1)
        self.top.addLayout(row)
        self.go.show(); self.go.setEnabled(False)
        fill_grid(self.grid, db)
        if not ss.was_read(db):
            self.status.setText("Bank H has not been read yet — press <b>Read bank H</b> so empty slots show.")
        else:
            first = next((s for s in ss.SLOTS if ss.slot_map(db).get(s, {}).get("state") == "free"), None)
            if first:
                self.grid.setCurrentCell(*divmod(first - 1, 16))
                self.plan(first)
            self.status.setText("Click a slot to choose where the first sound goes. Nothing has been written yet.")

    def plan(self, start):
        if start >= ss.SCRATCH:
            return
        steps, left = ss.send_plan(self.db, self.patches, start)
        self.show_steps(steps)
        self.summary.setText(f"From <b>H:{start:03d}</b>: {len(steps)} slot{'s' if len(steps) != 1 else ''}"
                             + (f". {len(left)} will not fit before H127." if left else "."))

    def read_h(self):
        if not self.ready():
            return
        drv, known = self.drv, _known(self.db)
        self.start(lambda prog, canc: drv.read_banks("H", prog, canc, known), self._after_read)

    def _after_read(self, reads):
        ss.remember_read(self.db, reads)
        fill_grid(self.grid, self.db)
        r, c = self.grid.currentRow(), self.grid.currentColumn()
        if r >= 0:
            self.plan(r * 16 + c + 1)
        self.status.setText(f"Bank H read: {len(reads)} sounds. Nothing has been written yet.")

    def go_text(self, n):
        return f"Send {n} sound{'s' if n != 1 else ''}"

    def run_label(self):
        return "Send to slot"

    def write(self):
        if self.ready():
            super().write()

    def written(self, res):
        super().written(res)
        if res.kept:
            self.status.setText(self.status.text() + f"<br>{len(res.kept)} sound(s) that were in no source are now in "
                                "Sources › User: " + ", ".join(res.kept[:6]))
        fill_grid(self.grid, self.db)


class SyntaktSyncDialog(_Syntakt, SlotsDialog):
    """Read bank H (keeping anything new), then send the "For Syntakt" sounds not on it into empty H slots."""

    def __init__(self, parent, session, db, root):
        SlotsDialog.__init__(self, parent, session, db, root, "Sync with the Syntakt", "Into")
        self.intro.setText(
            f"Brings the Syntakt and your <b>{ss.FOR_CRATE}</b> crate together. <b>1 · Get:</b> bank H is read; any "
            "sound in no source is kept in Sources › User. <b>2 · Send:</b> the sounds in "
            f"<i>{ss.FOR_CRATE}</i> that are not on the Syntakt yet go into empty H slots. Nothing is written over a "
            "sound, nothing is removed, and banks A–G and H128 are never written.")
        row = QHBoxLayout()
        self.read_btn = QPushButton("1 · Read bank H"); self.read_btn.clicked.connect(self.read_h)
        row.addWidget(self.read_btn); row.addStretch(1)
        self.top.addLayout(row)
        self.go.show(); self.go.setEnabled(False)
        n = len(ss.crate_patches(db, ss.for_crate(db)))
        self.summary.setText(f"<i>{ss.FOR_CRATE}</i> holds {n} Syntakt sound{'s' if n != 1 else ''}. "
                             + ("Add sounds to it from the list (right-click)." if not n else
                                "Read the Syntakt to see which still need sending."))

    def read_h(self):
        if not self.ready():
            return
        drv, known = self.drv, _known(self.db)
        self.status.setText("Reading bank H…")
        self.start(lambda prog, canc: drv.read_banks("H", prog, canc, known), self.after_read)

    def after_read(self, reads):
        items = [(r.hash, r.place, r.name, r.data, False) for r in reads if r.data and r.slot != ss.SCRATCH]
        kept = factory.keep_in_sources(self.db, self.root, "syntakt", items, set()) if items else {"n_user": 0}
        if kept["n_user"]:
            Scanner(self.db.path, [(self.root, "library")]).run()
        ss.remember_read(self.db, reads)
        steps, left, there = ss.sync_plan(self.db)
        self.show_steps(steps)
        bits = ["<b>Get:</b> " + (f"{kept['n_user']} new sound{'s' if kept['n_user'] != 1 else ''} kept in Sources › "
                                   "User." if kept["n_user"] else "nothing new in bank H.")]
        bits.append(f"<b>Send:</b> {len(steps)} to send, {there} already on the Syntakt"
                    + (f", <span style='color:#b45309'>{len(left)} with no empty H slot left</span>" if left else "") + ".")
        self.summary.setText(" ".join(bits))
        self.status.setText("Nothing has been written yet." if steps else "The Syntakt is in step with the crate.")

    def go_text(self, n):
        return f"Send {n} sound{'s' if n != 1 else ''}"

    def run_label(self):
        return "Sync"

    def write(self):
        if self.ready():
            super().write()


class SyntaktEmptyDialog(_Syntakt, SlotsDialog):
    """Initialize bank H slots to empty (from a right-click in the bank view)."""

    def __init__(self, parent, session, db, root, slots):
        SlotsDialog.__init__(self, parent, session, db, root, "Initialize Syntakt slots", "Now holds")
        self.intro.setText(
            "Empties the slots below in bank H. Each sound is read first and kept in <i>Syntakt › Backups</i>, and in "
            "Sources › User if it is in no source, so nothing is lost; Undo on the Syntakt menu puts them back. Banks "
            "A–G and H128 are never touched.")
        steps, already = ss.empty_plan(db, slots)
        self.show_steps(steps)
        self.summary.setText(f"<b>{len(steps)} slot{'s' if len(steps) != 1 else ''} to empty</b>"
                             + (f", {already} already empty." if already else "."))
        self.go.show()
        self.go.setEnabled(bool(steps))
        self.status.setText("Nothing has been written yet.")

    def go_text(self, n):
        return f"Empty {n} slot{'s' if n != 1 else ''}"

    def run_label(self):
        return "Initialize slots"

    def write(self):
        if self.ready():
            super().write()


class SyntaktUndoDialog(_Syntakt, UndoDialog):
    pass


def _known(db):
    """The last read of bank H, so slots whose name and size are unchanged are not read again."""
    return {(r["bank"], r["slot"]): (r["info"], r["hash"], r["name"])
            for r in db.q("SELECT bank, slot, info, hash, name FROM slot_state WHERE synth='syntakt' AND bank=?",
                          (ss.BANK,)) if r["info"]}
