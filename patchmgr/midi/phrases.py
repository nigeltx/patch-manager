"""Test phrases (spec 6.5, Table 6.2) and the player that sends them, with MIDI clock when a phrase needs it."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class Phrase:
    key: str
    label: str
    events: list            # [(seconds, 'on'|'off', note)], times from the start
    length: float           # seconds
    clock: bool = False
    bpm: float | None = None
    for_: str = ""


def _single():
    return [(0.0, "on", 60), (2.0, "off", 60)], 3.5


def _low_pulse():
    ev = []
    for i in range(4):
        ev += [(i * 1.0, "on", 36), (i * 1.0 + 0.45, "off", 36)]
    return ev, 4.5


def _rolling(bpm=145):
    s = 60.0 / bpm / 4               # a sixteenth
    ev = []
    for beat in range(8):            # 2 bars of 4/4
        for k in (1, 2, 3):          # off-beat sixteenths: the classic psytrance roll
            t = (beat * 4 + k) * s
            ev += [(t, "on", 40), (t + s * 0.8, "off", 40)]
    return ev, 8 * 4 * s + 1.5


def _pad():
    notes = (60, 64, 67, 71)
    return [(0.0, "on", n) for n in notes] + [(4.0, "off", n) for n in notes], 6.5


def _stab():
    ev = []
    for i in range(4):
        t = i * 0.5
        ev += [(t, "on", 48), (t, "on", 55), (t + 0.25, "off", 48), (t + 0.25, "off", 55)]
    return ev, 3.0


def _arp():
    return [(0.0, "on", 60), (8.0, "off", 60)], 9.0


def builtins(bpm=145):
    out = []
    for key, label, fn, clock, for_ in (
            ("single", "Single note", _single, False, "Anything; hear the release tail"),
            ("pulse", "Low pulse", _low_pulse, False, "Bass"),
            ("rolling", "Rolling bass", lambda: _rolling(bpm), False, "Psytrance bass"),
            ("pad", "Pad chord", _pad, False, "Pads (four notes leaves one Take 5 voice spare)"),
            ("stab", "Two-note stab", _stab, False, "Sub 37 in DUO MODE"),
            ("arp", "Hold for arp", _arp, True, "Arpeggiator and sequencer patches; sends MIDI clock")):
        ev, length = fn()
        out.append(Phrase(key, label, ev, length, clock, bpm if (clock or key == "rolling") else None, for_))
    return out


ROLE_PHRASE = {"bass": "pulse", "pad": "pad", "seq": "arp", "keys": "pad", "lead": "single", "pluck": "single",
               "fx": "single", "drum": "single"}


def default_for(tags: list[str]) -> str:
    for t in tags:
        if t in ROLE_PHRASE:
            return ROLE_PHRASE[t]
    return "single"


def from_midi_file(path: str, max_seconds=20.0) -> Phrase:
    """Your own phrase: notes from any channel of a short MIDI file, played on the synth's channel."""
    import mido
    mf = mido.MidiFile(path)
    t, ev = 0.0, []
    for msg in mf:                               # iterating a MidiFile yields delta times in seconds
        t += msg.time
        if t > max_seconds:
            break
        if msg.type == "note_on" and msg.velocity > 0:
            ev.append((t, "on", msg.note))
        elif msg.type in ("note_off", "note_on"):
            ev.append((t, "off", msg.note))
    if not ev:
        raise ValueError("no notes in that MIDI file")
    return Phrase("file", path.replace("\\", "/").split("/")[-1], ev, min(t, max_seconds) + 1.0)


class PhrasePlayer:
    """Plays one phrase at a time on a Device. stop() — or a new play() — ends it with all notes off."""

    def __init__(self):
        self.thread = None
        self.stop_flag = threading.Event()
        self.device = None
        self.channel = 0
        self.on_done = None

    def play(self, device, channel, phrase: Phrase, velocity=100, octave=0, bpm=145, on_done=None, own_clock=True):
        self.stop()
        if not own_clock and phrase.clock:
            import dataclasses
            phrase = dataclasses.replace(phrase, clock=False)
        self.device, self.channel, self.on_done = device, channel, on_done
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(phrase, velocity, octave, bpm, self.stop_flag), daemon=True)
        self.thread.start()

    def _run(self, ph, vel, octave, bpm, stop):
        dev, ch = self.device, self.channel
        sounding = set()
        clock_thread = None
        if ph.clock:
            clock_thread = threading.Thread(target=self._clock, args=(dev, bpm, stop), daemon=True)
            dev.send([0xFA]); clock_thread.start()
            time.sleep(0.05)
        t0 = time.perf_counter()
        try:
            for t, kind, note in sorted(ph.events, key=lambda e: (e[0], e[1] == "on")):
                n = max(0, min(127, note + 12 * octave))
                while not stop.is_set():
                    dt = t - (time.perf_counter() - t0)
                    if dt <= 0:
                        break
                    stop.wait(min(dt, 0.02))
                if stop.is_set():
                    break
                if kind == "on":
                    dev.send([0x90 | ch, n, vel]); sounding.add(n)
                else:
                    dev.send([0x80 | ch, n, 0]); sounding.discard(n)
            while not stop.is_set() and time.perf_counter() - t0 < ph.length:
                stop.wait(0.05)
        finally:
            for n in sounding:
                dev.send([0x80 | ch, n, 0])
            if clock_thread:
                stop.set(); clock_thread.join(0.5); dev.send([0xFC])
            all_notes_off(dev, ch)
            cb = self.on_done
            if cb and self.thread is threading.current_thread():
                cb()

    @staticmethod
    def _clock(dev, bpm, stop):
        tick = 60.0 / bpm / 24
        nxt = time.perf_counter()
        while not stop.is_set():
            dev.send([0xF8])
            nxt += tick
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)

    def stop(self):
        th = self.thread
        if th and th.is_alive():
            self.stop_flag.set()
            th.join(1.0)
        self.thread = None

    def playing(self):
        return bool(self.thread and self.thread.is_alive())


def all_notes_off(dev, ch):
    try:
        dev.send([0xB0 | ch, 123, 0])       # all notes off
        dev.send([0xB0 | ch, 64, 0])        # sustain up
    except Exception:                        # noqa: BLE001
        pass


class Transport:
    """MIDI Start + clock (24 ppq) until stopped, so a patch's own sequencer or arpeggiator runs (spec 6.5)."""

    def __init__(self):
        self.device = None
        self.stop_flag = threading.Event()
        self.thread = None
        self.bpm = 120.0

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, device, bpm):
        self.stop()
        self.device, self.bpm = device, float(bpm)
        self.stop_flag = threading.Event()
        device.send([0xFA])
        self.thread = threading.Thread(target=self._run, args=(device, self.stop_flag), daemon=True)
        self.thread.start()

    def _run(self, dev, stop):
        nxt = time.perf_counter()
        while not stop.is_set():
            try:
                dev.send([0xF8])
            except Exception:                    # noqa: BLE001 — port gone
                return
            nxt += 60.0 / self.bpm / 24
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)
            elif d < -0.1:
                nxt = time.perf_counter()        # fell behind (PC busy): don't rush to catch up

    def set_bpm(self, bpm):
        self.bpm = float(bpm)

    def restart(self):
        """Stop and start again from the top — used when a new patch is loaded."""
        if self.running():
            dev = self.device
            self.stop()
            self.start(dev, self.bpm)

    def stop(self):
        th, dev = self.thread, self.device
        if th and th.is_alive():
            self.stop_flag.set(); th.join(0.5)
            try:
                dev.send([0xFC])
            except Exception:                    # noqa: BLE001
                pass
        self.thread = None
