"""Main window (spec 5.1), milestone 1: library, search, tags, ratings, crates, previews. No MIDI yet."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QPushButton, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QSplitter,
                               QStackedWidget, QTableView, QTableWidget, QTableWidgetItem, QToolBar, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import audio
from ..library import search
from ..library.db import DB
from ..library.formats import SYNTH_LABEL
from ..library.scanner import Scanner, find_deluge_cards, find_library_root
from .audition import AuditionPanel
from .details import Details
from .session import Session
from .models import TYPE_LABEL, PatchModel, SortProxy

APP = "Patch Manager"
from patchmgr import __version__
VERSION = f"{__version__} (M2 Audition + Syntakt)"
SYNTH_ORDER = ["take5", "sub37", "deluge", "syntakt"]
HELP = """<b>Search</b> — type words to match names, packs and notes, and add filters:<br>
<code>synth:take5</code> &nbsp; <code>synth:sub37</code> &nbsp; <code>synth:deluge</code> &nbsp; <code>synth:syntakt</code><br>
<code>tag:pad tag:dark</code> (all must match) &nbsp; <code>rating:&gt;=4</code> &nbsp; <code>rating:0</code><br>
<code>source:paid</code> / free / factory / unsorted / card &nbsp; <code>pack:audiotent</code> &nbsp; <code>crate:keepers</code><br>
<code>unheard</code> &nbsp; <code>heard</code> &nbsp; <code>preview</code> &nbsp; <code>dupe</code> &nbsp; <code>unrated</code><br>
Put <code>-</code> in front of anything to exclude it: <code>-tag:drum</code>. Use quotes for spaces: <code>pack:"lost clouds"</code>.<br><br>
<b>Keys</b> — ↑ ↓ move (and load, if that box is ticked) · Enter loads and plays the test phrase · P plays it again ·
Esc stops · T starts or stops the transport · 1–5 rate the selected patches, 0 clears · Space plays or stops the preview
recording · Ctrl+F search · Ctrl+R rescan · Delete removes from the crate you are viewing."""


class ScanWorker(QObject):
    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path, roots):
        super().__init__()
        self.db_path, self.roots, self.stop = db_path, roots, False

    def run(self):
        try:
            p = Scanner(self.db_path, self.roots, lambda p: self.progress.emit(p.done, p.total, p.current),
                        lambda: self.stop).run()
            self.done.emit(p)
        except Exception as e:                       # noqa: BLE001
            import traceback
            self.failed.emit("".join(traceback.format_exception(e)))


class MainWindow(QMainWindow):
    def __init__(self, db: DB):
        super().__init__()
        self.db = db
        self.player = audio.Player()
        self.scope = {}
        self.thread = None
        self.setWindowTitle(f"{APP} {VERSION}")
        self.resize(1400, 860)

        # ---------------- toolbar
        tb = QToolBar("Main"); tb.setMovable(False); self.addToolBar(tb)
        a = QAction("Scan library", self); a.setShortcut("Ctrl+R"); a.triggered.connect(self.scan); tb.addAction(a)
        a = QAction("Add a folder…", self); a.triggered.connect(self.add_folder); tb.addAction(a)
        a = QAction("Library folder…", self); a.triggered.connect(self.choose_root); tb.addAction(a)
        tb.addSeparator()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search — e.g.  glass choir   tag:pad   rating:>=4   unheard")
        self.search.setClearButtonEnabled(True); self.search.setMinimumWidth(420)
        self.search.textChanged.connect(lambda: self.search_timer.start(200))
        tb.addWidget(self.search)
        a = QAction("Save as smart crate", self); a.triggered.connect(self.save_smart); tb.addAction(a)
        a = QAction("Help", self); a.triggered.connect(lambda: QMessageBox.information(self, "Search and keys", HELP)); tb.addAction(a)
        tb.addSeparator()
        self.pills = {}
        for key, label in (("take5", "Take 5"), ("sub37", "Sub 37"), ("syntakt", "Syntakt"), ("keylab", "KeyLab")):
            b = QPushButton(f"○ {label}"); b.setFlat(True); b.setToolTip("Not connected to the PC")
            b.clicked.connect(lambda _=False, k=key: self.show_preflight(k))
            tb.addWidget(b); self.pills[key] = b
        self.put_back_act = QAction("Put back", self); self.put_back_act.setEnabled(False)
        self.put_back_act.setToolTip("Send the Take 5 the sound it had when the session started")
        self.put_back_act.triggered.connect(self.put_back); tb.addAction(self.put_back_act)
        self.preflights = {}
        self.search_timer = QTimer(self); self.search_timer.setSingleShot(True); self.search_timer.timeout.connect(self.refresh)

        # ---------------- sources
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True); self.tree.setMinimumWidth(240)
        self.tree.itemSelectionChanged.connect(self._scope_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)

        # ---------------- patch list
        self.model = PatchModel(self); self.proxy = SortProxy(self); self.proxy.setSourceModel(self.model)
        self.table = QTableView(); self.table.setModel(self.proxy); self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False); self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setAlternatingRowColors(True); self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        for c, w in enumerate([26, 240, 64, 190, 90, 150, 72, 52]):
            self.table.setColumnWidth(c, w)
        hh.setSectionResizeMode(1, QHeaderView.Stretch); hh.setStretchLastSection(False)
        self.table.selectionModel().selectionChanged.connect(self._selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(lambda _: self.toggle_preview())

        # ---------------- problems view
        pw = QWidget(); pv = QVBoxLayout(pw)
        self.prob_lbl = QLabel(""); pv.addWidget(self.prob_lbl)
        self.show_skipped = QCheckBox("Also show files that were skipped because they are not patches (PDFs, MIDI files…)")
        self.show_skipped.toggled.connect(self._fill_problems); pv.addWidget(self.show_skipped)
        self.prob_table = QTableWidget(0, 2); self.prob_table.setHorizontalHeaderLabels(["File", "Why"])
        self.prob_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.prob_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.prob_table.verticalHeader().setVisible(False); self.prob_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        pv.addWidget(self.prob_table)
        self.center = QStackedWidget(); self.center.addWidget(self.table); self.center.addWidget(pw)

        # ---------------- details
        self.session = Session(db)
        self.aud = AuditionPanel(self.session, db)
        self.aud.load_requested.connect(self.load_selected)
        self.aud.play_requested.connect(self.play_selected)
        self.details = Details(db, self.aud)
        self.details.changed.connect(self._details_changed)
        self.details.play_preview.connect(self.play)
        self.details.stop_preview.connect(self.stop)

        split = QSplitter(); split.addWidget(self.tree); split.addWidget(self.center); split.addWidget(self.details)
        split.setStretchFactor(1, 1); split.setSizes([250, 800, 350])
        self.setCentralWidget(split)

        # ---------------- status
        self.status_lbl = QLabel(""); self.statusBar().addWidget(self.status_lbl, 1)
        self.edit_lbl = QLabel(""); self.edit_lbl.setStyleSheet("color:#9a3412;")
        self.statusBar().addPermanentWidget(self.edit_lbl)
        self.progress = QProgressBar(); self.progress.setMaximumWidth(260); self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        if not audio.AVAILABLE:
            self.statusBar().addPermanentWidget(QLabel("No preview audio: " + audio.WHY))

        # ---------------- keys
        for k in range(6):
            QShortcut(QKeySequence(str(k)), self.table, activated=lambda k=k: self.rate_selected(k))
        QShortcut(QKeySequence("Space"), self.table, activated=self.toggle_preview)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: (self.search.setFocus(), self.search.selectAll()))
        QShortcut(QKeySequence("Delete"), self.table, activated=self.remove_from_crate)
        QShortcut(QKeySequence(Qt.Key_Return), self.table, activated=self.play_selected)
        QShortcut(QKeySequence(Qt.Key_Enter), self.table, activated=self.play_selected)
        QShortcut(QKeySequence("T"), self.table, activated=self.aud.transport_btn.toggle)
        QShortcut(QKeySequence("P"), self.table, activated=self.play_selected)
        QShortcut(QKeySequence("Escape"), self, activated=self.aud.stop)
        for sc in self.table.findChildren(QShortcut):
            sc.setContext(Qt.WidgetShortcut)        # typing in the search box must not rate or play
        self.session.devices_changed.connect(self._devices)
        self.session.preflight_done.connect(self._preflight)
        self.session.loaded.connect(self._loaded)
        self.session.verified.connect(self._verified)
        self.session.message.connect(lambda m: self.status_lbl.setText(m))

        self.fill_tree()
        self.refresh()
        QTimer.singleShot(300, self._first_run)

    # ================================================================ library roots and scanning
    def roots(self):
        out = []
        root = self.db.setting("library_root")
        if root and os.path.isdir(root):
            out.append((root, "library"))
        for f in json.loads(self.db.setting("extra_folders", "[]")):
            if os.path.isdir(f):
                out.append((f, "folder"))
        out += [(c, "card") for c in find_deluge_cards()]
        return out

    def _first_run(self):
        if not self.db.setting("library_root"):
            found = find_library_root()
            if found:
                self.db.set_setting("library_root", found)
                self.status_lbl.setText(f"Library: {found}")
            else:
                QMessageBox.information(self, APP, "Choose your Studio → Patches folder (in Google Drive) — the app "
                                                   "indexes it where it is and never changes it.")
                self.choose_root()
                if not self.db.setting("library_root"):
                    return
            self.scan()
        elif not self.db.one("SELECT COUNT(*) FROM patch"):
            self.scan()

    def choose_root(self):
        d = QFileDialog.getExistingDirectory(self, "Your Studio → Patches folder", self.db.setting("library_root") or "")
        if d:
            self.db.set_setting("library_root", os.path.normpath(d))
            self.scan()

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of patches (read where it is, never changed)")
        if d:
            extra = json.loads(self.db.setting("extra_folders", "[]"))
            if os.path.normpath(d) not in extra:
                extra.append(os.path.normpath(d))
                self.db.set_setting("extra_folders", json.dumps(extra))
            self.scan()

    def scan(self):
        if self.thread is not None:
            return
        roots = self.roots()
        if not roots:
            QMessageBox.warning(self, APP, "No library folder yet — use Library folder… to choose Studio → Patches.")
            return
        self.progress.show(); self.progress.setRange(0, 0)
        self.status_lbl.setText("Scanning " + ", ".join(r for r, _ in roots) + " …")
        self.thread = QThread(self)
        self.worker = ScanWorker(self.db.path, roots)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._scan_progress)
        self.worker.done.connect(self._scan_done)
        self.worker.failed.connect(self._scan_failed)
        self.thread.start()

    def _scan_progress(self, done, total, cur):
        self.progress.setRange(0, max(total, 1)); self.progress.setValue(done)
        self.status_lbl.setText(f"Scanning {done:,} of {total:,} — {cur}")

    def _end_thread(self):
        self.thread.quit(); self.thread.wait(); self.thread = None
        self.progress.hide()

    def _scan_done(self, p):
        self._end_thread()
        self.fill_tree(); self.refresh()
        total = self.db.one("SELECT COUNT(*) FROM patch")
        self.status_lbl.setText(f"Scanned {p.done:,} files · {total:,} patches in the library · {p.added:,} new"
                                + (f" · {p.problems} files with problems (see Problems)" if p.problems else ""))

    def _scan_failed(self, msg):
        self._end_thread()
        QMessageBox.critical(self, APP, "The scan stopped with an error:\n\n" + msg[-1500:])

    # ================================================================ sources tree
    def fill_tree(self):
        keep = self.scope
        self.tree.blockSignals(True); self.tree.clear()
        total = self.db.one("SELECT COUNT(*) FROM patch") or 0
        allit = QTreeWidgetItem([f"All patches  ({total:,})"]); allit.setData(0, Qt.UserRole, {})
        self.tree.addTopLevelItem(allit)
        counts = {r["synth"]: r["n"] for r in self.db.q("SELECT synth, COUNT(*) n FROM patch GROUP BY synth")}
        packs = self.db.q(
            "SELECT s.synth, s.type, s.pack, COUNT(DISTINCT po.patch_id) n FROM source s JOIN source_file sf "
            "ON sf.source_id=s.id JOIN patch_origin po ON po.file_id=sf.id GROUP BY s.synth, s.type, s.pack")
        for syn in SYNTH_ORDER:
            it = QTreeWidgetItem([f"{SYNTH_LABEL[syn]}  ({counts.get(syn, 0):,})"]); it.setData(0, Qt.UserRole, {"synth": syn})
            f = it.font(0); f.setBold(True); it.setFont(0, f)
            self.tree.addTopLevelItem(it)
            for t in ("free", "factory", "paid", "unsorted", "card"):
                rows = [r for r in packs if r["synth"] == syn and r["type"] == t]
                if not rows:
                    continue
                n = self.db.one("SELECT COUNT(DISTINCT po.patch_id) FROM source s JOIN source_file sf ON sf.source_id=s.id "
                                "JOIN patch_origin po ON po.file_id=sf.id WHERE s.synth=? AND s.type=?", (syn, t))
                ti = QTreeWidgetItem([f"{TYPE_LABEL[t]}  ({n:,})"]); ti.setData(0, Qt.UserRole, {"synth": syn, "type": t})
                it.addChild(ti)
                for r in sorted(rows, key=lambda r: (r["pack"] or "").lower()):
                    if r["pack"]:
                        pi = QTreeWidgetItem([f"{r['pack']}  ({r['n']:,})"])
                        pi.setData(0, Qt.UserRole, {"synth": syn, "type": t, "pack": r["pack"]}); ti.addChild(pi)
            it.setExpanded(True)
        other = self.db.q("SELECT kind, COUNT(*) n FROM patch WHERE kind!='sound' GROUP BY kind")
        for r in other:
            label = {"project": "Syntakt projects", "backup": "Whole-synth backups"}.get(r["kind"], r["kind"])
            oi = QTreeWidgetItem([f"{label}  ({r['n']:,})"]); oi.setData(0, Qt.UserRole, {"kind": r["kind"]})
            self.tree.addTopLevelItem(oi)
        crates = self.db.crates()
        ch = QTreeWidgetItem(["Crates"]); ch.setData(0, Qt.UserRole, {"header": "crates"})
        f = ch.font(0); f.setBold(True); ch.setFont(0, f); self.tree.addTopLevelItem(ch)
        for c in crates:
            if not c["query"]:
                ci = QTreeWidgetItem([f"{c['name']}  ({c['n']})"]); ci.setData(0, Qt.UserRole, {"crate_id": c["id"], "crate": c["name"]})
                ch.addChild(ci)
        sh = QTreeWidgetItem(["Smart crates"]); sh.setData(0, Qt.UserRole, {"header": "smart"})
        sh.setFont(0, f); self.tree.addTopLevelItem(sh)
        for c in crates:
            if c["query"]:
                si = QTreeWidgetItem([f"{c['name']}"]); si.setToolTip(0, c["query"])
                si.setData(0, Qt.UserRole, {"smart_id": c["id"], "query": c["query"], "crate": c["name"]}); sh.addChild(si)
        ch.setExpanded(True); sh.setExpanded(True)
        np_ = self.db.one("SELECT COUNT(*) FROM source_file WHERE status='problem'") or 0
        pi = QTreeWidgetItem([f"Problems  ({np_})"]); pi.setData(0, Qt.UserRole, {"problems": True})
        self.tree.addTopLevelItem(pi)
        self.tree.blockSignals(False)
        self._reselect(keep)

    def _reselect(self, scope):
        def walk(it):
            if (it.data(0, Qt.UserRole) or {}) == scope:
                self.tree.blockSignals(True); self.tree.setCurrentItem(it); self.tree.blockSignals(False); return True
            return any(walk(it.child(i)) for i in range(it.childCount()))
        for i in range(self.tree.topLevelItemCount()):
            if walk(self.tree.topLevelItem(i)):
                return

    def _scope_changed(self):
        it = self.tree.currentItem()
        d = (it.data(0, Qt.UserRole) if it else None) or {}
        if d.get("header"):
            return
        self.scope = d
        if d.get("problems"):
            self.center.setCurrentIndex(1); self._fill_problems(); return
        self.center.setCurrentIndex(0)
        self.refresh()

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        d = (it.data(0, Qt.UserRole) if it else None) or {}
        m = QMenu(self)
        if d.get("header") == "crates" or d.get("crate_id"):
            m.addAction("New crate…", self.new_crate)
        if d.get("crate_id") or d.get("smart_id"):
            cid = d.get("crate_id") or d.get("smart_id")
            m.addAction("Rename…", lambda: self._rename_crate(cid, d["crate"]))
            m.addAction("Delete crate", lambda: self._delete_crate(cid, d["crate"]))
        if d.get("pack") or d.get("type"):
            m.addAction("Show in Explorer", lambda: self._open_source(d))
        if not m.isEmpty():
            m.exec(self.tree.viewport().mapToGlobal(pos))

    # ================================================================ list
    def refresh(self):
        if self.center.currentIndex() == 1:
            return
        text = self.search.text()
        scope = {k: v for k, v in self.scope.items() if k in ("synth", "type", "pack", "crate_id", "kind")}
        if self.scope.get("query"):
            text = (self.scope["query"] + " " + text).strip()
        try:
            sql, args = search.build(text, scope)
            rows = self.db.q(sql, args)
            self.search.setStyleSheet("")
            self.search.setToolTip("")
        except search.QueryError as e:
            self.search.setStyleSheet("background:#fef2f2;")
            self.search.setToolTip(str(e)); self.status_lbl.setText("Search: " + str(e))
            return
        sel = {r["hash"] for r in self.selected_rows()}
        self.model.set_rows(rows)
        if sel:
            for i, r in enumerate(self.model.rows):
                if r["hash"] in sel:
                    self.table.selectRow(self.proxy.mapFromSource(self.model.index(i, 0)).row()); break
        self.status_lbl.setText(f"{len(rows):,} patches shown")

    def selected_rows(self):
        out = []
        for ix in self.table.selectionModel().selectedRows():
            out.append(self.model.row(self.proxy.mapToSource(ix).row()))
        return out

    def _selected(self):
        rows = self.selected_rows()
        self.details.show_row(rows[0] if rows else None)
        if len(rows) == 1 and self.aud.auto.isChecked() and self.session.ready(rows[0]["synth"]):
            if self.aud.autoplay.isChecked() and rows[0]["synth"] != "syntakt":
                QTimer.singleShot(0, self.play_selected)
            else:
                QTimer.singleShot(0, self.load_selected)

    def _details_changed(self):
        r = self.details.row
        self.refresh_row(r["hash"] if r else None)
        self.fill_tree_counts_only()

    def refresh_row(self, h):
        """Update one row in place (rating/tags) without losing the selection or scroll position."""
        if not h:
            return
        new = self.db.q(*search.one(h))
        if not new:
            return
        for i, r in enumerate(self.model.rows):
            if r["hash"] == h:
                self.model.rows[i] = dict(new[0])
                self.model.dataChanged.emit(self.model.index(i, 0), self.model.index(i, self.model.columnCount() - 1))

    def fill_tree_counts_only(self):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if (it.data(0, Qt.UserRole) or {}).get("header") == "crates":
                counts = {c["id"]: c["n"] for c in self.db.crates()}
                for j in range(it.childCount()):
                    c = it.child(j); d = c.data(0, Qt.UserRole)
                    c.setText(0, f"{d['crate']}  ({counts.get(d['crate_id'], 0)})")

    def rate_selected(self, n):
        rows = self.selected_rows()
        for r in rows:
            self.db.set_rating(r["hash"], n)
            self.refresh_row(r["hash"])
        if rows:
            self.details.show_row(self.details.row and next((x for x in self.model.rows if x["hash"] == self.details.row["hash"]), None))
            self.status_lbl.setText(f"Rated {len(rows)} patch{'es' if len(rows) > 1 else ''} {'★' * n if n else '(cleared)'}")

    # ================================================================ audition (M2)
    def load_selected(self):
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        r = rows[0]
        if r["kind"] != "sound" or not self.session.ready(r["synth"]):
            self.aud.show_row(r); return
        data = self.db.one("SELECT data FROM patch WHERE hash=?", (r["hash"],))
        try:
            self.session.load(r["synth"], r["hash"], r["name"], data)
        except Exception as e:                       # noqa: BLE001
            QMessageBox.warning(self, APP, f"Could not load '{r['name']}':\n\n{e}"); return
        self.refresh_row(r["hash"])

    def play_selected(self):
        """Play the test phrase on the selected patch — loading it first if the synth holds something else."""
        rows = self.selected_rows()
        if len(rows) != 1 or not self.session.ready(rows[0]["synth"]):
            return
        r = rows[0]
        drv = self.session.drivers.get(r["synth"])
        if r["synth"] == "syntakt":                     # no note: the sound is heard with FUNC + YES on the Syntakt
            if not drv.loaded or drv.loaded[0] != r["hash"]:
                self.load_selected()
            else:
                self._loaded("syntakt", r["hash"], r["name"])
            return
        if not drv.loaded or drv.loaded[0] != r["hash"]:
            self.load_selected()
            QTimer.singleShot(150, self.aud.play)          # let the synth take the new sound first
        else:
            self.aud.play()

    def _loaded(self, synth, h, name):
        label = SYNTH_LABEL[synth]
        self.status_lbl.setText(f"{label} edit buffer: {name}")
        if synth == "take5":
            self.edit_lbl.setText(f"Take 5 is playing '{name}' — pressing WRITE twice on the Take 5 now would save it "
                                  "over the program on its display")
            self.put_back_act.setEnabled(bool(self.session.drivers.get("take5") and self.session.drivers["take5"].pre))
        elif synth == "sub37":
            self.edit_lbl.setText(f"Sub 37 is playing '{name}' — saving on the panel now would store it in the "
                                  "selected preset")
        else:
            drv = self.session.drivers.get("syntakt")
            if drv:
                self.edit_lbl.setText(f"Syntakt: '{name}' is in {drv.bank}{drv.slot} — press FUNC + YES on the Syntakt "
                                      f"(sound browser on {drv.bank}{drv.slot}) to hear it")

    def _verified(self, synth, h, same, got):
        if same:
            d = self.session.drivers.get("syntakt")
            where = f"scratch slot {d.bank}{d.slot}" if synth == "syntakt" and d else "edit buffer"
            self.status_lbl.setText(f"{SYNTH_LABEL[synth]} confirmed: its {where} now holds '{got}'")
        else:
            self.status_lbl.setText(f"⚠ The {SYNTH_LABEL[synth]} did not take the patch — it reports '{got}'. "
                                    "Details are in session log.txt")
            self.status_lbl.setStyleSheet("color:#b91c1c;")
            QTimer.singleShot(8000, lambda: self.status_lbl.setStyleSheet(""))

    def put_back(self):
        if self.session.put_back("take5"):
            p = self.preflights.get("take5")
            self.edit_lbl.setText("")
            self.status_lbl.setText("The Take 5 has its original sound back")

    def _devices(self, state):
        for key, b in self.pills.items():
            st = state.get(key, "absent")
            label = {"take5": "Take 5", "sub37": "Sub 37", "syntakt": "Syntakt", "keylab": "KeyLab"}[key]
            mark, colour, tip = {"absent": ("○", "#6b7280", "Not connected to the PC"),
                                 "checking": ("◌", "#b7791f", "Checking…"),
                                 "ready": ("●", "#047857", "Connected and checked — click for details"),
                                 "problem": ("●", "#b91c1c", "Connected, but the check failed — click for details")}[st]
            b.setText(f"{mark} {label}"); b.setStyleSheet(f"color:{colour}; font-weight:600;"); b.setToolTip(tip)
        if not any(state.get(k) in ("ready", "checking") for k in ("take5", "sub37", "syntakt")):
            self.edit_lbl.setText("")
        if state.get("take5") == "absent":
            self.put_back_act.setEnabled(False)
        self.aud.show_row(self.details.row)

    def _preflight(self, key, p):
        self.preflights[key] = p
        label = SYNTH_LABEL[key]
        if p.ok:
            self.status_lbl.setText(f"{label} ready — " + p.lines[0][1])
        else:
            self.status_lbl.setText(f"{label}: " + next((t for s_, t in p.lines if s_ == "bad"), "check failed"))
            self.show_preflight(key)

    def show_preflight(self, key):
        if key == "keylab":
            st = self.session.state.get("keylab")
            QMessageBox.information(self, APP, "The KeyLab is on the PC. Tick 'Play it from the KeyLab' to play the synth "
                                               "being auditioned." if st != "absent" else
                                    "The KeyLab is not on the PC. In a patch session its USB goes to the PC with the synths.")
            return
        p = self.preflights.get(key)
        label = SYNTH_LABEL[key]
        if p is None:
            QMessageBox.information(self, APP, f"The {label} is not connected to the PC. For a patch session, give the PC "
                                               "the synths (the hub cable now, the sharing switch's button later).")
            return
        icon = {"ok": "✓", "warn": "!", "bad": "✗"}
        text = "<br>".join(f"<b>{icon[s_]}</b> {t}" for s_, t in p.lines)
        box = QMessageBox(self); box.setWindowTitle(f"{label} session check"); box.setText(text)
        again = box.addButton("Check again", QMessageBox.ActionRole)
        chan = box.addButton("MIDI channel…", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is again:
            self.session.recheck(key)
        elif box.clickedButton() is chan:
            drv = self.session.drivers.get(key)
            cur = drv.channel if drv else 1
            hint = {"syntakt": "the Syntakt's AUTO CHANNEL (SETTINGS › MIDI CONFIG › CHANNELS)",
                    "take5": "the Take 5's MIDI channel (GLOBAL 14)", "sub37": "the Sub 37's MIDI › 1.1 IN CHANNEL"}[key]
            ch, ok = QInputDialog.getInt(self, f"{label} MIDI channel", f"Test phrases and the KeyLab use {hint}:",
                                         cur, 1, 16)
            if ok:
                self.session.set_channel(key, ch)
                self.status_lbl.setText(f"{label} test phrases now go out on channel {ch}")

    # ================================================================ previews
    def play(self, path):
        try:
            self.player.play(path)
        except Exception as e:                       # noqa: BLE001
            QMessageBox.warning(self, APP, f"Could not play {os.path.basename(path)}:\n\n{e}"); return
        r = self.details.row
        if r:
            self.db.mark_heard(r["hash"])
        self.status_lbl.setText(f"Playing {os.path.basename(path)} — Space stops")

    def stop(self):
        self.player.stop()
        self.status_lbl.setText("Stopped")

    def toggle_preview(self):
        if self.player.playing():
            self.stop(); return
        p = self.details.first_preview()
        if p:
            self.play(p)
        else:
            self.status_lbl.setText("No preview recording for this patch")

    # ================================================================ crates
    def new_crate(self, add_selected=False):
        name, ok = QInputDialog.getText(self, "New crate", "Name (e.g. Keepers, To try, PS01 Fractal Dawn):")
        if not ok or not name.strip():
            return None
        try:
            cid = self.db.new_crate(name)
        except Exception:                            # noqa: BLE001
            QMessageBox.warning(self, APP, f"There is already a crate called '{name.strip()}'."); return None
        if add_selected:
            self.db.crate_add(cid, [r["hash"] for r in self.selected_rows()])
        self.fill_tree(); self.details.show_row(self.details.row)
        return cid

    def save_smart(self):
        q = self.search.text().strip()
        scope = self.scope
        parts = [q]
        if scope.get("synth"):
            parts.insert(0, f"synth:{scope['synth']}")
        if scope.get("type"):
            parts.insert(1, f"source:{scope['type']}")
        if scope.get("pack"):
            parts.insert(2, f'pack:"{scope["pack"]}"')
        q = " ".join(p for p in parts if p)
        if not q:
            QMessageBox.information(self, APP, "Type a search first (for example  tag:pad unheard), then save it."); return
        name, ok = QInputDialog.getText(self, "Save as smart crate", f"Name for the search\n{q}")
        if ok and name.strip():
            try:
                self.db.new_crate(name, q)
            except Exception:                        # noqa: BLE001
                QMessageBox.warning(self, APP, f"There is already a crate called '{name.strip()}'."); return
            self.fill_tree()

    def _rename_crate(self, cid, old):
        name, ok = QInputDialog.getText(self, "Rename crate", "New name:", text=old)
        if ok and name.strip():
            self.db.rename_crate(cid, name); self.fill_tree()

    def _delete_crate(self, cid, name):
        if QMessageBox.question(self, APP, f"Delete the crate '{name}'? The patches stay in the library.") == QMessageBox.Yes:
            self.db.delete_crate(cid); self.scope = {}; self.fill_tree(); self.refresh(); self.details.show_row(None)

    def remove_from_crate(self):
        cid = self.scope.get("crate_id")
        rows = self.selected_rows()
        if cid and rows:
            self.db.crate_remove(cid, [r["hash"] for r in rows]); self.refresh(); self.fill_tree_counts_only()

    def _table_menu(self, pos):
        rows = self.selected_rows()
        if not rows:
            return
        m = QMenu(self)
        cm = m.addMenu("Add to crate")
        for c in self.db.crates():
            if not c["query"]:
                cm.addAction(c["name"], lambda cid=c["id"]: (self.db.crate_add(cid, [r["hash"] for r in rows]),
                                                             self.fill_tree_counts_only(), self.details.show_row(self.details.row)))
        cm.addSeparator(); cm.addAction("New crate…", lambda: self.new_crate(add_selected=True))
        if self.scope.get("crate_id"):
            m.addAction(f"Remove from '{self.scope['crate']}'", self.remove_from_crate)
        rm = m.addMenu("Rate")
        for n in range(5, -1, -1):
            rm.addAction("★" * n if n else "Clear", lambda n=n: self.rate_selected(n))
        m.addSeparator()
        m.addAction("Show in Explorer", lambda: self._show_file(rows[0]))
        m.addAction("Copy name", lambda: self._copy("\n".join(r["name"] for r in rows)))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _copy(self, text):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

    def _show_file(self, row):
        o = self.db.origins(row["id"])
        if not o:
            return
        path = o[0]["path"]
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            QMessageBox.information(self, APP, path)

    def _open_source(self, d):
        r = self.db.q("SELECT sf.path, s.root FROM source s JOIN source_file sf ON sf.source_id=s.id WHERE s.synth=? AND s.type=? "
                      + ("AND s.pack=? " if d.get("pack") else "") + "LIMIT 1",
                      [d["synth"], d["type"]] + ([d["pack"]] if d.get("pack") else []))
        if r and sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(r[0]["path"])])

    # ================================================================ problems
    def _fill_problems(self):
        rows = self.db.q("SELECT sf.rel, sf.problem, sf.status, s.root FROM source_file sf JOIN source s ON s.id=sf.source_id "
                         "WHERE sf.status='problem'" + (" OR sf.status='skipped'" if self.show_skipped.isChecked() else "")
                         + " ORDER BY sf.status, sf.rel")
        np_ = sum(1 for r in rows if r["status"] == "problem")
        self.prob_lbl.setText(f"{np_} file{'s' if np_ != 1 else ''} could not be read. Each is listed whole with the reason; "
                              "nothing in them was half-read." if np_ else "Every patch file was read.")
        self.prob_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            a = QTableWidgetItem(r["rel"]); b = QTableWidgetItem(r["problem"] or "")
            if r["status"] == "skipped":
                a.setForeground(Qt.gray); b.setForeground(Qt.gray)
            self.prob_table.setItem(i, 0, a); self.prob_table.setItem(i, 1, b)

    def closeEvent(self, e):
        if self.thread is not None:
            self.worker.stop = True
            self.thread.quit(); self.thread.wait(3000)
        self.player.stop()
        t5 = self.session.drivers.get("take5")
        if t5 and t5.loaded and t5.pre and t5.pre.original_edit:
            self.session.put_back("take5")
        self.session.close()
        if any(self.session.state.get(k) != "absent" for k in ("take5", "sub37", "syntakt")):
            QMessageBox.information(self, APP, "Patch session over. Give the synths back to the Deluge (move the hub "
                                               "cable back, or press the sharing switch's button).")
        super().closeEvent(e)
