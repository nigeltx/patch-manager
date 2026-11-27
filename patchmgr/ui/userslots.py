"""Take 5 user slots (spec 7.9, 7.10): Clear user slots…, Fill user slots…, Undo the last clear or fill."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..library import factory, userslots
from ..library.scanner import Scanner
from .models import AMBER, GREEN

TITLE = "Take 5 user slots"


class FnWorker(QObject):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.done.emit(self.fn(lambda d, t, s: self.progress.emit(d, t, s), lambda: self._cancel))
        except Exception as e:                      # shown to Nigel in plain words, with the detail in the log
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")


class SlotsDialog(QDialog):
    """Table of steps + progress + one action button. Subclasses fill the steps."""
    def __init__(self, parent, session, db, root, title, last_col="Now"):
        super().__init__(parent)
        self.session, self.db, self.root = session, db, root
        self.drv = session.drivers.get("take5") if session else None
        self.steps = []
        self.thread = self.worker = None
        self.setWindowTitle(title)
        self.resize(820, 620)
        self.v = QVBoxLayout(self)
        self.intro = QLabel(); self.intro.setWordWrap(True); self.intro.setTextFormat(Qt.RichText)
        self.v.addWidget(self.intro)
        self.top = QVBoxLayout(); self.v.addLayout(self.top)
        self.summary = QLabel(); self.summary.setWordWrap(True); self.v.addWidget(self.summary)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Send", "Slot", "Writes", last_col])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        h = self.table.horizontalHeader()
        for c, m in enumerate([QHeaderView.ResizeToContents, QHeaderView.ResizeToContents, QHeaderView.Stretch,
                               QHeaderView.Stretch]):
            h.setSectionResizeMode(c, m)
        self.table.itemChanged.connect(self._ticked)
        self.v.addWidget(self.table, 1)
        self.bar = QProgressBar(); self.bar.hide(); self.v.addWidget(self.bar)
        self.status = QLabel(); self.status.setWordWrap(True); self.v.addWidget(self.status)
        b = QHBoxLayout(); b.addStretch(1)
        self.go = QPushButton(); self.go.hide(); self.go.clicked.connect(self.write)
        self.cancel_btn = QPushButton("Stop after this slot"); self.cancel_btn.hide(); self.cancel_btn.clicked.connect(self._cancel)
        self.close_btn = QPushButton("Close"); self.close_btn.clicked.connect(self.reject)
        for w in (self.go, self.cancel_btn, self.close_btn):
            b.addWidget(w)
        self.v.addLayout(b)

    # ---------------------------------------------------------------- table
    def show_steps(self, steps):
        self.steps = steps
        self.table.blockSignals(True)
        self.table.setRowCount(len(steps))
        for i, s in enumerate(steps):
            t = QTableWidgetItem(); t.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            t.setCheckState(Qt.Checked if s.tick else Qt.Unchecked)
            self.table.setItem(i, 0, t)
            for c, v in enumerate([s.place, s.name, s.why], 1):
                self.table.setItem(i, c, QTableWidgetItem(v))
            self._paint(i)
        self.table.blockSignals(False)
        self._label()

    def _paint(self, i):
        col = QColor(GREEN if self.steps[i].tick else "#ffffff")
        for c in range(4):
            self.table.item(i, c).setBackground(col)

    def _ticked(self, item):
        if item.column() != 0:
            return
        self.steps[item.row()].tick = item.checkState() == Qt.Checked
        self.table.blockSignals(True); self._paint(item.row()); self.table.blockSignals(False)
        self._label()

    def _label(self):
        n = sum(1 for s in self.steps if s.tick)
        self.go.setText(self.go_text(n)); self.go.setEnabled(n > 0)

    def go_text(self, n):
        return f"Write {n} slots"

    # ---------------------------------------------------------------- threads
    def start(self, fn, on_done):
        self.bar.show(); self.bar.setValue(0)
        self.go.setEnabled(False); self.close_btn.setEnabled(False); self.cancel_btn.show()
        self.thread = QThread(self)
        self.worker = FnWorker(fn)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(lambda r: self._finish(on_done, r))
        self.worker.failed.connect(self._failed)
        self.thread.start()

    def _progress(self, d, t, s):
        self.bar.setMaximum(max(t, 1)); self.bar.setValue(d); self.status.setText(s)

    def _stop_thread(self):
        self.cancel_btn.hide(); self.close_btn.setEnabled(True); self.bar.hide()
        if self.thread:
            self.thread.quit(); self.thread.wait(2000)
        self.thread = self.worker = None

    def _finish(self, on_done, r):
        self._stop_thread()
        on_done(r)

    def _failed(self, msg):
        self._stop_thread()
        self.status.setText("<b>Stopped:</b> " + msg.split("\n\n")[0])
        if self.session and hasattr(self.session, "log"):
            self.session.log(msg)

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False); self.status.setText("Stopping after the slot in hand…")

    def reject(self):
        if self.thread is not None:
            return                          # a write is running: wait for it, or press Stop
        super().reject()

    # ---------------------------------------------------------------- writing
    def write(self):
        todo = [s for s in self.steps if s.tick]
        label = self.run_label()
        self.start(lambda prog, canc: userslots.run(self.drv, self.db, self.root, todo, label, prog, canc), self.written)

    def run_label(self):
        return "Write"

    def written(self, res):
        lines = [f"<b>{res.done} slots written</b> and checked."]
        if res.skipped:
            lines.append(f"{len(res.skipped)} skipped because they had changed since the read: "
                         + ", ".join(p for p, _ in res.skipped[:12]) + ("…" if len(res.skipped) > 12 else ""))
        if res.stopped:
            lines.append("Stopped when you asked; the slots not reached are as they were.")
        if res.failed:
            lines.append("<span style='color:#b91c1c'>" + res.failed + "</span>")
        lines.append(f"The old programs are in <i>Take 5 › Backups</i>. Undo is on the Take 5 menu.")
        self.status.setText("<br>".join(lines))
        self.go.hide(); self.close_btn.setText("Close"); self.close_btn.setDefault(True)

    def ready(self):
        if not self.drv or not self.session.ready("take5"):
            self.status.setText("<b>The Take 5 is not connected.</b> Switch it on and check its USB cable.")
            return False
        return True


# ---------------------------------------------------------------- Clear user slots (spec 7.9)
class ClearDialog(SlotsDialog):
    def __init__(self, parent, session, db, root):
        super().__init__(parent, session, db, root, "Clear the Take 5's user slots")
        self.intro.setText(
            "Writes a blank init program into every user slot, U1-01 to U16-16, so they are free for your own sounds. "
            "Factory banks F1–F16 are not touched. First every user slot is read and kept in Backups, and any sound "
            "that is in no source goes to <b>Sources › User</b>, so nothing of yours is lost.")
        row = QHBoxLayout()
        self.init_lbl = QLabel(); self.init_lbl.setWordWrap(True)
        row.addWidget(QLabel("<b>1 · Init program</b>")); row.addWidget(self.init_lbl, 1)
        self.top.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("On the Take 5 choose Init Program and save it into user slot  U"))
        self.bank = QSpinBox(); self.bank.setRange(1, 16); self.bank.setValue(16)
        self.prog = QSpinBox(); self.prog.setRange(1, 16); self.prog.setValue(16)
        row2.addWidget(self.bank); row2.addWidget(QLabel("-")); row2.addWidget(self.prog)
        self.read_init_btn = QPushButton("Read it"); self.read_init_btn.clicked.connect(self.read_init)
        row2.addWidget(self.read_init_btn); row2.addStretch(1)
        self.top.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("<b>2 · Read the user slots</b>"))
        self.read_btn = QPushButton("Read U1–U16 (about 40 s)"); self.read_btn.clicked.connect(self.read_user)
        row3.addWidget(self.read_btn); row3.addStretch(1)
        self.top.addLayout(row3)
        self.go.setText("Clear"); self.go.show(); self.go.setEnabled(False)
        self.init = userslots.load_init(db, root)
        self._show_init()

    def go_text(self, n):
        return f"Clear {n} slots"

    def run_label(self):
        return "Clear user slots"

    def _show_init(self):
        if self.init:
            self.init_lbl.setText(f"<span style='color:#065f46'>Using <b>{self.init[2]}</b> (kept from before). "
                                  "Read a slot again to use a different one.</span>")
        else:
            self.init_lbl.setText("Not made yet.")
        self.read_btn.setEnabled(self.init is not None)

    def read_init(self):
        if not self.ready():
            return
        b, p = self.bank.value(), self.prog.value()
        m = self.drv.read_program(b, p)
        if m is None:
            self.status.setText(f"The Take 5 did not send U{b}-{p:02d}."); return
        h, name = userslots.hash_of(m)
        if factory.library_hashes(self.db, "take5") and h in factory.library_hashes(self.db, "take5"):
            if QMessageBox.question(self, TITLE, f"U{b}-{p:02d} holds <b>{name}</b>, which is a sound in your library, "
                                    "not an init program. Use it anyway?") != QMessageBox.Yes:
                return
        elif QMessageBox.question(self, TITLE, f"U{b}-{p:02d} holds <b>{name}</b>. Use this as the init program for "
                                  "every cleared slot?") != QMessageBox.Yes:
            return
        userslots.save_init(self.db, self.root, m)
        self.init = userslots.load_init(self.db, self.root)
        self._show_init()

    def read_user(self):
        if not self.ready():
            return
        drv = self.drv
        self.status.setText("Reading U1–U16…")
        self.start(lambda prog, canc: drv.read_slots(userslots.USER_BANKS, prog, canc), self.after_read)

    def after_read(self, reads):
        if len(reads) < 256:
            self.status.setText(f"Read stopped after {len(reads)} slots — nothing was written."); return
        folder = factory.backup_folder(self.root, "take5", "Before clear")
        factory.write_backup(folder, "take5", reads)
        items = [(r.hash, r.place, r.name, r.data, r.blank) for r in reads]
        kept = factory.keep_in_sources(self.db, self.root, "take5", items, set())
        Scanner(self.db.path, [(self.root, "library")]).run()
        self.db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "place": r.place, "name": r.name, "hash": r.hash}
                                    for r in reads])
        steps, c = userslots.clear_plan(self.db, reads, self.init[0])
        self.show_steps(steps)
        self.summary.setText(
            f"<b>{len(steps)} slots to clear:</b> {c['factory copy']} copies of factory sounds, {c['yours']} of yours "
            f"(safe in Sources), {c['other']} blank or repeated. {c['init already']} already hold the init program."
            + (f" <b>{kept['n_user']} {'sound that was in no source is' if kept['n_user'] == 1 else 'sounds that were in no source are'}"
               "</b> now in Sources › User." if kept["n_user"] else "")
            + " The whole read is in Backups.")
        self.status.setText("Nothing has been written yet.")


# ---------------------------------------------------------------- Fill user slots (spec 7.10)
class FillDialog(SlotsDialog):
    def __init__(self, parent, session, db, root):
        super().__init__(parent, session, db, root, "Fill the Take 5's user slots", "Type")
        self.intro.setText(
            "Sends your own Take 5 patches — the ones whose only source is <b>Sources › User</b> — into the user slots, "
            "packed from the first slot holding the init program, in Patches order (Bass, Lead, Pad … then by name). "
            "Only init slots are used, so nothing of yours is written over. Each slot is read first and checked after.")
        self.go.show()
        init = userslots.load_init(db, root)
        if not init:
            self.summary.setText("<b>No init program yet.</b> Run Clear user slots first."); self.go.setEnabled(False)
            return
        steps, left = userslots.fill_plan(db, init[1])
        self.show_steps(steps)
        free = db.one("SELECT COUNT(*) FROM slot_state WHERE synth='take5' AND bank BETWEEN 1 AND 16 AND hash=?", (init[1],))
        mine = len(userslots.your_patches(db))
        self.summary.setText(
            f"<b>{len(steps)} of your {mine} patches to send</b> into {free} init slots."
            + (f" {len(left)} will not fit: " + ", ".join(r["name"] for r in left[:8]) + ("…" if len(left) > 8 else "")
               if left else "")
            + ("" if steps else " Nothing to send — your patches are already on the Take 5, or no init slots are free."))
        if not steps:
            self.go.setEnabled(False)

    def go_text(self, n):
        return f"Send {n} patches"

    def run_label(self):
        return "Fill user slots"

    def write(self):
        if self.ready():
            super().write()


# ---------------------------------------------------------------- Undo
class UndoDialog(SlotsDialog):
    def __init__(self, parent, session, db, root):
        super().__init__(parent, session, db, root, "Undo on the Take 5", "Puts back")
        run = userslots.last_run(db)
        self.go.show()
        if not run:
            self.intro.setText("Nothing to undo."); self.go.setEnabled(False); return
        self.label = run
        steps = userslots.undo_steps(db, run)
        self.intro.setText(f"Puts back the programs that <b>{run[18:]}</b> ({run[:10]}) wrote over, from their backups. "
                           "A slot is only put back if it still holds what that run wrote there.")
        self.show_steps(steps)

    def go_text(self, n):
        return f"Put back {n} slots"

    def run_label(self):
        return f"Undo {self.label[18:]}"

    def write(self):
        if self.ready():
            super().write()


# ---------------------------------------------------------------- Send to slot (spec 5.2, 7.1)
STATE_COLOUR = {"free": "#d8f0e4", "yours": "#e5e7eb", "unknown": "#fde8c8"}


class SendDialog(SlotsDialog):
    """Pick the first user slot on a 16 × 16 grid; the chosen patches go to it and the slots after it."""

    def __init__(self, parent, session, db, root, patches):
        super().__init__(parent, session, db, root, "Send to a Take 5 slot", "Now holds")
        self.patches = patches
        self.resize(900, 780)
        n = len(patches)
        self.intro.setText(
            f"Sends <b>{n} patch{'es' if n != 1 else ''}</b> to the Take 5's user slots"
            + (" — the first to the slot you pick, the rest to the slots after it" if n > 1 else "")
            + ". Pick a slot below: green slots are free (the init program), grey ones hold a sound in your library, "
              "amber ones hold a sound in no source — it is kept in Sources › User before it is written over. "
              "Factory banks F1–F16 are never written. Each slot is read first, kept in Backups, and checked after.")
        self.grid = QTableWidget(16, 16)
        self.grid.setHorizontalHeaderLabels([f"{p:02d}" for p in range(1, 17)])
        self.grid.setVerticalHeaderLabels([f"U{b}" for b in range(1, 17)])
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.grid.verticalHeader().setDefaultSectionSize(20)
        self.grid.setMinimumHeight(16 * 20 + 30)
        self.grid.setStyleSheet("QTableWidget { font-size: 8pt; }")
        self.grid.cellClicked.connect(self.pick)
        self.top.addWidget(self.grid)
        row = QHBoxLayout()
        self.read_all_btn = QPushButton("Read U1–U16 (about 40 s)")
        self.read_all_btn.setToolTip("Refresh the grid from the Take 5; otherwise it shows the last read")
        self.read_all_btn.clicked.connect(self.read_all)
        row.addWidget(self.read_all_btn); row.addStretch(1)
        self.top.addLayout(row)
        self.go.show(); self.go.setEnabled(False)
        self.paint_grid()
        first = next((k for k in userslots.slots() if userslots.slot_map(db).get(k, {}).get("state") == "free"), None)
        if first:
            self.grid.setCurrentCell(first[0] - 1, first[1] - 1)
            self.plan(first)
        self.status.setText("Click a slot to choose where the first patch goes. Nothing has been written yet.")

    def paint_grid(self):
        sm = userslots.slot_map(self.db)
        for b in range(1, 17):
            for p in range(1, 17):
                v = sm.get((b, p))
                it = QTableWidgetItem(v["name"] if v else "")
                it.setToolTip(f"U{b}-{p:02d}: " + (f"{v['name']} — {v['state']}" if v else "not read yet"))
                it.setBackground(QColor(STATE_COLOUR.get(v["state"], "#ffffff") if v else "#ffffff"))
                self.grid.setItem(b - 1, p - 1, it)

    def pick(self, r, c):
        """Read the chosen slot on the spot, so the name shown is what is really there (spec 5.2)."""
        b, p = r + 1, c + 1
        if self.ready():
            m = self.drv.read_program(b, p)
            if m is None:
                self.status.setText(f"The Take 5 did not send U{b}-{p:02d}."); return
            h, name = userslots.hash_of(m)
            self.db.set_slots("take5", [{"bank": b, "slot": p, "place": f"U{b}-{p:02d}", "name": name, "hash": h}])
            self.db.con.commit()
            self.paint_grid()
        self.plan((b, p))

    def plan(self, start):
        steps, left = userslots.send_plan(self.db, self.patches, start)
        self.show_steps(steps)
        self.summary.setText(f"From <b>U{start[0]}-{start[1]:02d}</b>: {len(steps)} slot{'s' if len(steps) != 1 else ''}"
                             + (f". {len(left)} will not fit before U16-16." if left else "."))

    def read_all(self):
        if not self.ready():
            return
        drv = self.drv
        self.start(lambda prog, canc: drv.read_slots(userslots.USER_BANKS, prog, canc), self._after_read)

    def _after_read(self, reads):
        self.db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "place": r.place, "name": r.name, "hash": r.hash}
                                    for r in reads])
        self.db.con.commit()
        self.paint_grid()
        r = self.grid.currentRow(); c = self.grid.currentColumn()
        if r >= 0:
            self.plan((r + 1, c + 1))
        self.status.setText(f"Read {len(reads)} slots. Nothing has been written yet.")

    def go_text(self, n):
        return f"Send {n} patch{'es' if n != 1 else ''}"

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
        self.paint_grid()


# ---------------------------------------------------------------- Sync (spec 7.8)
class SyncDialog(SlotsDialog):
    """Get first, then send the "For Take 5" patches that are not on it into free slots."""

    def __init__(self, parent, session, db, root):
        super().__init__(parent, session, db, root, "Sync with the Take 5", "Into")
        self.intro.setText(
            f"Brings the Take 5 and your <b>{userslots.FOR_CRATE}</b> crate together. <b>1 · Get:</b> every user slot is "
            "read; any sound in no source is kept in Sources › User, so everything on the Take 5 is in your library. "
            f"<b>2 · Send:</b> the patches in <i>{userslots.FOR_CRATE}</i> that are not on the Take 5 yet go into free "
            "slots (the init program). Nothing is written over a sound, and nothing is removed from the Take 5.")
        row = QHBoxLayout()
        self.read_btn = QPushButton("1 · Read U1–U16 (about 40 s)"); self.read_btn.clicked.connect(self.read_user)
        row.addWidget(self.read_btn); row.addStretch(1)
        self.top.addLayout(row)
        self.go.show(); self.go.setEnabled(False)
        n = len(userslots.crate_patches(db, userslots.for_crate(db)))
        self.summary.setText(f"<i>{userslots.FOR_CRATE}</i> holds {n} Take 5 patch{'es' if n != 1 else ''}. "
                             "Add patches to it from the list (right-click › Add to crate)." if not n else
                             f"<i>{userslots.FOR_CRATE}</i> holds {n} Take 5 patch{'es' if n != 1 else ''}. Read the "
                             "Take 5 to see which still need sending.")

    def read_user(self):
        if not self.ready():
            return
        drv = self.drv
        self.status.setText("Reading U1–U16…")
        self.start(lambda prog, canc: drv.read_slots(userslots.USER_BANKS, prog, canc), self.after_read)

    def after_read(self, reads):
        if len(reads) < 256:
            self.status.setText(f"Read stopped after {len(reads)} slots — nothing was written."); return
        folder = factory.backup_folder(self.root, "take5", "Before sync")
        factory.write_backup(folder, "take5", reads)
        items = [(r.hash, r.place, r.name, r.data, r.blank) for r in reads]
        kept = factory.keep_in_sources(self.db, self.root, "take5", items, set())
        Scanner(self.db.path, [(self.root, "library")]).run()
        self.db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "place": r.place, "name": r.name, "hash": r.hash}
                                    for r in reads])
        self.db.con.commit()
        steps, left, there = userslots.sync_plan(self.db)
        self.show_steps(steps)
        bits = [f"<b>Get:</b> " + (f"{kept['n_user']} new sound{'s' if kept['n_user'] != 1 else ''} kept in Sources › "
                                    "User." if kept["n_user"] else "nothing new on the Take 5.")]
        bits.append(f"<b>Send:</b> {len(steps)} to send, {there} already on the Take 5"
                    + (f", <span style='color:#b45309'>{len(left)} with no free slot left</span>" if left else "") + ".")
        self.summary.setText(" ".join(bits))
        self.status.setText("Nothing has been written yet." if steps else "The Take 5 is in step with the crate.")

    def go_text(self, n):
        return f"Send {n} patch{'es' if n != 1 else ''}"

    def run_label(self):
        return "Sync"

    def write(self):
        if self.ready():
            super().write()
