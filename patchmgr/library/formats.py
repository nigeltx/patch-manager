"""File readers for every patch format in Studio → Patches (spec 4.2).

Each reader takes the file's bytes and path and returns a list of Patch records, or raises
FormatError with a plain reason. A file is never half-read: any problem rejects the whole file.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field

# ---------------------------------------------------------------- shared


class FormatError(Exception):
    """The file is not a patch file the app can read; the message says why, in plain words."""


@dataclass
class Patch:
    synth: str                      # "take5" | "sub37" | "deluge" | "syntakt"
    name: str
    data: bytes                     # the bytes the app would send or copy, exactly as found
    sound_hash: str                 # identity: hash of the sound data alone (spec 4.3)
    fmt: str                        # "program", "edit buffer", "preset", "all presets", "xml", "stsnd", "stprj"
    index: int = 0                  # position in the file
    bank: int | None = None         # original bank (1-based) where the file says
    slot: int | None = None         # original program/slot (1-based)
    tags: list[str] = field(default_factory=list)     # tags found in the file itself
    samples: list[str] = field(default_factory=list)  # Deluge sample paths referenced
    kind: str = "sound"             # "sound" | "project" | "backup"
    blank: bool = False             # an init / blank starting sound ("Init Preset"): never factory, slot counts as free


def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def split_sysex(data: bytes) -> list[bytes]:
    """Split a .syx file into whole F0…F7 messages. Anything outside a message, or an unfinished one, is an error."""
    msgs, i, n = [], 0, len(data)
    while i < n:
        if data[i] != 0xF0:
            if data[i] in (0x0A, 0x0D, 0x20, 0x00) and not data[i:].strip(b"\r\n \x00"):
                break                                  # trailing padding some tools add
            raise FormatError(f"stray byte {data[i]:02X} at offset {i} (not inside a SysEx message)")
        j = data.find(b"\xF7", i + 1)
        if j < 0:
            raise FormatError(f"a SysEx message starting at offset {i} never ends (no F7)")
        msg = data[i:j + 1]
        bad = next((k for k, x in enumerate(msg[1:-1], 1) if x & 0x80), None)
        if bad is not None:
            raise FormatError(f"status byte {msg[bad]:02X} inside a SysEx message at offset {i + bad}")
        msgs.append(msg)
        i = j + 1
    if not msgs:
        raise FormatError("empty file")
    return msgs


# ---------------------------------------------------------------- Sequential Take 5

T5_ID = b"\xF0\x01\x35"
T5_PROGRAM, T5_EDIT = 0x02, 0x03
T5_PROG_LEN, T5_EDIT_LEN = 4695, 4693
T5_NAME_AT, T5_NAME_LEN = 195, 20


def unpack_ms(b: bytes) -> bytes:
    """Sequential 'packed MS bit': in each group of 8 the first byte holds the top bits of the next 7."""
    out = bytearray()
    for k in range(0, len(b), 8):
        hi = b[k]
        for n, x in enumerate(b[k + 1:k + 8]):
            out.append(x | (((hi >> n) & 1) << 7))
    return bytes(out)


def pack_ms(b: bytes) -> bytes:
    out = bytearray()
    for k in range(0, len(b), 7):
        chunk = b[k:k + 7]
        hi = 0
        for n, x in enumerate(chunk):
            hi |= ((x >> 7) & 1) << n
        out.append(hi)
        out.extend(x & 0x7F for x in chunk)
    return bytes(out)


def t5_payload(msg: bytes) -> bytes:
    return msg[6:-1] if msg[3] == T5_PROGRAM else msg[4:-1]


def t5_name(unpacked: bytes) -> str:
    raw = unpacked[T5_NAME_AT:T5_NAME_AT + T5_NAME_LEN]
    return raw.split(b"\x00")[0].decode("latin-1").strip()


def read_take5(data: bytes, path: str) -> list[Patch]:
    msgs = split_sysex(data)
    out = []
    for i, m in enumerate(msgs):
        if not m.startswith(T5_ID):
            raise FormatError(f"message {i + 1} is not a Take 5 message (starts {m[:4].hex(' ').upper()})")
        if m[3] == T5_PROGRAM and len(m) == T5_PROG_LEN:
            fmt, bank, slot = "program", m[4] + 1, m[5] + 1
        elif m[3] == T5_EDIT and len(m) == T5_EDIT_LEN:
            fmt, bank, slot = "edit buffer", None, None
        else:
            raise FormatError(f"message {i + 1} is a Take 5 message of type {m[3]:02X} and {len(m):,} bytes, "
                              "not a program (4,695) or edit buffer (4,693)")
        un = unpack_ms(t5_payload(m))
        name = t5_name(un) or f"Program {i + 1}"
        out.append(Patch("take5", name, m, sha(b"take5" + un), fmt, i, bank, slot))
    return out


def t5_to_edit(msg: bytes) -> bytes:
    return b"\xF0\x01\x35\x03" + t5_payload(msg) + b"\xF7"


# ---------------------------------------------------------------- Moog Sub 37

S37_ID = b"\xF0\x04\x0C"
S37_BUFFER, S37_PRESET, S37_ALL = 0x04, 0x02, 0x01
S37_LEN, S37_ALL_LEN = 756, 203800
S37_DATA_FROM = 7                    # F0 04 0C type ver slotH slotL | data … | checksum(3) F7


S37_RECORD = 796                     # ALL PRESETS: 7-byte header, 256 records of 796 bytes, then 00 01 F7 (test 18)
S37_RECORD_DATA = 748                # a record = the 748 bytes after a single preset's header (data + checksum), then zeros
S37_NAME_AT, S37_NAME_GROUPS = 23, 10
S37_BLANK_NAMES = {"init preset"}


def s37_sound(msg: bytes) -> bytes:
    return msg[S37_DATA_FROM:-4]


def s37_name(msg: bytes) -> str:
    """The preset's own name: 3-byte groups each holding two characters as a 16-bit word
    ((b0 & 0x0F) << 12 | b1 << 6 | b2), starting at byte 23 of a single-preset message."""
    out = bytearray()
    for i in range(S37_NAME_AT, S37_NAME_AT + 3 * S37_NAME_GROUPS, 3):
        if i + 2 >= len(msg):
            break
        w = ((msg[i] & 0x0F) << 12) | ((msg[i + 1] & 0x3F) << 6) | (msg[i + 2] & 0x3F)
        out += bytes([w >> 8, w & 0xFF])
    text = out.split(b"\x00")[0].decode("latin-1", "replace")
    return "".join(c for c in text if c.isprintable()).strip()


def s37_split_all(data: bytes) -> list[bytes]:
    """An ALL PRESETS dump → 256 single stored-preset messages (F0 04 0C 02 07 hi lo … F7), slot 0 = 1-1.
    Message byte 14 is 60 in every single dump but 00 in most records; the checksum ignores it, so it is set to 60
    and a slot split from a backup is byte-for-byte the same as a CUR PRESET dump of that slot (bench test 18)."""
    if len(data) != S37_ALL_LEN or not data.startswith(S37_ID + bytes([S37_ALL])):
        raise FormatError("not a Sub 37 ALL PRESETS dump (203,800 bytes starting F0 04 0C 01)")
    out = []
    for n in range(256):
        rec = bytearray(data[7 + S37_RECORD * n: 7 + S37_RECORD * n + S37_RECORD_DATA])
        rec[7] = 0x60
        out.append(bytes([0xF0, 0x04, 0x0C, S37_PRESET, data[4], n >> 7, n & 0x7F]) + bytes(rec) + b"\xF7")
    return out


NUM_PREFIX = re.compile(r"^\s*(\d{1,4})\s*[-_. ]\s*")


def name_from_file(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.strip()


def read_sub37(data: bytes, path: str) -> list[Patch]:
    if len(data) == S37_ALL_LEN and data.startswith(S37_ID + bytes([S37_ALL])):
        stem = name_from_file(path)
        out = []
        for n, m in enumerate(s37_split_all(data)):
            name = s37_name(m) or f"{stem} {n // 16 + 1}-{n % 16 + 1}"
            out.append(Patch("sub37", name, m, sha(b"sub37" + s37_sound(m)), "stored preset", n,
                             bank=n // 16 + 1, slot=n % 16 + 1, blank=name.lower() in S37_BLANK_NAMES))
        out.append(Patch("sub37", stem, data, sha(b"sub37all" + data), "all presets", 256, kind="backup"))
        return out
    msgs = split_sysex(data)
    out = []
    stem = name_from_file(path)
    for i, m in enumerate(msgs):
        if not m.startswith(S37_ID):
            raise FormatError(f"message {i + 1} is not a Moog Sub 37 message (starts {m[:4].hex(' ').upper()})")
        if len(m) != S37_LEN or m[3] not in (S37_BUFFER, S37_PRESET):
            raise FormatError(f"message {i + 1} is a Sub 37 message of type {m[3]:02X} and {len(m):,} bytes, "
                              "not a 756-byte preset")
        slot = None
        if m[3] == S37_PRESET:
            n = (m[5] << 7) | m[6]
            slot = n
        name = stem if len(msgs) == 1 else f"{stem} #{i + 1}"
        fmt = "preset" if m[3] == S37_BUFFER else "stored preset"
        own = s37_name(m)
        out.append(Patch("sub37", name, m, sha(b"sub37" + s37_sound(m)), fmt, i,
                         bank=(slot // 16 + 1) if slot is not None else None,
                         slot=(slot % 16 + 1) if slot is not None else None,
                         blank=own.lower() in S37_BLANK_NAMES))
    return out


# ---------------------------------------------------------------- Deluge

DELUGE_SAMPLE_TAG = re.compile(rb"<fileName>([^<]+)</fileName>|fileName=\"([^\"]+)\"")


def read_deluge(data: bytes, path: str) -> list[Patch]:
    head = data[:600].lstrip(b"\xef\xbb\xbf \r\n\t")
    if not head.startswith(b"<?xml") and b"<sound" not in head:
        raise FormatError("not a Deluge preset (no XML header)")
    if b"<sound" not in data:
        if b"<kit" in data:
            raise FormatError("a Deluge kit, not a synth preset (kits belong to the Deluge Sample Manager)")
        if b"<song" in data:
            raise FormatError("a Deluge song, not a synth preset")
        raise FormatError("XML without a <sound> preset in it")
    if not data.rstrip().endswith(b">"):
        raise FormatError("the XML is cut short")
    samples = []
    for a, b in DELUGE_SAMPLE_TAG.findall(data):
        s = (a or b).decode("utf-8", "replace").strip()
        if s and s not in samples:
            samples.append(s)
    return [Patch("deluge", name_from_file(path), data, sha(b"deluge" + data), "xml", samples=samples)]


# ---------------------------------------------------------------- Elektron Syntakt

SYNTAKT_SLOT_BYTES = (23, 24)          # the pool bank (0–7 = A–H) and slot − 1 the sound was saved from


def syntakt_identity(name: str, body: bytes) -> str:
    """A Syntakt sound's identity: its name and its sound data, leaving out the two bytes that only record which
    pool slot it was saved from — so G020 and a copy of it in H127 are the same sound, and a copy you renamed is
    a sound of your own (Nigel, 26 Nov 2026)."""
    b = bytearray(body)
    for i in SYNTAKT_SLOT_BYTES:
        if i < len(b):
            b[i] = 0
    return sha(b"syntakt2" + (name or "").encode("utf-8") + b"\x00" + bytes(b))


def read_elektron(data: bytes, path: str) -> list[Patch]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        man = json.loads(z.read("manifest.json").decode("utf-8-sig"))
        names = z.namelist()
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise FormatError(f"not a readable Elektron file ({e.__class__.__name__}: {e})")
    ftype = man.get("FileType", "")
    payload = man.get("Payload") or name_from_file(path)
    body = b"".join(z.read(n) for n in sorted(names) if n != "manifest.json")
    tags = [t.title() for t in (man.get("MetaInfo") or {}).get("Tags", []) if isinstance(t, str)]
    if ftype == "Sound":
        return [Patch("syntakt", payload, data, syntakt_identity(payload, body), "stsnd", tags=tags)]
    if ftype == "Project":
        return [Patch("syntakt", name_from_file(path), data, sha(b"syntaktprj" + body), "stprj", kind="project")]
    raise FormatError(f"an Elektron file of type '{ftype}', not a sound or project")


def make_stsnd(name: str, payload: bytes) -> bytes:
    """A .stsnd file for a sound read from the Syntakt's pool: the payload unchanged, named in the manifest, so it
    has the same identity as the same sound in a pack (spec 4.3)."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"FileType": "Sound", "Payload": name, "ProductType": "Syntakt",
                                                "MetaInfo": {"Tags": []}}, indent=1))
        z.writestr(name, payload)
    return b.getvalue()


# ---------------------------------------------------------------- slots on a synth

SYNTAKT_BANKS = "ABCDEFGH"


def place(synth: str, bank: int, slot: int, path: str | None = None) -> str:
    """How the synth's own panel names a slot: Take 5 U3-07 or F12-01, Sub 37 10-5, Syntakt A:002, Deluge a file."""
    if synth == "take5":
        return f"U{bank}-{slot:02d}" if bank <= 16 else f"F{bank - 16}-{slot:02d}"
    if synth == "sub37":
        return f"{bank}-{slot}"
    if synth == "syntakt":
        return f"{SYNTAKT_BANKS[bank - 1]}:{slot:03d}"
    return path or f"{bank}-{slot}"


# ---------------------------------------------------------------- dispatch

SYNTH_FOLDERS = {"take 5": "take5", "sub 37": "sub37", "deluge": "deluge", "syntakt": "syntakt"}
SYNTH_LABEL = {"take5": "Take 5", "sub37": "Sub 37", "deluge": "Deluge", "syntakt": "Syntakt"}
PATCH_EXT = {".syx", ".xml", ".stsnd", ".stprj"}
AUDIO_EXT = {".wav", ".mp3", ".aif", ".aiff", ".flac"}


def read_file(data: bytes, path: str, synth_hint: str | None) -> list[Patch]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".syx":
        if data.startswith(T5_ID):
            return read_take5(data, path)
        if data.startswith(S37_ID):
            return read_sub37(data, path)
        if not data:
            raise FormatError("empty file")
        raise FormatError(f"SysEx for a synth the app does not know (starts {data[:4].hex(' ').upper()})")
    if ext == ".xml":
        return read_deluge(data, path)
    if ext in (".stsnd", ".stprj"):
        return read_elektron(data, path)
    raise FormatError("not a patch file")
