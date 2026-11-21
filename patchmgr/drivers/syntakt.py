"""Elektron Syntakt driver — audition through one scratch slot in the sound pool (bench-tested 19 Nov 2026).

Uses Elektron's transfer protocol as implemented by the open-source Elektroid. A .stsnd is a zip whose payload file
is written unchanged to /soundbanks/<bank>/<slot>; the Syntakt's sound browser shows it at once, even while open.
The Syntakt cannot be told to load a sound onto a track (Syntakt Track Bench Test 1.3: sound dumps sent to it are
ignored), so audition is: leave the Syntakt's sound browser on the scratch slot (H128), pick a sound in the app (it is
written to H128), and press FUNC + YES on the Syntakt to preview it. Nothing is loaded onto a track and the app sends
no note. Only the scratch slot is ever written; a sound the app did not put there is saved first."""
from __future__ import annotations

import io
import json
import os
import struct
import threading
import time
import zipfile
import zlib

HDR = bytes([0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00])
BLOCK = 0x2000
SYNTAKT_ID = 30


def encode(msg: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(msg), 7):
        chunk = msg[i:i + 7]
        acc = 0
        for k in range(7):
            acc <<= 1
            if k < len(chunk) and chunk[k] & 0x80:
                acc |= 1
        out.append(acc)
        out.extend(b & 0x7F for b in chunk)
    return bytes(out)


def decode(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 8):
        hi, shift = data[i], 0x40
        for b in data[i + 1:i + 8]:
            out.append(b | (0x80 if hi & shift else 0))
            shift >>= 1
    return bytes(out)


def payload_of(stsnd: bytes):
    """-> (name, payload bytes) from a .stsnd file's bytes."""
    z = zipfile.ZipFile(io.BytesIO(stsnd))
    man = json.loads(z.read("manifest.json").decode("utf-8-sig"))
    if man.get("FileType") != "Sound":
        raise ValueError(f"this is a Syntakt {man.get('FileType', 'file').lower()}, not a sound")
    return man["Payload"], z.read(man["Payload"])


def parse_list(r: bytes):
    pos, out = 18, []
    while pos < len(r):
        end = r.find(b"\0", pos)
        if end < 0:
            break
        name = r[pos:end].decode("cp1252", "replace"); pos = end + 1
        has_children, typ = r[pos], r[pos + 1]; pos += 2
        if typ == 1:
            pos += 4; out.append({"name": name, "slot": None})
        else:
            sid, size = struct.unpack(">II", r[pos:pos + 8]); pos += 12
            out.append({"name": name, "slot": None if has_children else sid, "size": size})
    return out


class ElektronError(Exception):
    pass


class Syntakt:
    key = "syntakt"
    label = "Syntakt"

    def __init__(self, device, channel=14, scratch=("H", 128), backup_dir=None, owned_name=None):
        self.dev = device
        self.channel = channel                 # the Syntakt's AUTO CHANNEL, for test phrases and KeyLab through
        self.bank, last = scratch
        self.slots = (last,)
        self.slot = last
        self.backup_dir = backup_dir
        self.owned_name = owned_name           # name of the sound the app last put in the scratch slot
        self.loaded = None
        self.pre = None
        self.seq = 0
        self.lock = threading.Lock()
        self.on_owned = None                   # callback(name) so the session can remember it
        self.foreign = {}

    @property
    def path(self):
        return f"/soundbanks/{self.bank}/{self.slot}"

    # ---------------------------------------------------------------- protocol
    def _cmd(self, cmd, args=b"", timeout=5.0):
        self.seq = (self.seq + 1) & 0xFFFF
        seq = self.seq
        msg = bytes([seq >> 8, seq & 0xFF, 0, 0, cmd]) + bytes(args)

        def test(m):
            if not m.startswith(HDR) or len(m) < 12:
                return False
            r = decode(m[6:-1])
            return len(r) >= 6 and (r[2] << 8 | r[3]) == seq and r[4] == (cmd | 0x80)
        reply = self.dev.request(HDR + encode(msg) + b"\xF7", test, timeout)
        if reply is None:
            raise ElektronError(f"no reply to command {cmd:02X}")
        return decode(reply[6:-1])

    @staticmethod
    def _ok(r, what):
        if not r[5]:
            text = r[6:].split(b"\0")[0].decode("latin-1")
            raise ElektronError(f"{what} refused{': ' + text if text else ''}")
        return r

    def list(self, path):
        r = self._ok(self._cmd(0x53, path.encode("cp1252") + b"\0" + struct.pack(">IIB", 0, 0, 1)), f"listing {path}")
        return parse_list(r)

    def read(self, path):
        r = self._ok(self._cmd(0x54, path.encode("cp1252") + b"\0" + struct.pack(">IB", BLOCK, 1)), f"reading {path}")
        jid, buf, seq = struct.unpack(">I", r[6:10])[0], bytearray(), 0
        while True:
            r = self._ok(self._cmd(0x55, struct.pack(">II", jid, seq)), f"reading {path}")
            size = struct.unpack(">I", r[23:27])[0]
            buf.extend(r[27:27 + size])
            if r[18]:
                break
            seq += 1
            time.sleep(0.05)
        self._cmd(0x56, struct.pack(">I", jid))
        return bytes(buf)

    def write(self, path, data):
        r = self._ok(self._cmd(0x57, struct.pack(">I", len(data)) + path.encode("cp1252") + b"\0"), f"writing {path}")
        jid = struct.unpack(">I", r[6:10])[0]
        time.sleep(0.05)
        for seq, off in enumerate(range(0, len(data), BLOCK)):
            chunk = data[off:off + BLOCK]
            crc = zlib.crc32(chunk, 0xFFFFFFFF) & 0xFFFFFFFF
            self._ok(self._cmd(0x58, struct.pack(">IIII", jid, seq, crc, len(chunk)) + chunk), f"writing {path}")
            time.sleep(0.05)
        r = self._ok(self._cmd(0x59, struct.pack(">II", jid, len(data))), f"finishing {path}")
        got = struct.unpack(">I", r[10:14])[0]
        if got != len(data):
            raise ElektronError(f"the Syntakt stored {got} bytes, not {len(data)}")

    def clear(self, path):
        self._ok(self._cmd(0x5C, path.encode("cp1252") + b"\0"), f"clearing {path}")

    def in_scratch(self, slot=None):
        slot = slot or self.slot
        return next((e for e in self.list(f"/soundbanks/{self.bank}") if e["slot"] == slot), None)

    def scratch_label(self):
        return f"{self.bank}{self.slot}"

    # ---------------------------------------------------------------- driver calls
    def preflight(self):
        from .take5 import Preflight
        p = Preflight(False)
        try:
            with self.lock:
                r = self._cmd(0x01, timeout=1.5)
                if r[5] != SYNTAKT_ID:
                    p.lines.append(("bad", f"An Elektron device answered, but it is not a Syntakt (id {r[5]})"))
                    return p
                v = self._cmd(0x02)[10:].split(b"\0")[0].decode("latin-1")
                p.os_version = v
                p.lines.append(("ok", f"Syntakt found — OS {v}"))
                items = {e["slot"]: e for e in self.list(f"/soundbanks/{self.bank}") if e["slot"]}
        except ElektronError as e:
            p.lines.append(("bad", f"The Syntakt did not answer ({e}). Close Elektron Transfer — only one program may "
                                   "talk to it — and check its USB cable reaches the PC."))
            return p
        owned = {n.upper() for n in (self.owned_name or "").split("|") if n}
        for sl in self.slots:
            here = items.get(sl)
            if here is None:
                p.lines.append(("ok", f"Scratch slot {self.bank}{sl} is empty"))
            elif here["name"].upper() in owned:
                p.lines.append(("ok", f"Scratch slot {self.bank}{sl} holds '{here['name']}' from an earlier audition"))
            else:
                p.lines.append(("warn", f"Scratch slot {self.bank}{sl} holds '{here['name']}', which the app did not put "
                                        "there. It will be saved to _Patch Manager\\Backups\\Syntakt before it is used."))
                self.foreign[sl] = here["name"]
        p.lines.append(("ok", f"Each sound you pick goes to {self.bank}{self.slot}. On the Syntakt, leave the SOUND "
                              f"BROWSER on {self.bank}{self.slot} and press FUNC + YES to hear it"))
        p.lines.append(("ok", f"Play sends no note to the Syntakt. The KeyLab goes to MIDI channel {self.channel}, the "
                              "Syntakt's AUTO CHANNEL, which plays the active track (change it with MIDI channel… below)"))
        p.ok = True
        p.channel = self.channel
        self.pre = p
        return p

    def load(self, data: bytes, h: str, name: str):
        sound_name, payload = payload_of(data)
        with self.lock:
            if self.loaded and self.loaded[0] == h:
                return                                   # already in the scratch slot
            slot = self.slot
            if slot in self.foreign:
                self._backup_foreign(slot)
            self.write(f"/soundbanks/{self.bank}/{slot}", payload)
            self.loaded = (h, name)
            names = [n for n in (self.owned_name or "").split("|") if n][-1:] + [sound_name]
            self.owned_name = "|".join(names)
            if self.on_owned:
                self.on_owned(self.owned_name)

    def _backup_foreign(self, slot):
        data = self.read(f"/soundbanks/{self.bank}/{slot}")
        if self.backup_dir:
            os.makedirs(self.backup_dir, exist_ok=True)
            safe = "".join(c for c in self.foreign[slot] if c.isalnum() or c in " -_") or "sound"
            fn = os.path.join(self.backup_dir, f"{time.strftime('%Y-%m-%d %H%M%S')} {self.bank}{slot} {safe}.payload")
            with open(fn, "wb") as f:
                f.write(data)
        del self.foreign[slot]

    def read_edit(self, data: bytes):
        """What the scratch slot holds now — used to confirm each audition arrived."""
        sound_name, _ = payload_of(data)
        with self.lock:
            here = self.in_scratch(self.slot)
        got = here["name"] if here else "(empty)"
        return got, here is not None and here["name"].strip().upper() == sound_name.strip().upper()

    def put_back(self):
        return False
