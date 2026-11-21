"""Sequential Take 5 driver, milestone 2: recognise, preflight, edit-buffer audition (spec 2.1, 3.2, 3.4, 6.1).
Nothing here writes to a program slot."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..library.formats import T5_EDIT, T5_EDIT_LEN, T5_ID, T5_NAME_AT, T5_PROGRAM, t5_name, t5_payload, unpack_ms

INQUIRY = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]
REQ_EDIT = [0xF0, 0x01, 0x35, 0x06, 0xF7]


@dataclass
class Preflight:
    ok: bool
    lines: list = field(default_factory=list)       # (state 'ok'|'warn'|'bad', text)
    channel: int | None = None                      # 1–16, or None for All
    os_version: str = ""
    original_edit: bytes | None = None


def is_inquiry_reply(m):
    return len(m) >= 13 and m[1] == 0x7E and m[3] == 0x06 and m[4] == 0x02 and m[5] == 0x01 and m[6] == 0x35


def is_edit_dump(m):
    return m.startswith(T5_ID) and len(m) == T5_EDIT_LEN and m[3] == T5_EDIT


def edit_from(data: bytes) -> bytes:
    """Any Take 5 program or edit-buffer message → an Edit Buffer Data Dump (plays, stores nothing)."""
    if not data.startswith(T5_ID) or data[3] not in (T5_PROGRAM, T5_EDIT):
        raise ValueError("not a Take 5 program")
    return b"\xF0\x01\x35\x03" + t5_payload(data) + b"\xF7"


class Take5:
    key = "take5"
    label = "Take 5"
    CAN = {"audition": True, "read_slot": False, "write_slot": False, "backup": False}   # M2

    def __init__(self, device):
        self.dev = device
        self.channel = 16
        self.loaded = None          # (hash, name) now in the edit buffer, sent by the app
        self.pre: Preflight | None = None

    def preflight(self) -> Preflight:
        p = Preflight(False)
        r = self.dev.request(INQUIRY, is_inquiry_reply, 1.5)
        if r is None:
            p.lines.append(("bad", "The Take 5 did not answer the device inquiry. Check its USB cable reaches the PC "
                                   "and GLOBAL 21 MIDI SysEx is On."))
            return p
        p.channel = None if r[2] == 0x7F else r[2] + 1
        p.os_version = ".".join(str(x) for x in r[10:13])
        p.lines.append(("ok", f"Take 5 found — OS {p.os_version}, MIDI channel {p.channel or 'All'}"))
        if r[10] < 2:
            p.lines.append(("warn", f"OS {p.os_version} is older than 2.0; audition is only tested on 2.1"))
        e = self.dev.request(REQ_EDIT, is_edit_dump, 2.0)
        if e is None:
            p.lines.append(("bad", "The Take 5 answered but sent no program data back. Set GLOBAL 21 MIDI SysEx "
                                   "On and GLOBAL 22 SysEx Cable to USB."))
            return p
        p.original_edit = e
        p.lines.append(("ok", f"SysEx over USB works — the sound on the Take 5 now is '{t5_name(unpack_ms(t5_payload(e)))}' "
                              "(kept, so it can be put back)"))
        if p.channel:
            self.channel = p.channel
        else:
            p.lines.append(("warn", "MIDI channel is All; test phrases go out on channel 16"))
        p.ok = True
        self.pre = p
        return p

    def load(self, data: bytes, h: str, name: str):
        self.dev.send(edit_from(data))
        self.loaded = (h, name)

    def put_back(self):
        if self.pre and self.pre.original_edit:
            self.dev.send(self.pre.original_edit)
            self.loaded = None
            return True
        return False

    def read_edit(self, data: bytes):
        """-> (name the Take 5 reports in its edit buffer, whether it matches data)."""
        e = self.dev.request(REQ_EDIT, is_edit_dump, 2.0)
        if e is None:
            return "(no reply)", False
        return t5_name(unpack_ms(t5_payload(e))), t5_payload(e) == t5_payload(data)

    def check_loaded(self, data: bytes) -> bool:
        """Read the edit buffer back and compare with what was sent (used by the M2 self-check)."""
        e = self.dev.request(REQ_EDIT, is_edit_dump, 2.0)
        return e is not None and t5_payload(e) == t5_payload(data)
