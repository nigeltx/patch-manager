"""Main window (spec 5.1), milestone 1: library, search, tags, ratings, crates, previews. No MIDI yet."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFrame, QPushButton, QToolButton, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QSplitter,
                               QStackedWidget, QTableView, QTableWidget, QTableWidgetItem, QToolBar, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import audio
from ..library import layout, ptypes, rebuild, search
from ..library.db import DB
from ..library.formats import SYNTH_LABEL
from ..library.scanner import Scanner, find_deluge_cards, find_library_root
from .audition import AuditionPanel
from .details import Details
from .session import Session
from .synths import MODES, READ_SYNTHS, ReadDialog, ReadWorker
from .type_dialog import TypeDialog
from .library_dialogs import AlertsDialog, DuplicatesDialog, RebuildDialog, ReorganizeDialog
from .models import TYPE_LABEL, PatchModel, SortProxy

APP = "Patch Manager"
from patchmgr import __version__
VERSION = f"{__version__} (Sources, Patches, Backups · duplicates · Take 5 user slots)"
SYNTH_ORDER = ["take5", "sub37", "deluge", "syntakt"]
HELP = """<b>Search</b> — type words to match names, packs and notes, and add filters:<br>
<code>synth:take5</code> &nbsp; <code>synth:sub37</code> &nbsp; <code>synth:deluge</code> &nbsp; <code>synth:syntakt</code><br>
<code>tag:pad tag:dark</code> (all must match) &nbsp; <code>rating:&gt;=4</code> &nbsp; <code>rating:0</code><br>
<code>source:paid</code> / factory / free / user / unsorted / card &nbsp; <code>pack:audiotent</code> &nbsp; <code>crate:keepers</code><br>
<code>unheard</code> &nbsp; <code>heard</code> &nbsp; <code>preview</code> &nbsp; <code>dupe</code> &nbsp; <code>unrated</code>
&nbsp; <code>factory</code> &nbsp; <code>onsynth</code> &nbsp; <code>blank</code><br>
<code>type:bass</code> &nbsp; <code>type:pad</code> &nbsp; <code>type:drums</code> &nbsp; <code>-type:fx</code><br>
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
        self.db.update_type_guesses()                # first start after 0.5.0: types for the whole library
        self.player = audio.Player()
        self.scope = {}
        self.thread = None
        self.setWindowTitle(f"{APP} {VERSION}")
        self.resize(1400, 860)

        # ---------------- toolbar
        tb = QToolBar("Main"); tb.setMovable(False); self.addToolBar(tb)
        self.lib_btn = QToolButton(); self.lib_btn.setText("Library ▾"); self.lib_btn.setPopupMode(QToolButton.InstantPopup)
        lm = QMenu(self)
        a = lm.addAction("Scan library", self.scan); a.setShortcut("Ctrl+R"); self.addAction(a)
        lm.addAction("Rebuild Patches…", self.open_rebuild)
        lm.addAction("Reorganize library…", self.open_reorganize)
        lm.addAction("Sources changed or missing…", self.open_alerts)
        lm.addAction("Find duplicates in Sources…", self.open_duplicates)
        lm.addSeparator()
        lm.addAction("Add a folder…", self.add_folder)
        lm.addAction("Library folder…", self.choose_root)
        self.lib_btn.setMenu(lm); tb.addWidget(self.lib_btn)
        self.rebuild_act = QAction("Rebuild", self); self.rebuild_act.triggered.connect(self.open_rebuild)
        self.rebuild_act.setToolTip("Bring Patches up to date with Sources: one file per sound, by type")
        tb.addAction(self.rebuild_act)
        self.get_btn = QToolButton(); self.get_btn.setText("Get from synth ▾"); self.get_btn.setPopupMode(QToolButton.InstantPopup)
        self.get_menu = QMenu(self); self.get_menu.aboutToShow.connect(self._fill_get_menu); self.get_btn.setMenu(self.get_menu)
        self.get_btn.setToolTip("Read a synth and keep the sounds that are in no source in Sources › User")
        tb.addWidget(self.get_btn)
        tb.addSeparator()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search — e.g.  glass choir   tag:pad   rating:>=4   unheard")
        self.search.setClearButtonEnabled(True); self.search.setMinimumWidth(420)
        self.search.textChanged.connect(lambda: self.search_timer.start(200))
        tb.addWidget(self.search)
        a = QAction("Save as smart crate", self); a.triggered.connect(self.save_smart); tb.addAction(a)
        from .. import __version__ as _v
        from ..updater import UpdateManager
        self.updates = UpdateManager(self, APP, "nigeltx/patch-manager", "PatchManager-Setup-", _v,
                                     self.db.setting, self.db.set_setting)
        self.help_btn = QToolButton(); self.help_btn.setText("Help ▾"); self.help_btn.setPopupMode(QToolButton.InstantPopup)
        hm = QMenu(self)
        hm.addAction("Search and keys", lambda: QMessageBox.information(self, "Search and keys", HELP))
        hm.addSeparator()
        hm.addAction("Check for updates…", lambda: self.updates.check(asked=True))
        hm.addAction("Update settings…", self.updates.settings_dialog)
        hm.addAction(f"About Patch Manager {_v}", lambda: QMessageBox.information(
            self, APP, f"Patch Manager {_v}\nnigeltx/patch-manager on GitHub"))
        self.help_btn.setMenu(hm); tb.addWidget(self.help_btn)
        self.synths_btn = QToolButton(); self.synths_btn.setText("Synths ▾")
        self.synths_btn.setPopupMode(QToolButton.InstantPopup)
        self.synths_menu = QMenu(self); self.synths_menu.aboutToShow.connect(self._fill_synths_menu)
        self.synths_btn.setMenu(self.synths_menu)
        self.synths_btn.setToolTip("Initialize factory presets, back up a synth, or refresh what is on it")
        tb.addWidget(self.synths_btn)
        tb.addSeparator()
        self.pills = {}
        for key, label in (("take5", "Take 5"), ("sub37", "Sub 37"), ("syntakt", "Syntakt"), ("deluge", "Deluge")):
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
        for c, w in enumerate([26, 24, 220, 80, 56, 140, 70, 100, 56, 44]):
            self.table.setColumnWidth(c, w)
        hh.setSectionResizeMode(2, QHeaderView.Stretch); hh.setStretchLastSection(False)
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
        listw = QWidget(); lv = QVBoxLayout(listw); lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(4)
        self.banner = QFrame(); self.banner.setStyleSheet("QFrame{background:#fff7ed; border:1px solid #fdba74; "
                                                          "border-radius:4px;}")
        bl = QHBoxLayout(self.banner); bl.setContentsMargins(10, 6, 10, 6)
        self.banner_lbl = QLabel(""); self.banner_lbl.setWordWrap(True); self.banner_lbl.setStyleSheet("border:none;")
        bl.addWidget(self.banner_lbl, 1)
        self.banner_go = QPushButton("Initialize…"); bl.addWidget(self.banner_go)
        later = QPushButton("Later"); later.clicked.connect(self._banner_later); bl.addWidget(later)
        self.banner_go.clicked.connect(lambda: self.open_read("init", self.banner_synth))
        self.banner.hide(); self.banner_synth = None; self.banner_skip = set()
        lv.addWidget(self.banner); lv.addWidget(self.table)
        self.center = QStackedWidget(); self.center.addWidget(listw); self.center.addWidget(pw)
        self.bg_thread = None

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
        root = self.db.setting("library_root")
        if root and os.path.isdir(root):
            layout.load_types(self.db, root)
            from ..library import duplicates
            duplicates.load(self.db, root)
            from ..library import userslots as uslots
            if layout.is_new_layout(root, "take5") and uslots.keep_init_as_patch(root):
                # the Take 5's init program (Basic Program) is now a patch too: index it and put it in Patches
                Scanner(self.db.path, [(root, "library")]).run()
                QTimer.singleShot(500, lambda: self.open_rebuild(["take5"], quiet=True))
            if not self.db.setting("reorganize_offered") and not layout.reorganize_plan(root).empty:
                self.db.set_setting("reorganize_offered", "1")
                if QMessageBox.question(
                        self, APP, "<b>New in this version: Sources and Patches.</b><br><br>Each synth folder gets "
                        "<b>Sources</b> — every pack and factory set as it came, never changed — and <b>Patches</b>, "
                        "where every sound is kept once, in a folder for its type.<br><br>See what Reorganize would move "
                        "now? (It shows the plan first; it is also on the Library menu.)") == QMessageBox.Yes:
                    self.open_reorganize()
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
        root = self.db.setting("library_root")
        if p.changed or p.missing:
            QMessageBox.warning(self, APP, f"{p.changed + p.missing} file(s) in Sources changed or went missing outside "
                                           "the app. Their sounds are kept as they were until you decide.")
            self.open_alerts()
        elif root and p.sources_changed and any(layout.is_new_layout(root, s) for s in layout.SYNTH_ORDER):
            self.open_rebuild(quiet=True)

    def _scan_failed(self, msg):
        self._end_thread()
        QMessageBox.critical(self, APP, "The scan stopped with an error:\n\n" + msg[-1500:])

    # ================================================================ Sources and Patches (spec 4.4, 4.5, 4.11)
    def _root_or_warn(self):
        root = self.db.setting("library_root")
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, APP, "Choose the library folder first (Library ▾ › Library folder…).")
            return None
        return root

    def open_rebuild(self, synths=None, quiet=False):
        root = self._root_or_warn()
        if not root or self.thread is not None:
            return
        dlg = RebuildDialog(self, self.db, root, synths or layout.SYNTH_ORDER)
        if dlg.nothing and quiet:
            return
        if dlg.exec() and dlg.result_:
            r = dlg.result_
            self.status_lbl.setText(f"Patches rebuilt: {r['added']} added, {r['moved']} moved, {r['removed']} taken out")
            if r["hand"]:
                self.scan()
                return
        self.fill_tree(); self.refresh()

    def open_reorganize(self):
        root = self._root_or_warn()
        if not root or self.thread is not None:
            return
        dlg = ReorganizeDialog(self, self.db, root)
        if dlg.exec() and dlg.result_:
            moved, errors, listed = dlg.result_
            if listed:
                self.status_lbl.setText(f"Reorganized. {len(listed)} empty old folders are on the Studio Cleanup list — "
                                        "run Clean up Studio.cmd when convenient.")
            self.scan()

    def open_duplicates(self):
        root = self._root_or_warn()
        if not root or self.thread is not None:
            return
        dlg = DuplicatesDialog(self, self.db, root)
        dlg.exec()
        if dlg.changed_anything:
            self.open_rebuild(quiet=True)
        self.fill_tree(); self.refresh()

    def open_alerts(self):
        dlg = AlertsDialog(self, self.db)
        dlg.exec()
        if dlg.changed_anything:
            self.scan()
        else:
            self.fill_tree()

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
            "ON sf.source_id=s.id JOIN live_origin po ON po.file_id=sf.id GROUP BY s.synth, s.type, s.pack")
        for syn in SYNTH_ORDER:
            it = QTreeWidgetItem([f"{SYNTH_LABEL[syn]}  ({counts.get(syn, 0):,})"]); it.setData(0, Qt.UserRole, {"synth": syn})
            f = it.font(0); f.setBold(True); it.setFont(0, f)
            self.tree.addTopLevelItem(it)
            self._patches_branch(it, syn)
            src = None
            for t in ("factory", "paid", "free", "user", "unsorted", "card", "backup"):
                rows = [r for r in packs if r["synth"] == syn and r["type"] == t]
                if not rows:
                    continue
                parent = it
                if t in layout.LIB_TYPES:
                    if src is None:
                        src = QTreeWidgetItem(["Sources  🔒"]); src.setData(0, Qt.UserRole, {"header": "sources"})
                        src.setToolTip(0, "Every pack, factory set and synth read as it came. The app never changes "
                                          "these files; Patches is built from them.")
                        it.addChild(src)
                    parent = src
                n = self.db.one("SELECT COUNT(DISTINCT po.patch_id) FROM source s JOIN source_file sf ON sf.source_id=s.id "
                                "JOIN live_origin po ON po.file_id=sf.id WHERE s.synth=? AND s.type=?", (syn, t))
                ti = QTreeWidgetItem([f"{TYPE_LABEL[t]}  ({n:,})"]); ti.setData(0, Qt.UserRole, {"synth": syn, "type": t})
                parent.addChild(ti)
                for r in sorted(rows, key=lambda r: (r["pack"] or "").lower()):
                    if r["pack"]:
                        pi = QTreeWidgetItem([f"{r['pack']}  ({r['n']:,})"])
                        pi.setData(0, Qt.UserRole, {"synth": syn, "type": t, "pack": r["pack"]}); ti.addChild(pi)
            it.setExpanded(True)
        if not self.db.one("SELECT 1 FROM patch_file LIMIT 1"):
            self._type_section()                     # until Patches has been built, BY TYPE across the library
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
        na = len(self.db.source_alerts())
        if na:
            ai = QTreeWidgetItem([f"⚠ Sources changed or missing  ({na})"]); ai.setData(0, Qt.UserRole, {"alerts": True})
            ai.setForeground(0, Qt.darkRed); self.tree.addTopLevelItem(ai)
        self.tree.blockSignals(False)
        self._reselect(keep)

    def _patches_branch(self, parent, syn):
        """<Synth> › Patches › one line per type folder, with how many are on the synth (spec 4.5)."""
        counts = rebuild.counts(self.db, syn)
        if not counts:
            return
        label = SYNTH_LABEL[syn]
        total = sum(n for n, _ in counts.values()); on = sum(o for _, o in counts.values())
        pi = QTreeWidgetItem([f"Patches  ({total:,}" + (f" · {on} on the {label}" if on else "") + ")"])
        pi.setData(0, Qt.UserRole, {"synth": syn, "patches": True})
        f = pi.font(0); f.setBold(True); pi.setFont(0, f)
        parent.addChild(pi)
        order = [rebuild.folder_name(t) for t in ptypes.ALL_TYPES + [ptypes.UNKNOWN]]
        for folder in sorted(counts, key=lambda x: (order.index(x) if x in order else 99, x)):
            n, o = counts[folder]
            ci = QTreeWidgetItem([f"{folder}  ({n:,}" + (f" · {o} on" if o else "") + ")"])
            ci.setData(0, Qt.UserRole, {"synth": syn, "pfolder": folder})
            if o:
                ci.setForeground(0, Qt.darkGreen)
            pi.addChild(ci)
        pi.setExpanded(True)

    def _type_section(self):
        """BY TYPE, as in the Sample Manager: the app's guess from names, tags and folders, or your own choice."""
        counts = self.db.type_counts()
        hdr = QTreeWidgetItem(["By type"]); hdr.setData(0, Qt.UserRole, {"header": "types"})
        f = hdr.font(0); f.setBold(True); hdr.setFont(0, f)
        hdr.setToolTip(0, "Guessed from the patch name, its tags and its folders. Right-click a patch → Set type… to "
                          "correct it.")
        self.tree.addTopLevelItem(hdr)
        for t in ptypes.MUSIC:
            if counts.get(t):
                it = QTreeWidgetItem([f"{t}  ({counts[t]:,})"]); it.setData(0, Qt.UserRole, {"ptype": t}); hdr.addChild(it)
        if counts.get("Drums"):
            d = QTreeWidgetItem([f"Drums  ({counts['Drums']:,})"]); d.setData(0, Qt.UserRole, {"ptype": "Drums"})
            hdr.addChild(d)
            for t in ptypes.DRUMS:
                if counts.get(t):
                    it = QTreeWidgetItem([f"{t}  ({counts[t]:,})"]); it.setData(0, Qt.UserRole, {"ptype": t}); d.addChild(it)
        if counts.get(ptypes.UNKNOWN):
            it = QTreeWidgetItem([f"Unknown  ({counts[ptypes.UNKNOWN]:,})"])
            it.setData(0, Qt.UserRole, {"ptype": ptypes.UNKNOWN}); hdr.addChild(it)
        hdr.setExpanded(True)

    def set_type_selected(self):
        rows = [r for r in self.selected_rows() if r["kind"] == "sound"]
        if not rows:
            return
        dlg = TypeDialog(self, len(rows), ptypes.unpack(rows[0].get("ptypes")))
        if dlg.exec() and dlg.result_value:
            hashes = [r["hash"] for r in rows]
            if dlg.result_value[0] == "set":
                self.db.set_user_type(hashes, dlg.result_value[1])
            else:
                self.db.clear_user_type(hashes)
            root = self.db.setting("library_root")
            if root and os.path.isdir(root):
                layout.save_types(self.db, root)
                self._move_retyped(root, set(hashes))
            self.fill_tree(); self.refresh()
            self.details.show_row(self.details.row and next((x for x in self.model.rows
                                                             if x["hash"] == self.details.row["hash"]), None))

    def _move_retyped(self, root, hashes):
        """Set type… moves the file in Patches to its new folder at once; nothing else changes (spec 4.5)."""
        if not self.db.one("SELECT 1 FROM patch_file LIMIT 1"):
            return
        pl = rebuild.plan(self.db, root)
        only = rebuild.Plan(root=root, moves=[m for m in pl.moves if m[2] in hashes])
        if only.moves:
            rebuild.apply(self.db, only)

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
        if d.get("alerts"):
            QTimer.singleShot(0, self.open_alerts); return
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
        if d.get("type") == "user" and d.get("pack"):
            m.addSeparator()
            m.addAction("Delete user source…", lambda: self._delete_user_source(d))
        if d.get("pfolder") or d.get("patches"):
            m.addAction("Show in Explorer", lambda: self._open_patches(d))
        if not m.isEmpty():
            m.exec(self.tree.viewport().mapToGlobal(pos))

    # ================================================================ list
    def refresh(self):
        if self.center.currentIndex() == 1:
            return
        text = self.search.text()
        scope = {k: v for k, v in self.scope.items() if k in ("synth", "type", "pack", "crate_id", "kind", "ptype",
                                                              "pfolder", "patches")}
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
        self.status_lbl.setText(f"{len(rows):,} patch{'' if len(rows) == 1 else 'es'} shown")

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
            label = SYNTH_LABEL[key]
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
            if self.db.last_init(key):
                self.background_refresh(key)
            elif key not in self.banner_skip:
                self._offer_init(key)
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
            QMessageBox.information(self, APP, f"The {label} is not connected to the PC. Check its USB cable reaches the "
                                               "hub on the PC and the synth is switched on.")
            return
        icon = {"ok": "✓", "warn": "!", "bad": "✗"}
        text = "<br>".join(f"<b>{icon[s_]}</b> {t}" for s_, t in p.lines)
        box = QMessageBox(self); box.setWindowTitle(f"{label} session check"); box.setText(text)
        again = box.addButton("Check again", QMessageBox.ActionRole)
        chan = box.addButton("MIDI channel…", QMessageBox.ActionRole) if key != "deluge" else None
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

    # ================================================================ reading the synths (M3)
    def _fill_synths_menu(self):
        m = self.synths_menu; m.clear()
        for key in READ_SYNTHS:
            label = SYNTH_LABEL[key]
            ok = self.session.ready(key)
            init = self.db.last_init(key)
            sub = m.addMenu(f"{label}" + ("" if ok else "  (not connected)"))
            for mode in ("get", "init", "backup", "refresh"):
                a = sub.addAction(MODES[mode] + ("…" if mode != "refresh" else ""),
                                  lambda k=key, md=mode: self.open_read(md, k))
                a.setEnabled(ok and (mode != "refresh" or key != "sub37"))
            if key == "take5":
                sub.addSeparator()
                t5 = [r for r in self.selected_rows() if r["synth"] == "take5" and r["kind"] == "sound"]
                a = sub.addAction(f"Send {len(t5)} selected to a slot…" if t5 else "Send to a slot… (select Take 5 patches)",
                                  lambda: self.open_send_slot())
                a.setEnabled(ok and bool(t5))
                sub.addAction("Sync…", lambda: self.open_user_slots("SyncDialog")).setEnabled(ok)
                sub.addSeparator()
                for text, cls in (("Clear user slots…", "ClearDialog"), ("Fill user slots…", "FillDialog"),
                                  ("Undo the last write…", "UndoDialog")):
                    a = sub.addAction(text, lambda c=cls: self.open_user_slots(c))
                    a.setEnabled(ok or cls == "FillDialog")
            sub.addSeparator()
            when = time.strftime('%d %b %Y', time.localtime(init["run_at"])) if init else None
            info = sub.addAction(f"Initialized {when}: {init['marked_factory']} factory presets" if init
                                 else "Not initialized yet")
            info.setEnabled(False)

    def open_send_slot(self, rows=None):
        """Send to slot (spec 5.2, 7.1) for the selected Take 5 patches."""
        rows = [r for r in (rows or self.selected_rows()) if r["synth"] == "take5" and r["kind"] == "sound"]
        if not rows:
            QMessageBox.information(self, APP, "Select one or more Take 5 patches in the list first.")
            return
        patches = [{"hash": r["hash"], "name": r["name"],
                    "data": self.db.one("SELECT data FROM patch WHERE hash=?", (r["hash"],))} for r in rows]
        self.open_user_slots("SendDialog", patches)

    def open_user_slots(self, cls, patches=None):
        """Take 5 user slots (spec 5.2, 7.1, 7.8–7.10)."""
        from . import userslots as us
        root = self._root_or_warn()
        if not root:
            return
        if not layout.is_new_layout(root, "take5"):
            QMessageBox.information(self, APP, "Reorganize the library first (Library ▾ › Reorganize library…), so your "
                                               "own sounds have a place in Sources › User before any slot is cleared.")
            return
        dlg = (us.SendDialog(self, self.session, self.db, root, patches) if cls == "SendDialog"
               else getattr(us, cls)(self, self.session, self.db, root))
        dlg.exec()
        if cls in ("ClearDialog", "SendDialog", "SyncDialog"):
            self.open_rebuild(["take5"], quiet=True)       # sounds kept from the user slots join Patches
        self.fill_tree(); self.refresh()

    def _fill_get_menu(self):
        m = self.get_menu; m.clear()
        for key in READ_SYNTHS:
            ok = self.session.ready(key)
            a = m.addAction(SYNTH_LABEL[key] + ("…" if ok else "  (not connected)"), lambda k=key: self.open_read("get", k))
            a.setEnabled(ok)

    def open_read(self, mode, key):
        if key is None:
            return
        root = self.db.setting("library_root")
        if mode in ("get", "init") and root and not layout.is_new_layout(root, key):
            if QMessageBox.question(self, APP, f"{MODES[mode]} keeps sounds in Sources, which comes with the new "
                                               "library layout. Reorganize the library now?") == QMessageBox.Yes:
                self.open_reorganize()
            if not layout.is_new_layout(root, key):
                return
        if self.bg_thread is not None:
            QMessageBox.information(self, APP, "The app is still reading a synth in the background — try again in a "
                                               "minute.")
            return
        if mode != "refresh" and not self.db.setting("library_root"):
            QMessageBox.warning(self, APP, "Choose the library folder first (Library folder…) — backups are kept in it.")
            return
        if key == self.banner_synth:
            self.banner.hide()
        dlg = ReadDialog(self, mode, key, self.session, self.db, self.db.setting("library_root"))
        dlg.exec()
        kept = getattr(dlg, "kept", None)
        if kept and (kept["n_factory"] or kept["n_user"]):
            self.status_lbl.setText(f"Kept {kept['n_user']} sounds in Sources › User"
                                    + (f" and {kept['n_factory']} in Sources › Factory" if kept["n_factory"] else ""))
            self.open_rebuild([key], quiet=True)
        self.fill_tree(); self.refresh()
        rows = self.selected_rows()
        self.details.show_row(rows[0] if rows else None)
        self._next_banner()

    def _offer_init(self, key):
        if self.banner.isVisible() and self.banner_synth != key:
            return
        self.banner_synth = key
        label = SYNTH_LABEL[key]
        self.banner_lbl.setText(f"<b>The {label} has not been initialized.</b> Initialize factory presets reads it once: "
                                f"factory presets get an <b>F</b>, and every sound on the {label} shows green.")
        self.banner.show()

    def _banner_later(self):
        if self.banner_synth:
            self.banner_skip.add(self.banner_synth)
        self.banner.hide(); self._next_banner()

    def _next_banner(self):
        self.banner_synth = None if not self.banner.isVisible() else self.banner_synth
        for key in ("take5", "sub37", "syntakt"):
            if self.session.ready(key) and not self.db.last_init(key) and key not in self.banner_skip:
                self._offer_init(key); return

    def background_refresh(self, key):
        """After a synth connects: read what is on it now, quietly, so the green rows are current (spec 4.8)."""
        if key not in ("take5", "syntakt") or self.bg_thread is not None:
            return
        self.bg_thread = QThread(self)
        self.bg_worker = ReadWorker("refresh", key, self.session, self.db.path, self.db.setting("library_root"))
        self.bg_worker.moveToThread(self.bg_thread)
        self.bg_thread.started.connect(self.bg_worker.run)
        label = SYNTH_LABEL[key]
        self.bg_worker.progress.connect(lambda d, t, txt: self.status_lbl.setText(
            f"Reading what is on the {label}: {d} of {t}" if t else f"Reading the {label}…"))
        self.bg_worker.done.connect(lambda res: self._bg_end(f"Green rows updated — read {len(res['reads'])} slots on "
                                                            f"the {label}"))
        self.bg_worker.failed.connect(lambda msg: self._bg_end(f"Could not read what is on the {label}: {msg}"))
        self.bg_thread.start()

    def _bg_end(self, text):
        if self.bg_thread:
            self.bg_thread.quit(); self.bg_thread.wait(); self.bg_thread = None
        self.status_lbl.setText(text)
        self.refresh()
        rows = self.selected_rows()
        self.details.show_row(rows[0] if rows else None)

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
        sounds = [r for r in rows if r["kind"] == "sound"]
        if sounds:
            m.addAction(f"Set type… ({len(sounds)})" if len(sounds) > 1 else "Set type…", self.set_type_selected)
        t5 = [r for r in sounds if r["synth"] == "take5"]
        if t5:
            from ..library import userslots as uslots
            m.addSeparator()
            a = m.addAction(f"Send to a Take 5 slot… ({len(t5)})" if len(t5) > 1 else "Send to a Take 5 slot…",
                            lambda: self.open_send_slot(t5))
            a.setEnabled(self.session.ready("take5"))
            m.addAction(f"Add to '{uslots.FOR_CRATE}' (Sync sends it)",
                        lambda: (self.db.crate_add(uslots.for_crate(self.db), [r["hash"] for r in t5]),
                                 self.fill_tree_counts_only()))
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
        pf = self.db.one("SELECT path FROM patch_file WHERE hash=?", (row["hash"],))
        o = self.db.origins(row["id"])
        if not o and not pf:
            return
        use_pf = pf and (self.scope.get("pfolder") or self.scope.get("patches") or not o)
        path = pf if use_pf else o[0]["path"]
        path = path.split("::")[0]
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            QMessageBox.information(self, APP, path)

    def _delete_user_source(self, d):
        """Sources › User is the one place you can remove from in the app (spec 4.4). It goes to Google Drive's trash."""
        import shutil
        root = self._root_or_warn()
        if not root:
            return
        folder = os.path.join(layout.sources_dir(root, d["synth"], "User"), d["pack"])
        if not os.path.exists(folder):
            QMessageBox.information(self, APP, f"{folder} is not there any more."); self.scan(); return
        name, ok = QInputDialog.getText(self, "Delete user source", f"Delete <b>{d['pack']}</b> from "
                                        f"{SYNTH_LABEL[d['synth']]} › Sources › User? It goes to Google Drive's trash. "
                                        "Its sounds leave Patches at the next rebuild unless another source has them."
                                        "<br><br>Type its name to confirm:")
        if not ok:
            return
        if name.strip() != d["pack"]:
            QMessageBox.information(self, APP, "The name did not match — nothing was deleted."); return
        try:
            shutil.rmtree(folder) if os.path.isdir(folder) else os.remove(folder)
        except OSError as e:
            QMessageBox.warning(self, APP, f"Could not delete it: {e.strerror or e}"); return
        self.scan()

    def _open_patches(self, d):
        root = self.db.setting("library_root")
        if not root:
            return
        p = layout.patches_dir(root, d["synth"])
        if d.get("pfolder"):
            p = os.path.join(p, d["pfolder"])
        if sys.platform == "win32" and os.path.isdir(p):
            subprocess.Popen(["explorer", os.path.normpath(p)])

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
        if self.bg_thread is not None:
            self.bg_worker.stop = True
            self.bg_thread.quit(); self.bg_thread.wait(5000)
        self.player.stop()
        t5 = self.session.drivers.get("take5")
        if t5 and t5.loaded and t5.pre and t5.pre.original_edit:
            self.session.put_back("take5")
        self.session.close()
        super().closeEvent(e)
