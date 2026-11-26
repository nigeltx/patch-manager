"""Reading the synths (M3, spec 4.7, 4.8, 5.3, 5.5): Initialize factory presets, Back up a synth, and refreshing what
is on each synth. The reading runs on a worker thread; the dialog shows progress and, for Initialize, the review."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..library import factory, layout
from ..library.db import DB
from ..library.formats import SYNTAKT_BANKS, SYNTH_LABEL
from ..library.scanner import Scanner
from .models import AMBER, GREEN

READ_SYNTHS = ("take5", "sub37", "syntakt", "deluge")
MODES = {"get": "Get from synth", "init": "Initialize factory presets", "backup": "Back up",
         "refresh": "Refresh what is on it"}

HOW = {
    "take5": "The app reads all 256 user and 256 factory programs, one by one. About 80 seconds; nothing is written "
             "to the Take 5.",
    "sub37": "The Sub 37 can't be asked for its presets, so you start the dump: on the Sub 37's SYSEX page choose "
             "<b>ALL PRESETS</b> and press <b>SEND</b>. It takes close to 3 minutes; the bar fills as it arrives.",
    "syntakt": "The app lists sound banks A–H and reads every sound in them over USB. Close Elektron Transfer first. "
               "Nothing is written to the Syntakt.",
    "deluge": "The app reads every synth preset in the Deluge's SYNTHS folder over USB (about 30 s for 170 presets). "
              "The Deluge's own USB cable must be on the PC. Nothing is written to the card.",
}


class ReadWorker(QObject):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, mode, synth, session, db_path, library_root):
        super().__init__()
        self.mode, self.synth, self.session = mode, synth, session
        self.db_path, self.root = db_path, library_root
        self.stop = False

    def cancelled(self):
        return self.stop

    def _p(self, done, total, text):
        self.progress.emit(done, total, text)

    def run(self):
        try:
            self.done.emit(self._run())
        except Exception as e:                       # noqa: BLE001
            log = getattr(self.session, "log", None)
            if log:
                log(f"READ {self.mode} {self.synth} failed: {e!r}")
            self.failed.emit(str(e) or e.__class__.__name__)

    def _known(self, db):
        rows = db.slot_rows(self.synth)
        if self.synth == "deluge":
            return {r["place"]: (r["info"], r["hash"], r["name"]) for r in rows if r["info"]}
        return {(r["bank"], r["slot"]): (r["info"], r["hash"], r["name"]) for r in rows if r["info"]}

    def _run(self):
        db = DB(self.db_path)
        full = self.mode != "refresh"
        whole, firmware = None, ""
        if self.synth == "deluge":
            d = self.session.drivers.get("deluge")
            if d is None or not self.session.ready("deluge"):
                raise RuntimeError("The Deluge is not connected and checked — click Deluge on the toolbar to see why")
            firmware = d.firmware
            reads = d.read_slots(self._p, self.cancelled, None if full else self._known(db))
        else:
            drv = self.session.drivers.get(self.synth)
            if drv is None or not self.session.ready(self.synth):
                raise RuntimeError(f"The {SYNTH_LABEL[self.synth]} is not connected and checked — see its button on "
                                   "the toolbar")
            firmware = getattr(drv.pre, "os_version", "") if drv.pre else ""
            if self.synth == "take5":
                reads = drv.read_slots(progress=self._p, cancelled=self.cancelled)
            elif self.synth == "sub37":
                from ..drivers.sub37 import slots_from_all
                self._p(0, 203800, "Waiting for ALL PRESETS — press SEND on the Sub 37")
                whole = drv.wait_all_presets(progress=lambda n: self._p(n, 203800, f"Receiving ALL PRESETS — {n:,} bytes"),
                                             cancelled=self.cancelled)
                if whole is None:
                    raise RuntimeError("No ALL PRESETS dump arrived from the Sub 37")
                reads = slots_from_all(whole)
            else:
                reads = drv.read_banks(SYNTAKT_BANKS, self._p, self.cancelled, None if full else self._known(db))
        if self.stop:
            raise RuntimeError("Stopped — nothing was saved")
        folder = None
        if full and reads and self.mode in ("init", "backup"):
            if not self.root:
                raise RuntimeError("No library folder is set, so there is nowhere to keep the backup")
            label = "Factory read" if self.mode == "init" else "Backup"
            folder = factory.backup_folder(self.root, self.synth, label)
            self._p(0, 0, "Saving the backup files…")
            factory.write_backup(folder, self.synth, reads, whole)
            self._p(0, 0, "Adding the backup to the library…")
            Scanner(self.db_path, [(self.root, "library")]).run()
        if self.mode != "init":
            factory.remember(db, self.synth, reads, replace_all=not self.stop, how="backup" if full else "read")
        return {"reads": reads, "folder": folder, "firmware": firmware}


class ReadDialog(QDialog):
    def __init__(self, parent, mode, synth, session, db, library_root):
        super().__init__(parent)
        self.mode, self.synth, self.session, self.db, self.root = mode, synth, session, db, library_root
        self.thread = self.worker = None
        self.result_ = None
        self.rows = []
        label = SYNTH_LABEL[synth]
        self.setWindowTitle(f"{MODES[mode]} — {label}")
        self.resize(820, 640 if mode in ("init", "get") else 300)
        v = QVBoxLayout(self)
        intro = {
            "get": f"Reads every slot of the {label} and keeps the sounds that are in no source — ones you made or "
                   "changed on the synth — in <b>Sources › User</b>, one file each, then adds them to Patches. The green "
                   "rows are updated too. Nothing is written to the synth.",
            "init": f"Reads the {label}, keeps what it read as a dated backup, then shows every slot so you can say which "
                    "presets are factory. Factory presets get an <b>F</b>, and their slots will be locked when Send to "
                    "slot arrives. Every sound read shows <b>green</b> in the list. Nothing is written to the synth.",
            "backup": f"Reads the {label} and keeps everything on it as a dated backup source in the library "
                      "(_Patch Manager › Backups). The green rows are updated too. Nothing is written to the synth.",
            "refresh": f"Reads the {label} again so the green rows show what is on it now. No backup is kept.",
        }[mode]
        last = db.last_init(synth)
        if mode == "init" and last:
            intro += (f"<br><br>Last run {time.strftime('%d %b %Y %H:%M', time.localtime(last['run_at']))}: "
                      f"{last['slots_read']} slots read, {last['marked_factory']} marked factory. Running it again "
                      "replaces those marks for the sounds it reads.")
        t = QLabel(intro); t.setWordWrap(True); t.setTextFormat(Qt.RichText); v.addWidget(t)
        how = QLabel(HOW[synth]); how.setWordWrap(True); how.setTextFormat(Qt.RichText)
        how.setStyleSheet("background:#f1f4fb; padding:8px; border-radius:4px;"); v.addWidget(how)
        self.banks = None
        if mode == "init" and synth != "deluge":
            h = QHBoxLayout()
            h.addWidget(QLabel("Factory banks:"))
            self.banks = QLineEdit(db.factory_banks(synth)); self.banks.setMaximumWidth(180); h.addWidget(self.banks)
            hint = {"take5": "U1–U16 are user banks, F1–F16 factory banks — e.g. F1-F16, or U1-U8, F1-F16",
                    "sub37": "e.g. 1-9 — banks after these are yours",
                    "syntakt": "sound banks A-H, e.g. A-G — the factory sounds fill A to G"}[synth]
            hl = QLabel(hint); hl.setStyleSheet("color:#6b7280;"); h.addWidget(hl); h.addStretch(1)
            v.addLayout(h)
            sub = QLabel("Presets in these banks are ticked as factory. So is any sound that matches a maker's file in "
                         "your Factory folder, wherever it sits. Everything else starts unticked, as yours.")
            sub.setWordWrap(True); sub.setStyleSheet("color:#6b7280;"); v.addWidget(sub)
        self.bar = QProgressBar(); self.bar.setRange(0, 1); self.bar.setValue(0); v.addWidget(self.bar)
        self.status = QLabel("Press Start when the synth is ready."); self.status.setWordWrap(True); v.addWidget(self.status)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Keep" if mode == "get" else "Factory", "Slot", "Name", "Why"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 64); self.table.setColumnWidth(1, 80); self.table.setColumnWidth(2, 220)
        self.table.hide(); v.addWidget(self.table, 1)
        self.bank_row = QHBoxLayout()
        self.bank_pick = QComboBox(); self.bank_row.addWidget(QLabel("Bank")); self.bank_row.addWidget(self.bank_pick)
        b1 = QPushButton("Tick the bank"); b1.clicked.connect(lambda: self._tick_bank(True)); self.bank_row.addWidget(b1)
        b2 = QPushButton("Untick the bank"); b2.clicked.connect(lambda: self._tick_bank(False)); self.bank_row.addWidget(b2)
        self.count = QLabel(""); self.bank_row.addStretch(1); self.bank_row.addWidget(self.count)
        v.addLayout(self.bank_row)
        self._show_bank_row(False)

        btns = QHBoxLayout(); btns.addStretch(1)
        self.start_btn = QPushButton("Start"); self.start_btn.setDefault(True); self.start_btn.clicked.connect(self.start)
        self.apply_btn = QPushButton("Apply"); self.apply_btn.clicked.connect(self.apply); self.apply_btn.hide()
        self.close_btn = QPushButton("Cancel"); self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.start_btn); btns.addWidget(self.apply_btn); btns.addWidget(self.close_btn)
        v.addLayout(btns)

    def _show_bank_row(self, on):
        for i in range(self.bank_row.count()):
            w = self.bank_row.itemAt(i).widget()
            if w:
                w.setVisible(on)

    # ---------------------------------------------------------------- run
    def start(self):
        if self.banks is not None:
            try:
                self.bank_set = factory.parse_banks(self.synth, self.banks.text())
            except ValueError as e:
                QMessageBox.warning(self, "Factory banks", str(e)); return
            self.db.set_setting(f"factory_banks_{self.synth}", self.banks.text().strip())
            self.banks.setEnabled(False)
        else:
            self.bank_set = set()
        self.start_btn.setEnabled(False)
        self.bar.setRange(0, 0)
        self.status.setText("Reading…")
        self.thread = QThread(self)
        self.worker = ReadWorker(self.mode, self.synth, self.session, self.db.path, self.root)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.thread.start()

    def _progress(self, done, total, text):
        if total:
            self.bar.setRange(0, total); self.bar.setValue(done)
        else:
            self.bar.setRange(0, 0)
        self.status.setText(text)

    def _end(self):
        if self.thread:
            self.thread.quit(); self.thread.wait(); self.thread = None

    def _failed(self, msg):
        self._end()
        self.bar.setRange(0, 1); self.bar.setValue(0)
        self.status.setText(f"<b style='color:#b91c1c'>Stopped:</b> {msg}")
        self.start_btn.setEnabled(True); self.start_btn.setText("Try again")
        if self.banks is not None:
            self.banks.setEnabled(True)

    def _done(self, res):
        self._end()
        self.result_ = res
        reads = res["reads"]
        self.bar.setRange(0, 1); self.bar.setValue(1)
        where = f" Backup kept in {os.path.basename(res['folder'])}." if res.get("folder") else ""
        if self.mode == "get":
            try:
                fb = factory.parse_banks(self.synth, self.db.factory_banks(self.synth)) if self.synth != "deluge" else set()
            except ValueError:
                fb = set()
            new = factory.new_sounds(self.db, self.synth, reads, fb)
            self.rows = [factory.ReviewRow(r, "yours", "In no source — made or changed on the synth", True) for r in new]
            if not new:
                self.status.setText(f"Read {len(reads)} slots. Every sound on the {SYNTH_LABEL[self.synth]} is already "
                                    "in a source — nothing new to keep. The green rows are up to date.")
                self.start_btn.hide(); self.close_btn.setText("Close")
                return
            self.status.setText(f"Read {len(reads)} slots. {len(new)} sounds are in no source. Untick any you do not "
                                "want to keep, then Keep.")
            self._fill()
            self.start_btn.hide(); self.apply_btn.setText("Keep in Sources › User"); self.apply_btn.show()
            self.apply_btn.setDefault(True)
            return
        if self.mode != "init":
            self.status.setText(f"Read {len(reads)} slots.{where} The green rows now show what is on the "
                                f"{SYNTH_LABEL[self.synth]}.")
            self.start_btn.hide(); self.close_btn.setText("Close")
            return
        self.rows = factory.review(reads, self.bank_set, self.db.maker_hashes(self.synth), self.db.maker_pack)
        self.status.setText(f"Read {len(reads)} slots.{where} Tick the factory presets, then Apply.")
        self._fill()
        self.start_btn.hide(); self.apply_btn.show(); self.apply_btn.setDefault(True)

    # ---------------------------------------------------------------- review
    def _fill(self):
        listed = [r for r in self.rows if r.status != "blank"]
        self.table.setRowCount(len(listed)); self.table.show()
        for i, r in enumerate(listed):
            cb = QTableWidgetItem(); cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            cb.setCheckState(Qt.Checked if r.tick else Qt.Unchecked); cb.setData(Qt.UserRole, self.rows.index(r))
            self.table.setItem(i, 0, cb)
            self.table.setItem(i, 1, QTableWidgetItem(r.read.place))
            self.table.setItem(i, 2, QTableWidgetItem(r.read.name))
            why = QTableWidgetItem(r.note)
            self.table.setItem(i, 3, why)
            self._paint(i)
        self.table.itemChanged.connect(self._changed)
        banks = sorted({r.read.bank for r in listed})
        self.bank_pick.clear()
        for b in banks:
            self.bank_pick.addItem(factory.bank_label(self.synth, b), b)
        self._show_bank_row(bool(banks) and self.synth != "deluge")
        blanks = sum(1 for r in self.rows if r.status == "blank")
        self._count(blanks)

    def _paint(self, i):
        r = self.rows[self.table.item(i, 0).data(Qt.UserRole)]
        for c in range(4):
            self.table.item(i, c).setBackground(QColor((GREEN if self.mode == "get" else AMBER) if r.tick else "#ffffff"))

    def _count(self, blanks=None):
        blanks = sum(1 for r in self.rows if r.status == "blank") if blanks is None else blanks
        n = sum(1 for r in self.rows if r.tick)
        if self.mode == "get":
            self.count.setText(f"{n} of {len(self.rows)} to keep")
            return
        self.count.setText(f"{n} ticked as factory · {blanks} blank slots not listed")
        if self.synth == "deluge":
            self.status.setText(self.status.text().split(" Tick")[0] + f" Tick the factory presets, then Apply. {n} ticked.")

    def _changed(self, item):
        if item.column() != 0:
            return
        r = self.rows[item.data(Qt.UserRole)]
        r.tick = item.checkState() == Qt.Checked
        self.table.blockSignals(True); self._paint(item.row()); self.table.blockSignals(False)
        self._count()

    def _tick_bank(self, on):
        bank = self.bank_pick.currentData()
        self.table.blockSignals(True)
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            r = self.rows[it.data(Qt.UserRole)]
            if r.read.bank == bank:
                r.tick = on
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                self._paint(i)
        self.table.blockSignals(False)
        self._count()

    def apply(self):
        res = self.result_
        self.kept = None
        if self.mode == "get":
            items = [(r.read.hash, r.read.place, r.read.name, r.read.data, False) for r in self.rows if r.tick]
            self.kept = self._keep(items, set())
            self.accept()
            return
        n = factory.apply(self.db, self.synth, self.rows, SYNTH_LABEL[self.synth], res.get("firmware", ""),
                          res.get("folder") or "")
        if self.root and layout.is_new_layout(self.root, self.synth):
            items = [(r.read.hash, r.read.place, r.read.name, r.read.data, r.status == "blank") for r in self.rows]
            self.kept = self._keep(items, {r.read.hash for r in self.rows if r.tick and r.status != "blank"})
        self.status.setText(f"{n} presets marked factory.")
        self.accept()

    def _keep(self, items, factory_hashes):
        """Write the sounds into Sources (spec 4.4) and add them to the library."""
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            kept = factory.keep_in_sources(self.db, self.root, self.synth, items, factory_hashes)
            Scanner(self.db.path, [(self.root, "library")]).run()
        finally:
            QGuiApplication.restoreOverrideCursor()
        return kept

    def reject(self):
        if self.thread is not None:
            self.worker.stop = True
            self.status.setText("Stopping…")
            return
        super().reject()

    def closeEvent(self, e):
        if self.thread is not None:
            self.worker.stop = True
            e.ignore(); return
        super().closeEvent(e)
