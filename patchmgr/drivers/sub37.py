"""Moog Sub 37 driver, audition only (spec 2.2, 6.2): every library file is a BUFFER SND dump, which plays and
stores nothing. A stored-preset dump (type 02) is turned into a buffer dump before sending; the checksum covers
only the data, so changing the type byte is safe (bench test 13)."""
from __future__ import annotations

from ..library.formats import S37_ALL, S37_ALL_LEN, S37_BUFFER, S37_ID, S37_LEN, S37_PRESET, place, read_sub37

INQUIRY = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]


def is_inquiry_reply(m):
    return len(m) >= 13 and m[1] == 0x7E and m[3] == 0x06 and m[4] == 0x02 and m[5] == 0x04 and m[7] == 0x0C


def buffer_from(data: bytes) -> bytes:
    if not data.startswith(S37_ID) or len(data) != S37_LEN or data[3] not in (S37_BUFFER, S37_PRESET):
        raise ValueError("not a single Sub 37 preset")
    return data[:3] + bytes([S37_BUFFER]) + data[4:]


class Sub37:
    key = "sub37"
    label = "Sub 37"

    def __init__(self, device, channel=15):
        self.dev = device
        self.channel = channel
        self.loaded = None
        self.pre = None

    def preflight(self):
        from .take5 import Preflight
        p = Preflight(False)
        r = self.dev.request(INQUIRY, is_inquiry_reply, 1.5)
        if r is None:
            p.lines.append(("bad", "The Sub 37 did not answer the device inquiry. Check MIDI › 2.1 IN PORTS and "
                                   "2.2 OUT PORTS are USB."))
            return p
        p.os_version = ".".join(str(x) for x in r[11:14])
        p.lines.append(("ok", f"Sub 37 found — firmware {p.os_version}"))
        p.lines.append(("warn", f"Test phrases go out on channel {self.channel}: MIDI › 1.1 IN CHANNEL must match "
                                "(SysEx audition works on any channel)"))
        p.ok = True
        p.channel = self.channel
        self.pre = p
        return p

    def load(self, data: bytes, h: str, name: str):
        self.dev.send(buffer_from(data))
        self.loaded = (h, name)

    def put_back(self):
        return False

    # ---------------------------------------------------------------- reading slots (M3)
    def wait_all_presets(self, timeout=600, progress=None, cancelled=None):
        """Wait for you to press SEND on ALL PRESETS (SYSEX page). -> the 203,800-byte dump, or None."""
        def test(m):
            return m.startswith(S37_ID) and len(m) == S37_ALL_LEN and m[3] == S37_ALL
        return self.dev.wait_for(test, timeout, progress, cancelled)


def slots_from_all(data: bytes):
    """ALL PRESETS → 256 SlotRead (bench test 18)."""
    from ..library.factory import SlotRead
    out = []
    for p in read_sub37(data, "ALL PRESETS.syx"):
        if p.kind == "sound":
            out.append(SlotRead("sub37", p.bank, p.slot, place("sub37", p.bank, p.slot), p.name, p.data, p.sound_hash,
                                blank=p.blank))
    return out
