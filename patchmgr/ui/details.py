"""The details panel on the right of the main window (spec 5.1): rating, tags, notes, crates, previews, origins."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QCompleter, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QScrollArea, QToolButton, QVBoxLayout,
                               QWidget)

from ..library.formats import SYNTH_LABEL
from .models import TYPE_LABEL


class FlowRow(QWidget):
    """A wrapping row of small buttons (tags)."""

    def __init__(self, per_row=4):
        super().__init__()
        self.grid = QGridLayout(self); self.grid.setContentsMargins(0, 0, 0, 0); self.grid.setSpacing(4)
        self.per_row = per_row

    def clear(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.setParent(None); w.deleteLater()

    def add(self, w):
        n = self.grid.count()
        self.grid.addWidget(w, n // self.per_row, n % self.per_row)


def heading(text):
    l = QLabel(text); l.setStyleSheet("font-weight:600; color:#374151; margin-top:10px;")
    return l


class Details(QScrollArea):
    changed = Signal()                 # something the list shows changed (rating, tags, crates)
    play_preview = Signal(str)
    stop_preview = Signal()

    def __init__(self, db, audition=None):
        super().__init__()
        self.db = db
        self.audition = audition
        self.row = None
        self.setWidgetResizable(True); self.setMinimumWidth(340)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(); self.setWidget(body)
        v = QVBoxLayout(body); v.setAlignment(Qt.AlignTop)

        self.title = QLabel("Select a patch"); self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size:16px; font-weight:700;")
        self.sub = QLabel(""); self.sub.setStyleSheet("color:#6b7280;"); self.sub.setWordWrap(True)
        v.addWidget(self.title); v.addWidget(self.sub)

        self.aud_slot = QVBoxLayout(); v.addLayout(self.aud_slot)
        self.onsynth = QLabel(""); self.onsynth.setWordWrap(True); self.onsynth.setTextFormat(Qt.RichText)
        self.onsynth.setStyleSheet("font-size:12px; margin-top:6px;"); v.addWidget(self.onsynth)

        v.addWidget(heading("Rating  (keys 1–5, 0 clears)"))
        sr = QHBoxLayout(); self.star_btns = []
        for i in range(1, 6):
            b = QToolButton(); b.setText("☆"); b.setStyleSheet("font-size:20px; color:#b7791f; border:none;")
            b.clicked.connect(lambda _=False, i=i: self.rate(i)); sr.addWidget(b); self.star_btns.append(b)
        clr = QToolButton(); clr.setText("clear"); clr.clicked.connect(lambda: self.rate(0)); sr.addWidget(clr)
        sr.addStretch(1); v.addLayout(sr)

        v.addWidget(heading("Tags"))
        self.tag_hint = QLabel("Grey tags were found in the file or guessed from the name — click one to confirm it.")
        self.tag_hint.setWordWrap(True); self.tag_hint.setStyleSheet("color:#6b7280; font-size:11px;")
        v.addWidget(self.tag_hint)
        self.tags = FlowRow(per_row=3); v.addWidget(self.tags)
        self.tag_edit = QLineEdit(); self.tag_edit.setPlaceholderText("Add a tag and press Enter")
        self.tag_edit.returnPressed.connect(self._add_tag); v.addWidget(self.tag_edit)
        self.starter = FlowRow(per_row=3); v.addWidget(self.starter)

        v.addWidget(heading("Notes"))
        self.notes = QLineEdit(); self.notes.setPlaceholderText("e.g. used in PS01 Fractal Dawn")
        self.notes.editingFinished.connect(self._save_notes); v.addWidget(self.notes)

        v.addWidget(heading("Crates"))
        self.crate_box = QVBoxLayout(); w = QWidget(); w.setLayout(self.crate_box); v.addWidget(w)

        v.addWidget(heading("Preview recordings  (Space plays)"))
        self.prev_list = QListWidget(); self.prev_list.setMaximumHeight(90)
        self.prev_list.itemDoubleClicked.connect(lambda it: self.play_preview.emit(it.data(Qt.UserRole)))
        v.addWidget(self.prev_list)
        pr = QHBoxLayout()
        self.play_btn = QPushButton("▶ Play"); self.play_btn.clicked.connect(self._play)
        self.stop_btn = QPushButton("■ Stop"); self.stop_btn.clicked.connect(self.stop_preview.emit)
        pr.addWidget(self.play_btn); pr.addWidget(self.stop_btn); pr.addStretch(1); v.addLayout(pr)

        v.addWidget(heading("Where it is"))
        self.where = QLabel(""); self.where.setWordWrap(True); self.where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.where.setStyleSheet("color:#374151; font-size:11px;"); v.addWidget(self.where)
        self.heard = QLabel(""); self.heard.setStyleSheet("color:#6b7280;"); v.addWidget(self.heard)
        if audition is not None:
            self.aud_slot.addWidget(audition)
        self.show_row(None)

    # ---------------------------------------------------------------- fill
    def show_row(self, row):
        self.row = row
        if self.audition is not None:
            self.audition.show_row(row)
        on = row is not None
        for w in (self.tag_edit, self.notes, self.play_btn, self.stop_btn):
            w.setEnabled(on)
        for b in self.star_btns:
            b.setEnabled(on)
        if not on:
            self.title.setText("Select a patch"); self.sub.setText(""); self.tags.clear(); self.starter.clear()
            self.notes.setText(""); self.prev_list.clear(); self.where.setText(""); self.heard.setText("")
            self.onsynth.setText("")
            self._fill_crates(); return
        h = row["hash"]
        self.title.setText(row["name"])
        kind = {"project": " · Syntakt project", "backup": " · whole-synth backup"}.get(row["kind"], "")
        self.sub.setText(f"{SYNTH_LABEL.get(row['synth'], row['synth'])} · {row['fmt']}{kind}")
        m = self.db.meta(h)
        self._show_stars(m["rating"] if m else 0)
        self.notes.setText((m["notes"] if m else "") or "")
        self._fill_tags()
        self._fill_crates()
        self.prev_list.clear()
        for p in self.db.previews(h):
            it = QListWidgetItem(f"{p['kind'].replace('pack ', '').title()} — {os.path.basename(p['path'])}")
            it.setData(Qt.UserRole, p["path"]); self.prev_list.addItem(it)
        if self.prev_list.count():
            self.prev_list.setCurrentRow(0)
        self.play_btn.setEnabled(self.prev_list.count() > 0)
        lines = []
        for o in self.db.origins(row["id"]):
            pos = ""
            if o["bank"]:
                b = o["bank"]
                pos = f" · bank {b} prog {o['slot']}" if b <= 16 else f" · factory bank {b - 16} prog {o['slot']}"
            lines.append(f"<b>{TYPE_LABEL.get(o['type'], o['type'])}</b> {o['rel']}{pos}")
        self.onsynth.setText(self._onsynth_text(row))
        self.where.setText("<br>".join(lines) or "<i>Its file is gone, but the patch and what you added are kept.</i>")
        if m and m["last_heard"]:
            self.heard.setText(f"Last heard {time.strftime('%d %b %Y %H:%M', time.localtime(m['last_heard']))} · "
                               f"{m['times_heard']} times")
        else:
            self.heard.setText("Never heard")

    def _onsynth_text(self, row):
        parts = []
        if row.get("is_factory"):
            parts.append("<span style='background:#fde7c7; color:#7c2d12; font-weight:700;'>&nbsp;F&nbsp;</span> "
                         "A factory preset — its slot on the synth is locked")
        elif row.get("blank"):
            parts.append("A blank starting sound (init preset) — a slot holding it counts as free")
        slots = self.db.slots_of(row["hash"])
        if slots:
            by = {}
            for s_ in slots:
                by.setdefault(s_["synth"], []).append(s_)
            for syn, ss in by.items():
                when = max(x["read_at"] or 0 for x in ss)
                names = ", ".join(x["place"] or "" for x in ss[:12]) + (f" and {len(ss) - 12} more" if len(ss) > 12 else "")
                parts.append(f"<span style='background:#d8f0e4; color:#065f46; font-weight:600;'>&nbsp;On the "
                             f"{SYNTH_LABEL.get(syn, syn)}&nbsp;</span> {names} "
                             f"<span style='color:#6b7280;'>— as read {time.strftime('%d %b %H:%M', time.localtime(when))}</span>")
        else:
            init = self.db.last_init(row["synth"]) if row["synth"] in SYNTH_LABEL else None
            parts.append("Not on the synth" + (f" — as of the last read" if init else
                                               " as far as the app knows — run Initialize factory presets from the Synths menu"))
        return "<br>".join(parts)

    def _show_stars(self, n):
        for i, b in enumerate(self.star_btns, 1):
            b.setText("★" if n and i <= n else "☆")

    def _fill_tags(self):
        self.tags.clear(); self.starter.clear()
        if not self.row:
            return
        have = set()
        for t in self.db.tags_for(self.row["hash"]):
            have.add(t["name"])
            b = QPushButton(t["name"] + ("  ✕" if t["confirmed"] else ""))
            if t["confirmed"]:
                b.setStyleSheet("background:#1f2937; color:white; border-radius:9px; padding:2px 8px;")
                b.setToolTip("Click to remove")
                b.clicked.connect(lambda _=False, n=t["name"]: self._tag(n, False))
            else:
                b.setStyleSheet("background:#e5e7eb; color:#6b7280; border-radius:9px; padding:2px 8px;")
                b.setToolTip(f"From the {'file' if t['origin'] == 'file' else 'name'} — click to confirm")
                b.clicked.connect(lambda _=False, n=t["name"]: self._tag(n, True))
            self.tags.add(b)
        for t in self.db.all_tags():
            if t["grp"] and t["name"] not in have:
                b = QPushButton("+ " + t["name"]); b.setFlat(True); b.setStyleSheet("color:#2563eb; text-align:left;")
                b.clicked.connect(lambda _=False, n=t["name"]: self._tag(n, True))
                self.starter.add(b)
        comp = QCompleter([t["name"] for t in self.db.all_tags()]); comp.setCaseSensitivity(Qt.CaseInsensitive)
        self.tag_edit.setCompleter(comp)

    def _fill_crates(self):
        while self.crate_box.count():
            w = self.crate_box.takeAt(0).widget()
            if w:
                w.setParent(None); w.deleteLater()
        manual = [c for c in self.db.crates() if not c["query"]]
        if not manual:
            l = QLabel("No crates yet — right-click patches → Add to crate → New crate…")
            l.setStyleSheet("color:#6b7280;"); l.setWordWrap(True); self.crate_box.addWidget(l); return
        mine = set(self.db.crates_of(self.row["hash"])) if self.row else set()
        for c in manual:
            cb = QCheckBox(c["name"]); cb.setChecked(c["name"] in mine); cb.setEnabled(self.row is not None)
            cb.toggled.connect(lambda on, cid=c["id"]: self._crate(cid, on))
            self.crate_box.addWidget(cb)

    # ---------------------------------------------------------------- edits
    def rate(self, n):
        if not self.row:
            return
        self.db.set_rating(self.row["hash"], n)
        self._show_stars(n); self.changed.emit()

    def _tag(self, name, on):
        self.db.set_tag(self.row["hash"], name, on)
        self._fill_tags(); self.changed.emit()

    def _add_tag(self):
        t = self.tag_edit.text().strip().lower()
        if t and self.row:
            for part in t.replace(",", " ").split():
                self.db.set_tag(self.row["hash"], part, True)
            self.tag_edit.clear(); self._fill_tags(); self.changed.emit()

    def _save_notes(self):
        if self.row:
            self.db.set_notes(self.row["hash"], self.notes.text().strip())

    def _crate(self, cid, on):
        (self.db.crate_add if on else self.db.crate_remove)(cid, [self.row["hash"]])
        self.changed.emit()

    def _play(self):
        it = self.prev_list.currentItem() or (self.prev_list.item(0) if self.prev_list.count() else None)
        if it:
            self.play_preview.emit(it.data(Qt.UserRole))

    def first_preview(self):
        it = self.prev_list.currentItem() or (self.prev_list.item(0) if self.prev_list.count() else None)
        return it.data(Qt.UserRole) if it else None
