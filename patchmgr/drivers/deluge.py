"""Synthstrom Deluge driver, reading only (M3): the Community Firmware's file protocol over USB SysEx, bench-tested
19 Nov 2026 on CF 1.3.0 (spec 2.3, Table 2.5; project notes deluge-bench-test/usb-file-protocol.md).

    request  F0 00 21 7B 01 04 <msgId> {json} [00 <packed data>] F7
    reply    F0 00 21 7B 01 05 <msgId> {json} [00 <packed data>] F7

Packing is Sequential's (a byte of high bits per 7 bytes). File blocks are 512 bytes. Names always come from the
Deluge's own listing — Windows changes some (TEST AUDITION.XML arrived as TEST_AUDITION.XML)."""
from __future__ import annotations

import json
import re
import threading

from ..library.formats import pack_ms, read_deluge, unpack_ms

HDR = bytes([0xF0, 0x00, 0x21, 0x7B, 0x01])
JSON_REQ, JSON_REPLY = 0x04, 0x05
BLOCK = 512
INQUIRY = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]
SYNTHS = "/SYNTHS"


class DelugeError(Exception):
    pass


def parse_reply(m: bytes):
    """-> (msgId, dict or None, binary data)."""
    body = bytes(m[7:-1])
    cut = body.find(b"\x00")
    text_b, data = (body, b"") if cut < 0 else (body[:cut], unpack_ms(body[cut + 1:]))
    text = text_b.decode("latin-1")
    for t in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            return m[6], json.loads(t), data
        except ValueError:
            pass
    return m[6], None, data


def is_inquiry_reply(m):
    return len(m) >= 15 and m[1] == 0x7E and m[3] == 0x06 and m[4] == 0x02 and m[5:9] == bytes([0x00, 0x21, 0x7B, 0x01])


class Deluge:
    key = "deluge"
    label = "Deluge"

    def __init__(self, device):
        self.dev = device
        self.lock = threading.Lock()
        self.mid_base, self.mid_n, self.seq = 0, 7, 0
        self.firmware = ""

    def _mid(self):
        self.seq = (self.seq % self.mid_n) + 1
        return self.mid_base + self.seq

    def cmd(self, name, args, data=None, timeout=4.0):
        mid = self._mid() if name != "session" else 0
        text = json.dumps({name: args}, separators=(",", ":")).encode("ascii", "replace")
        msg = HDR + bytes([JSON_REQ, mid]) + text + ((b"\x00" + pack_ms(data)) if data is not None else b"") + b"\xF7"

        def test(m):
            if not m.startswith(HDR) or len(m) < 9 or m[5] not in (JSON_REQ, JSON_REPLY):
                return False
            return m[6] == mid or (name == "session" and b"session" in m[:40])
        r = self.dev.request(msg, test, timeout)
        if r is None:
            raise DelugeError(f"the Deluge did not answer '{name}'")
        _, obj, d = parse_reply(r)
        body = (obj or {}).get("^" + name) or (obj or {}).get(name) or {}
        return body, d

    def connect(self):
        """Device inquiry, then a file session. -> firmware text."""
        with self.lock:
            r = self.dev.request(INQUIRY, is_inquiry_reply, 1.5)
            if r is None:
                raise DelugeError("the Deluge did not answer the device inquiry — is its USB cable on the PC?")
            self.firmware = ".".join(str(x) for x in r[12:15])
            b, _ = self.cmd("session", {"tag": "PatchManager"})
            if "sid" not in b:
                raise DelugeError("the Deluge did not open a file session — it needs the Community Firmware 1.3 or later")
            lo, hi = int(b.get("midMin", 1)), int(b.get("midMax", 7))
            self.mid_base, self.mid_n, self.seq = lo - 1, max(1, hi - lo + 1), 0
            return self.firmware

    def listdir(self, path):
        out, offset = [], 0
        with self.lock:
            while True:
                b, _ = self.cmd("dir", {"path": path, "offset": offset, "lines": 20})
                if b.get("err"):
                    raise DelugeError(f"listing {path} failed (error {b.get('err')})")
                items = b.get("list") or []
                out += items
                if len(items) < 20:
                    return out
                offset += len(items)

    def read(self, path):
        with self.lock:
            b, _ = self.cmd("open", {"path": path, "write": 0})
            if b.get("err") or not b.get("fid"):
                raise DelugeError(f"could not open {path} (error {b.get('err')})")
            fid, size, buf = b["fid"], int(b.get("size", 0)), bytearray()
            try:
                while len(buf) < size:
                    r, d = self.cmd("read", {"fid": fid, "addr": len(buf), "size": BLOCK})
                    if r.get("err") or not d:
                        raise DelugeError(f"reading {path} stopped at byte {len(buf)}")
                    buf.extend(d[:min(len(d), size - len(buf))])
            finally:
                self.cmd("close", {"fid": fid})
            return bytes(buf)

    # ---------------------------------------------------------------- reading presets (M3)
    def preset_files(self, folder=SYNTHS):
        """Every synth preset under SYNTHS, in subfolders too, except the app's own _AUDITION folder.
        -> [(path, size, date|time)]"""
        out = []
        for e in self.listdir(folder):
            name = e.get("name", "")
            path = f"{folder}/{name}"
            if int(e.get("attr", 0)) & 0x10:
                if not name.startswith((".", "_")):
                    out += self.preset_files(path)
            elif name.upper().endswith(".XML"):
                out.append((path, int(e.get("size", 0)), f"{e.get('date', '')}|{e.get('time', '')}"))
        return out

    def read_slots(self, progress=None, cancelled=None, known=None):
        """Every preset in SYNTHS as SlotRead. known: {path: (info, hash, name)} — unchanged files are not re-read."""
        from ..library.factory import SlotRead, deluge_slot
        from ..library.formats import FormatError
        files = self.preset_files()
        out = []
        for i, (path, size, stamp) in enumerate(files):
            if cancelled and cancelled():
                break
            rel = path.lstrip("/")
            info = f"{size}|{stamp}"
            old = (known or {}).get(rel)
            if old and old[0] == info:
                out.append(SlotRead("deluge", 0, deluge_slot(rel), rel, old[2], b"", old[1], info=info))
            else:
                data = self.read(path)
                try:
                    pt = read_deluge(data, path)[0]
                except FormatError:
                    continue                                       # a kit or song saved in SYNTHS: not a synth preset
                out.append(SlotRead("deluge", 0, deluge_slot(rel), rel, pt.name, data, pt.sound_hash, info=info))
            if progress:
                progress(i + 1, len(files), rel)
        return out
