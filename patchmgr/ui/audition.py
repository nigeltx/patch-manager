"""Audition panel (spec 5.1, 6.1, 6.5, 6.6): load to edit buffer, test phrases, KeyLab through."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider,
                               QSpinBox, QVBoxLayout)

from ..library.formats import SYNTH_LABEL
from ..midi.phrases import builtins, default_for, from_midi_file

AUDITION_SYNTHS = ("take5", "sub37", "syntakt")


class AuditionPanel(QFrame):
    load_requested = Signal()
    play_requested = Signal()

    def __init__(self, session, db):
        super().__init__()
        self.session, self.db = session, db
        self.row = None
        self.own = None
        self.setStyleSheet("QFrame#aud{background:palette(alternate-base);border-radius:6px;}")
        self.setObjectName("aud")
        v = QVBoxLayout(self)
        self.load_btn = QPushButton("Load to edit buffer only")
        self.load_btn.setStyleSheet("font-weight:600; padding:6px;")
        self.load_btn.clicked.connect(self.load_requested.emit)
        v.addWidget(self.load_btn)
        self.safe = QLabel(""); self.safe.setWordWrap(True); v.addWidget(self.safe)

        r = QHBoxLayout()
        self.phrase = QComboBox()
        self.phrases = builtins(int(db.setting("bpm", "145")))
        for p in self.phrases:
            self.phrase.addItem(p.label, p.key); self.phrase.setItemData(self.phrase.count() - 1, p.for_, Qt.ToolTipRole)
        self.phrase.addItem("Your own MIDI file…", "file")
        self.phrase.activated.connect(self._phrase_picked)
        r.addWidget(self.phrase, 1)
        self.play_btn = QPushButton("▶ Play  (Enter)"); self.play_btn.clicked.connect(self.play_requested.emit)
        self.stop_btn = QPushButton("■"); self.stop_btn.setToolTip("Stop and send all notes off (Esc)")
        self.stop_btn.clicked.connect(self.stop); self.stop_btn.setMaximumWidth(36)
        r.addWidget(self.play_btn); r.addWidget(self.stop_btn); v.addLayout(r)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Velocity"))
        self.vel = QSlider(Qt.Horizontal); self.vel.setRange(1, 127); self.vel.setValue(int(db.setting("velocity", "100")))
        self.vel.valueChanged.connect(lambda x: (self.vel_lbl.setText(str(x)), db.set_setting("velocity", x)))
        self.vel_lbl = QLabel(str(self.vel.value())); self.vel_lbl.setMinimumWidth(24)
        r2.addWidget(self.vel, 1); r2.addWidget(self.vel_lbl)
        v.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Octave"))
        self.octave = QSpinBox(); self.octave.setRange(-3, 3); r3.addWidget(self.octave)
        r3.addWidget(QLabel("  Tempo"))
        self.bpm = QSpinBox(); self.bpm.setRange(40, 220); self.bpm.setValue(int(db.setting("bpm", "145")))
        self.bpm.setSuffix(" BPM"); self.bpm.valueChanged.connect(self._bpm); r3.addWidget(self.bpm)
        r3.addStretch(1); v.addLayout(r3)

        r4 = QHBoxLayout()
        self.transport_btn = QPushButton("▶ Start transport")
        self.transport_btn.setCheckable(True)
        self.transport_btn.setToolTip("Sends MIDI Start and clock at the tempo shown, so the patch's own sequencer or "
                                      "arpeggiator runs (the Take 5 must be set to follow MIDI clock). Click again to stop.")
        self.transport_btn.toggled.connect(self._transport)
        r4.addWidget(self.transport_btn)
        self.restart = QCheckBox("Restart it on each patch")
        self.restart.setToolTip("Every new patch starts its sequence from the top")
        self.restart.setChecked(db.setting("restart_transport", "1") == "1")
        self.restart.toggled.connect(self._restart)
        r4.addWidget(self.restart); r4.addStretch(1)
        v.addLayout(r4)
        session.restart_transport = self.restart.isChecked()
        session.transport_changed.connect(self._transport_state)

        self.through = QCheckBox("Play it from the KeyLab")
        self.through.setToolTip("The KeyLab's keys, wheels, aftertouch and sustain go to the synth being auditioned, "
                                "on its channel")
        self.through.toggled.connect(self._through)
        v.addWidget(self.through)
        self.auto = QCheckBox("Load as I move through the list (↑ ↓)")
        self.auto.setChecked(db.setting("load_on_select", "1") == "1")
        self.auto.toggled.connect(self._auto)
        v.addWidget(self.auto)
        self.autoplay = QCheckBox("…and play the test phrase too")
        self.autoplay.setChecked(db.setting("play_on_select", "0") == "1")
        self.autoplay.toggled.connect(self._autoplay)
        v.addWidget(self.autoplay)
        self.autoplay.setEnabled(self.auto.isChecked())
        self.show_row(None)

    # ---------------------------------------------------------------- state
    def show_row(self, row):
        self.row = row
        syn = row["synth"] if row else None
        can = bool(row) and syn in AUDITION_SYNTHS and row["kind"] == "sound"
        ready = can and self.session.ready(syn)
        self.load_btn.setEnabled(ready)
        self.play_btn.setEnabled(ready)
        self.transport_btn.setEnabled(ready or self.session.transport.running())
        if not row:
            self.safe.setText("")
        elif not can:
            why = {"deluge": "Deluge audition over USB arrives with milestone M4.",
                   "syntakt": "Syntakt projects go to the Syntakt with Elektron Transfer; only single sounds are auditioned."}
            self.safe.setText(why.get(syn, "This item is not a single sound, so it cannot be auditioned."))
            self.safe.setStyleSheet("color:#8b919a;")
        elif not ready:
            st = self.session.state.get(syn, "absent")
            self.safe.setText(f"The {SYNTH_LABEL[syn]} is not connected to the PC." if st == "absent" else
                              f"The {SYNTH_LABEL[syn]} is being checked…" if st == "checking" else
                              f"The {SYNTH_LABEL[syn]} failed its check — see the session details (click its pill in the toolbar).")
            self.safe.setStyleSheet("color:#c2410c;")
        else:
            if syn == "syntakt":
                drv = self.session.drivers.get("syntakt")
                where = drv.scratch_label() if drv else "H128"
                self.safe.setText(f"Goes to scratch slot {where} only. Leave the Syntakt's SOUND BROWSER on {where} "
                                  "and press FUNC + YES there to hear it (Play here sends nothing to the Syntakt).")
            else:
                self.safe.setText("Saved programs on the synth are untouched: audition only fills the edit buffer.")
            self.safe.setStyleSheet("color:#16a34a;")
            tags = [t["name"] for t in self.db.tags_for(row["hash"])]
            key = default_for(tags)
            if self.phrase.currentData() != "file" or self.own is None:
                i = self.phrase.findData(key)
                if i >= 0:
                    self.phrase.setCurrentIndex(i)
            self.session.set_target(syn)

    def _phrase_picked(self, i):
        if self.phrase.itemData(i) == "file":
            f, _ = QFileDialog.getOpenFileName(self, "A short MIDI file to play", "", "MIDI files (*.mid *.midi)")
            if not f:
                self.phrase.setCurrentIndex(0); return
            try:
                self.own = from_midi_file(f)
            except Exception as e:                   # noqa: BLE001
                self.safe.setText(f"Could not read that MIDI file: {e}"); self.phrase.setCurrentIndex(0); return
            self.phrase.setItemText(i, f"Your own: {self.own.label}")

    def _auto(self, on):
        self.db.set_setting("load_on_select", "1" if on else "0")
        self.autoplay.setEnabled(on)

    def _autoplay(self, on):
        self.db.set_setting("play_on_select", "1" if on else "0")

    def _restart(self, on):
        self.db.set_setting("restart_transport", "1" if on else "0")
        self.session.restart_transport = on

    def _transport(self, on):
        if on:
            syn = self.row["synth"] if self.row else None
            if not syn or not self.session.start_transport(syn, self.bpm.value()):
                self.transport_btn.blockSignals(True); self.transport_btn.setChecked(False)
                self.transport_btn.blockSignals(False)
                self.safe.setText("Select a patch on a connected synth first, then start the transport.")
                return
        else:
            self.session.stop_transport()

    def _transport_state(self, running):
        self.transport_btn.blockSignals(True)
        self.transport_btn.setChecked(running)
        self.transport_btn.setText("■ Stop transport" if running else "▶ Start transport")
        self.transport_btn.setStyleSheet("background:#d1fae5; color:#065f46; font-weight:600;" if running else "")
        self.transport_btn.blockSignals(False)

    def _bpm(self, x):
        self.db.set_setting("bpm", x)
        self.session.transport.set_bpm(x)
        cur = self.phrase.currentData()
        self.phrases = builtins(x)
        self.phrase.setCurrentIndex(max(0, self.phrase.findData(cur)))

    def _through(self, on):
        self.session.set_through(on)

    def current_phrase(self):
        key = self.phrase.currentData()
        if key == "file":
            return self.own
        return next((p for p in self.phrases if p.key == key), self.phrases[0])

    def play(self):
        if not self.row or not self.session.ready(self.row["synth"]):
            return
        if self.row["synth"] == "syntakt":                 # heard with FUNC + YES in the Syntakt's browser
            return
        ph = self.current_phrase()
        if ph is None:
            return
        self.session.play(self.row["synth"], ph, self.vel.value(), self.octave.value(), self.bpm.value())

    def stop(self):
        self.session.stop()
