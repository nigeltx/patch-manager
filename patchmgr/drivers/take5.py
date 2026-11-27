"""Sequential Take 5 driver: recognise, preflight, edit-buffer audition (spec 2.1, 3.2, 3.4, 6.1), reading slots (M3)
and writing one user slot at a time with a read-back check (M4, spec 7.1). Factory banks are never written here."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..library.formats import (T5_EDIT, T5_EDIT_LEN, T5_ID, T5_NAME_AT, T5_PROG_LEN, T5_PROGRAM, place, read_take5,
                               t5_name, t5_payload, unpack_ms)

INQUIRY = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]
REQ_EDIT = [0xF0, 0x01, 0x35, 0x06, 0xF7]
ALL_BANKS = range(1, 33)            # 1–16 user banks U1–U16, 17–32 factory banks F1–F16 (bytes 0–31)
USER_BANKS = range(1, 17)
WRITE_SETTLE = 0.15                 # seconds between a write and its read-back (bench test 14 stored at once)


def req_program(bank, prog):
    """Request Program Dump: bank 1–32 (17–32 are the factory banks), program 1–16."""
    return [0xF0, 0x01, 0x35, 0x05, bank - 1, prog - 1, 0xF7]


def is_program(bank, prog):
    def test(m):
        return m.startswith(T5_ID) and len(m) == T5_PROG_LEN and m[3] == T5_PROGRAM and m[4] == bank - 1 \
            and m[5] == prog - 1
    return test


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


def program_to(data: bytes, bank: int, prog: int) -> bytes:
    """Any Take 5 program or edit-buffer message → a Program Data Dump for user bank 1–16, program 1–16."""
    if not 1 <= bank <= 16 or not 1 <= prog <= 16:
        raise ValueError(f"U{bank}-{prog:02d} is not a user slot")
    if not data.startswith(T5_ID) or data[3] not in (T5_PROGRAM, T5_EDIT):
        raise ValueError("not a Take 5 program")
    return bytes([0xF0, 0x01, 0x35, 0x02, bank - 1, prog - 1]) + t5_payload(data) + b"\xF7"


class Take5:
    key = "take5"
    label = "Take 5"
    CAN = {"audition": True, "read_slot": True, "write_slot": True, "backup": True}

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

    def read_edit_buffer(self):
        """The program now in the Take 5's edit buffer (an Edit Buffer Data Dump), or None. Used to take the init
        program — Basic Program — straight from the panel, without saving it to a slot."""
        e = self.dev.request(REQ_EDIT, is_edit_dump, 2.0)
        return e

    def check_loaded(self, data: bytes) -> bool:
        """Read the edit buffer back and compare with what was sent (used by the M2 self-check)."""
        e = self.dev.request(REQ_EDIT, is_edit_dump, 2.0)
        return e is not None and t5_payload(e) == t5_payload(data)

    # ---------------------------------------------------------------- reading slots (M3: backups, Initialize, green rows)
    def read_program(self, bank, prog, timeout=2.0):
        m = self.dev.request(req_program(bank, prog), is_program(bank, prog), timeout)
        if m is None:                              # one retry: a reply can be lost while the synth is busy
            m = self.dev.request(req_program(bank, prog), is_program(bank, prog), timeout)
        return m

    def read_slots(self, banks=ALL_BANKS, progress=None, cancelled=None):
        """Read every program in the given banks. -> list of SlotRead. Raises RuntimeError if the Take 5 stops answering."""
        from ..library.factory import SlotRead
        out, todo = [], [(b, p) for b in banks for p in range(1, 17)]
        for i, (b, p) in enumerate(todo):
            if cancelled and cancelled():
                break
            m = self.read_program(b, p)
            if m is None:
                raise RuntimeError(f"The Take 5 did not send program {place('take5', b, p)} — check its USB cable and "
                                   "that GLOBAL 21 MIDI SysEx is On")
            pt = read_take5(m, "slot.syx")[0]
            out.append(SlotRead("take5", b, p, place("take5", b, p), pt.name, m, pt.sound_hash))
            if progress:
                progress(i + 1, len(todo), f"{place('take5', b, p)} {pt.name}")
        return out

    # ---------------------------------------------------------------- writing a user slot (M4, spec 7.1)
    def write_program(self, data: bytes, bank: int, prog: int):
        """Write one user slot and read it back. -> (ok, what came back or None). Never a factory bank."""
        import time
        msg = program_to(data, bank, prog)
        self.dev.send(msg)
        time.sleep(WRITE_SETTLE)
        back = self.read_program(bank, prog)
        return (back is not None and t5_payload(back) == t5_payload(msg)), back
