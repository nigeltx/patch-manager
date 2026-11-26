"""The patch session (spec 3.1, 6): watches for the synths' USB ports, runs the preflight check, loads patches into
edit buffers, plays test phrases and passes the KeyLab through to the synth being auditioned."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, Signal

from ..drivers.deluge import Deluge
from ..drivers.sub37 import Sub37
from ..drivers.syntakt import Syntakt
from ..drivers.take5 import Take5
from ..library import layout
from ..midi import hub as midihub
from ..midi.phrases import PhrasePlayer, Transport, all_notes_off

DRIVERS = {"take5": Take5, "sub37": Sub37, "syntakt": Syntakt, "deluge": Deluge}
SYNTHS = ("take5", "sub37", "syntakt", "deluge")
THROUGH_TYPES = (0x80, 0x90, 0xA0, 0xB0, 0xD0, 0xE0)


class Session(QObject):
    devices_changed = Signal(object)            # {key: state}  state: 'absent' | 'checking' | 'ready' | 'problem'
    preflight_done = Signal(str, object)        # key, Preflight
    loaded = Signal(str, str, str)              # key, hash, name
    transport_changed = Signal(bool)
    verified = Signal(str, str, bool, str)      # key, hash, matched, what the synth reports
    message = Signal(str)
    phrase_finished = Signal()

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.hub = midihub.Hub()
        self.drivers = {}
        self.state = {k: "absent" for k in SYNTHS + ("keylab",)}
        self.pending = {}
        self.bg_lock = threading.Lock()                         # synth -> latest load request (Syntakt loads run in the background)
        self.player = PhrasePlayer()
        self.transport = Transport()
        self.restart_transport = False
        self.through_on = False
        self.target = None                        # driver the KeyLab plays
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(2000)
        QTimer.singleShot(200, self.poll)

    # ---------------------------------------------------------------- ports coming and going
    def poll(self):
        if midihub.rtmidi is None:
            self.message.emit(midihub.RTMIDI_ERROR); self.timer.stop(); return
        try:
            present = self.hub.present()
        except Exception as e:                       # noqa: BLE001
            self.message.emit(f"MIDI ports could not be listed: {e}"); return
        changed = False
        for key in SYNTHS:
            if key in present and self.state[key] == "absent":
                self._start(key); changed = True
            elif key not in present and self.state[key] != "absent":
                self._stop(key); changed = True
        if "keylab" in present and self.state["keylab"] == "absent":
            self.state["keylab"] = "ready"; changed = True
            if self.through_on:
                self._open_keylab()
        elif "keylab" not in present and self.state["keylab"] != "absent":
            self.state["keylab"] = "absent"; self.hub.close("keylab"); changed = True
        if changed:
            self.devices_changed.emit(dict(self.state))

    def _start(self, key):
        if key == "deluge":                        # its port pair is found by asking (Deluge.find)
            drv = Deluge(None, hub=self.hub, log=self.log)
            self.drivers[key] = drv
            self.state[key] = "checking"
            threading.Thread(target=self._preflight, args=(key, drv), daemon=True).start()
            return
        try:
            dev = self.hub.open(key)
        except Exception as e:                       # noqa: BLE001
            self.state[key] = "problem"
            self.message.emit(f"Could not open the {key} ports — is another program (an editor, Ableton, a bench "
                              f"test) using them? ({e})")
            return
        if dev is None:
            return
        ch = int(self.db.setting(f"channel_{key}", {"take5": "16", "sub37": "15", "syntakt": "14"}[key]))
        if key == "take5":
            drv = Take5(dev)
        elif key == "sub37":
            drv = Sub37(dev, ch)
        else:
            import os
            root = self.db.setting("library_root") or ""
            scratch = self.db.setting("syntakt_scratch", "H128")
            drv = Syntakt(dev, ch, (scratch[0].upper(), int(scratch[1:])),
                          backup_dir=os.path.join(layout.backups_dir(root, "syntakt"), "Audition slot") if root else None,
                          owned_name=self.db.setting("syntakt_owned"))
            drv.on_owned = lambda name: self.db.set_setting("syntakt_owned", name)
        self.drivers[key] = drv
        self.state[key] = "checking"
        threading.Thread(target=self._preflight, args=(key, drv), daemon=True).start()

    def _preflight(self, key, drv):
        try:
            p = drv.preflight()
        except Exception as e:                       # noqa: BLE001
            from ..drivers.take5 import Preflight
            p = Preflight(False, [("bad", f"Preflight failed: {e}")])
        self.state[key] = "ready" if p.ok else "problem"
        self.log(f"PREFLIGHT {key}: " + " | ".join(t for _, t in p.lines))
        self.preflight_done.emit(key, p)
        self.devices_changed.emit(dict(self.state))

    def _stop(self, key):
        if self.target is self.drivers.get(key):
            self.player.stop()
        d = self.drivers.get(key)
        if d and self.transport.device is d.dev:
            self.transport.stop_flag.set(); self.transport.thread = None
            self.transport_changed.emit(False)
        self.drivers.pop(key, None)
        self.hub.close(key)
        self.state[key] = "absent"

    def recheck(self, key):
        drv = self.drivers.get(key)
        if drv:
            self.state[key] = "checking"; self.devices_changed.emit(dict(self.state))
            threading.Thread(target=self._preflight, args=(key, drv), daemon=True).start()

    # ---------------------------------------------------------------- audition
    def ready(self, synth):
        return self.state.get(synth) == "ready" and synth in self.drivers

    def load(self, synth, h, name, data):
        drv = self.drivers.get(synth)
        if not drv or self.state.get(synth) != "ready":
            raise RuntimeError(f"The {synth} is not connected")
        if self.player.playing() and self.target is drv:
            self.player.stop()
        if synth == "syntakt":
            # Writing the scratch slot takes a few round trips; do it off the UI thread, and skip any request
            # overtaken by a newer one while it waited (arrowing quickly through a pack).
            self.pending[synth] = h
            threading.Thread(target=self._load_bg, args=(synth, drv, h, name, data), daemon=True).start()
            self.target = drv
            return
        drv.load(data, h, name)
        if self.transport.running():
            if self.transport.device is not drv.dev:
                self.transport.start(drv.dev, self.transport.bpm)     # follow the synth being auditioned
            elif self.restart_transport:
                self.transport.restart()
        self.target = drv
        self.db.mark_heard(h)
        self.log(f"LOAD {synth} '{name}' {h[:10]} ({len(data)} bytes in the library)")
        self.loaded.emit(synth, h, name)
        if hasattr(drv, "read_edit"):
            threading.Thread(target=self._verify, args=(synth, drv, h, name, data), daemon=True).start()

    def _load_bg(self, synth, drv, h, name, data):
        with self.bg_lock:                           # one Syntakt load at a time, newest wins
            if self.pending.get(synth) != h:
                return
            try:
                drv.load(data, h, name)
            except Exception as e:                   # noqa: BLE001
                self.log(f"LOAD {synth} '{name}' FAILED: {e}")
                self.message.emit(f"Could not send '{name}' to the Syntakt: {e}")
                return
            self.db.mark_heard(h)
            self.log(f"LOAD {synth} '{name}' {h[:10]} into scratch slot {drv.bank}{drv.slot}")
            self.loaded.emit(synth, h, name)
            if self.pending.get(synth) == h:
                self._verify(synth, drv, h, name, data)

    def _verify(self, synth, drv, h, name, data):
        """Read the edit buffer back after a load, so a sound that did not arrive is reported, not just assumed."""
        import time as _t
        _t.sleep(0.15)
        try:
            got_name, same = drv.read_edit(data)
        except Exception as e:                       # noqa: BLE001
            got_name, same = f"(error: {e})", False
        if drv.loaded and drv.loaded[0] != h:
            return                                   # another patch was loaded meanwhile
        self.log(f"CHECK {synth} '{name}': synth reports '{got_name}', {'same' if same else 'DIFFERENT'}")
        self.verified.emit(synth, h, same, got_name)

    def log(self, text):
        import os
        import time as _t
        from ..library.db import default_path
        try:
            with open(os.path.join(os.path.dirname(default_path()), "session log.txt"), "a", encoding="utf-8") as f:
                f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except OSError:
            pass

    def put_back(self, synth):
        drv = self.drivers.get(synth)
        return bool(drv and drv.put_back())

    def play(self, synth, phrase, velocity, octave, bpm):
        drv = self.drivers.get(synth)
        self.log(f"PLAY {synth} phrase '{phrase.label}' on channel {drv.channel if drv else '?'}"
                 f" — edit buffer holds '{drv.loaded[1] if drv and drv.loaded else 'nothing from the app'}'")
        if not drv or self.state.get(synth) != "ready":
            raise RuntimeError("not connected")
        self.target = drv
        self.player.play(drv.dev, drv.channel - 1, phrase, velocity, octave, bpm,
                         on_done=lambda: self.phrase_finished.emit(), own_clock=not self.transport.running())

    def start_transport(self, synth, bpm):
        drv = self.drivers.get(synth)
        if not drv or self.state.get(synth) != "ready":
            return False
        self.transport.start(drv.dev, bpm)
        self.log(f"TRANSPORT start on {synth} at {bpm} BPM")
        self.transport_changed.emit(True)
        return True

    def stop_transport(self):
        if self.transport.running():
            self.transport.stop()
            self.log("TRANSPORT stop")
        self.transport_changed.emit(False)

    def stop(self):
        self.player.stop()
        for d in self.drivers.values():
            if d.dev is not None and getattr(d, "channel", None):
                all_notes_off(d.dev, d.channel - 1)

    def set_channel(self, synth, ch):
        self.db.set_setting(f"channel_{synth}", ch)
        drv = self.drivers.get(synth)
        if drv:
            drv.channel = ch

    def set_target(self, synth):
        drv = self.drivers.get(synth)
        if drv:
            self.target = drv

    # ---------------------------------------------------------------- KeyLab through
    def set_through(self, on):
        self.through_on = on
        if on:
            self._open_keylab()
        else:
            self.hub.close("keylab")
            if self.target:
                all_notes_off(self.target.dev, self.target.channel - 1)

    def _open_keylab(self):
        if "keylab" in self.hub.devices:
            return True
        try:
            d = self.hub.open("keylab", on_short=self._through, need_out=False)
        except Exception as e:                       # noqa: BLE001
            self.message.emit(f"Could not open the KeyLab — close any other program using it ({e})")
            return False
        if d is None:
            self.message.emit("The KeyLab's MIDI port is not on the PC"); return False
        return True

    def _through(self, dev, m):
        t = self.target
        if t is None or not m or (m[0] & 0xF0) not in THROUGH_TYPES:
            return
        try:
            t.dev.send(bytes([(m[0] & 0xF0) | (t.channel - 1)]) + m[1:])
        except Exception:                            # noqa: BLE001
            pass

    def close(self):
        self.timer.stop()
        self.transport.stop()
        self.stop()
        self.hub.close_all()
