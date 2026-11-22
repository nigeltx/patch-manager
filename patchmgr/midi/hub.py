"""MIDI plumbing (spec 3.3, 8.5): ports by name, SysEx joined back together, paced sending, requests that wait
with a timeout, and port watching so the app knows when a patch session starts and ends."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

try:
    import rtmidi
    RTMIDI_ERROR = ""
except Exception as e:                       # noqa: BLE001
    rtmidi = None
    RTMIDI_ERROR = f"python-rtmidi is not available ({e})"

# Port names Windows gives the studio's devices; matched case-insensitively as substrings.
DEVICES = {
    "take5": ("take 5",),
    "sub37": ("moog sub 37", "subsequent 37", "sub 37"),
    "deluge": ("deluge",),
    "syntakt": ("elektron syntakt", "syntakt"),
    "keylab": ("keylab 61 mk3 midi", "keylab 61 mk3", "keylab"),
}
NOT_IT = {"keylab": ("daw",), "deluge": ("midiin2", "midiin3", "midiout2", "midiout3")}


class Reassembler:
    """Joins SysEx that rtmidi hands over in 1,024-byte pieces back into complete F0…F7 messages."""

    def __init__(self):
        self.pending = bytearray()

    def feed(self, chunk: bytes):
        out, start = [], 0
        while True:
            if not self.pending:
                s = chunk.find(b"\xF0", start)
                if s < 0:
                    return out
                start = s
            e = chunk.find(b"\xF7", start)
            if e < 0:
                self.pending += chunk[start:]
                return out
            s2 = chunk.rfind(b"\xF0", start + (0 if self.pending else 1), e)
            if s2 >= 0:
                self.pending = bytearray(); start = s2
                continue
            self.pending += chunk[start:e + 1]
            out.append(bytes(self.pending)); self.pending = bytearray()
            start = e + 1


def match(name: str, device: str) -> bool:
    low = name.lower()
    return any(k in low for k in DEVICES[device]) and not any(k in low for k in NOT_IT.get(device, ()))


def list_ports():
    if rtmidi is None:
        return [], []
    i, o = rtmidi.MidiIn(), rtmidi.MidiOut()
    try:
        return i.get_ports(), o.get_ports()
    finally:
        i.delete(); o.delete()


@dataclass
class Waiter:
    test: object
    event: threading.Event
    reply: bytes | None = None


class Device:
    """One synth or controller: an output and an input opened by name."""

    def __init__(self, key, in_name, out_name, on_short=None, on_sysex=None):
        self.key, self.in_name, self.out_name = key, in_name, out_name
        self.on_short, self.on_sysex = on_short, on_sysex
        self.lock = threading.Lock()
        self.waiters: list[Waiter] = []
        self.re = Reassembler()
        self.inp = self.out = None
        if out_name:
            self.out = rtmidi.MidiOut()
            self.out.open_port(self.out.get_ports().index(out_name))
        if in_name:
            self.inp = rtmidi.MidiIn()
            self.inp.ignore_types(sysex=False, timing=True, active_sense=True)
            self.inp.open_port(self.inp.get_ports().index(in_name))
            self.inp.set_callback(self._cb)

    def _cb(self, ev, _=None):
        m = bytes(ev[0])
        if not m:
            return
        if m[0] == 0xF0 or m[0] < 0x80 or self.re.pending:
            for x in self.re.feed(m):
                self._sysex(x)
        elif self.on_short:
            self.on_short(self, m)

    def _sysex(self, m):
        for w in list(self.waiters):
            try:
                ok = w.test(m)
            except Exception:                # noqa: BLE001
                ok = False
            if ok:
                w.reply = m; self.waiters.remove(w); w.event.set()
                return
        if self.on_sysex:
            self.on_sysex(self, m)

    def send(self, msg):
        if self.out is None:
            raise RuntimeError(f"{self.key} has no MIDI output")
        with self.lock:
            self.out.send_message(list(msg))

    def request(self, msg, test, timeout=2.0):
        """Send msg and wait for the first SysEx reply for which test(reply) is true; None after timeout."""
        w = Waiter(test, threading.Event())
        self.waiters.append(w)
        self.send(msg)
        w.event.wait(timeout)
        if w in self.waiters:
            self.waiters.remove(w)
        return w.reply

    def wait_for(self, test, timeout, progress=None, cancelled=None):
        """Wait, without sending anything, for a SysEx message the synth sends by itself (a dump started on its
        panel). progress(bytes so far) is called about twice a second while a long message arrives."""
        w = Waiter(test, threading.Event())
        self.waiters.append(w)
        end = time.time() + timeout
        try:
            while not w.event.wait(0.5):
                if progress:
                    progress(len(self.re.pending))
                if time.time() > end or (cancelled and cancelled()):
                    break
        finally:
            if w in self.waiters:
                self.waiters.remove(w)
        return w.reply

    def close(self):
        for p in (self.inp, self.out):
            if p is not None:
                try:
                    if p is self.inp:
                        p.cancel_callback()
                    p.close_port(); p.delete()
                except Exception:            # noqa: BLE001
                    pass
        self.inp = self.out = None


class Hub:
    """Owns every open Device. find() opens a device by its name pattern; watch() polls for ports coming and going."""

    def __init__(self):
        self.devices: dict[str, Device] = {}
        self.listeners = []          # callables(event, key)

    def available(self, key):
        ins, outs = list_ports()
        i = next((n for n in ins if match(n, key)), None)
        o = next((n for n in outs if match(n, key)), None)
        return i, o

    def open(self, key, on_short=None, on_sysex=None, need_out=True):
        if key in self.devices:
            return self.devices[key]
        i, o = self.available(key)
        if i is None and o is None or (need_out and o is None):
            return None
        d = Device(key, i, o if need_out else None, on_short, on_sysex)
        self.devices[key] = d
        return d

    def close(self, key):
        d = self.devices.pop(key, None)
        if d:
            d.close()

    def close_all(self):
        for k in list(self.devices):
            self.close(k)

    def present(self) -> set[str]:
        ins, outs = list_ports()
        names = ins + outs
        return {k for k in DEVICES if any(match(n, k) for n in names)}
