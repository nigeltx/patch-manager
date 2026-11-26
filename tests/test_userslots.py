"""Clearing and filling the Take 5's user slots (spec 7.9, 7.10)."""
import os

import pytest

from patchmgr.drivers.take5 import Take5, program_to
from patchmgr.library import factory, layout, userslots
from patchmgr.library.db import DB
from patchmgr.library.formats import t5_payload
from patchmgr.library.scanner import Scanner
from tests.test_library import t5_program


class WritableTake5:
    """A Take 5 that remembers what is written to it. U banks start as copies of the factory banks."""
    def __init__(self, bad_slot=None):
        self.slots = {}
        for b in range(32):
            for p in range(16):
                fb = b if b >= 16 else b + 16
                self.slots[(b, p)] = t5_program(b, p, f"F{fb - 15}P{p + 1}")
        self.bad_slot = bad_slot
        self.writes = []

    def send(self, msg):
        msg = bytes(msg)
        if msg[3] == 0x02:
            b, p = msg[4], msg[5]
            self.writes.append((b + 1, p + 1))
            stored = msg if (b + 1, p + 1) != self.bad_slot else msg[:300] + bytes([1]) * 40 + msg[340:]
            self.slots[(b, p)] = stored

    def request(self, msg, test, timeout=2.0):
        if msg[3] == 0x05:
            m = self.slots[(msg[4], msg[5])]
            m = m[:4] + bytes([msg[4], msg[5]]) + m[6:]
            return m if test(m) else None
        return None


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr("patchmgr.drivers.take5.WRITE_SETTLE", 0)
    root = tmp_path / "Patches"
    for t in layout.SOURCE_TYPES:
        (root / "Take 5" / "Sources" / t).mkdir(parents=True)
    (root / "Take 5" / "Sources" / "Factory" / "factory.syx").write_bytes(
        b"".join(t5_program(16 + b, p, f"F{b + 1}P{p + 1}") for b in range(16) for p in range(16)))
    mine = root / "Take 5" / "Sources" / "User" / "2026-11-22 Take 5 read"
    mine.mkdir()
    for n in ["Zed Pad", "Acid Bass", "Other Thing"]:
        (mine / f"{n}.syx").write_bytes(t5_program(0, 0, n))
    dbp = str(tmp_path / "lib.db")
    Scanner(dbp, [(str(root), "library")]).run()
    db = DB(dbp)
    return root, db, dbp


def test_program_to_refuses_factory_banks():
    with pytest.raises(ValueError):
        program_to(t5_program(0, 0, "x"), 17, 1)
    m = program_to(t5_program(5, 5, "x"), 16, 16)
    assert m[4:6] == bytes([15, 15]) and len(m) == 4695


def test_clear_then_fill(lib):
    root, db, dbp = lib
    synth = WritableTake5()
    t5 = Take5(synth)
    # 1 the init program, made on the panel in U16-16
    synth.slots[(15, 15)] = t5_program(15, 15, "Basic Program")
    init_msg = t5.read_program(16, 16)
    ih, name = userslots.save_init(db, str(root), init_msg)
    assert name == "Basic Program" and os.path.isfile(userslots.init_path(str(root)))
    # 2 read the user banks; nothing new to keep (all factory copies; the init is blank)
    reads = t5.read_slots(banks=userslots.USER_BANKS)
    assert factory.new_sounds(db, "take5", reads) == []
    db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "hash": r.hash, "place": r.place, "name": r.name} for r in reads])
    # 3 plan: 255 slots to clear, U16-16 already init
    init = userslots.load_init(db, str(root))[0]
    steps, counts = userslots.clear_plan(db, reads, init)
    assert len(steps) == 255 and counts["init already"] == 1 and counts["factory copy"] == 255
    res = userslots.run(t5, db, str(root), steps, "Clear user slots")
    assert res.done == 255 and not res.failed
    assert all(t5_payload(synth.slots[(b, p)]) == t5_payload(init) for b in range(16) for p in range(16))
    assert all(b <= 16 for b, _ in synth.writes)                       # never a factory bank
    assert len(os.listdir(res.backup)) == 255
    # 4 fill: your three, packed from U1-01 in Patches order (Bass, Pad, Unknown)
    steps, left = userslots.fill_plan(db, ih)
    assert [(s.place, s.name) for s in steps] == [("U1-01", "Acid Bass"), ("U1-02", "Zed Pad"), ("U1-03", "Other Thing")]
    res = userslots.run(t5, db, str(root), steps, "Fill user slots")
    assert res.done == 3
    assert userslots.fill_plan(db, ih)[0] == []                       # already on the synth: nothing to send again
    # 5 undo the fill: U1-01..03 back to init
    undo = userslots.undo_steps(db, res.run)
    assert len(undo) == 3
    userslots.run(t5, db, str(root), undo, "Undo")
    assert t5_payload(synth.slots[(0, 0)]) == t5_payload(init)


def test_a_slot_that_changed_is_skipped_and_a_bad_write_stops(lib):
    root, db, dbp = lib
    synth = WritableTake5(bad_slot=(1, 3))
    t5 = Take5(synth)
    synth.slots[(15, 15)] = t5_program(15, 15, "Basic Program")
    init = userslots.load_init(db, str(root)) or None
    userslots.save_init(db, str(root), t5.read_program(16, 16))
    init = userslots.load_init(db, str(root))[0]
    reads = t5.read_slots(banks=[1])
    steps, _ = userslots.clear_plan(db, reads, init)
    synth.slots[(0, 0)] = t5_program(0, 0, "Edited On Panel")         # changed after the read
    res = userslots.run(t5, db, str(root), steps, "Clear user slots")
    assert res.skipped and res.skipped[0][0] == "U1-01"
    assert res.done == 1 and "U1-03" in res.failed                    # U1-02 done, U1-03 failed and stopped
    assert synth.writes[-2:] == [(1, 3), (1, 3)]                      # the write, then the old program put back
    assert t5_payload(synth.slots[(0, 1)]) == t5_payload(init)          # U1-02 cleared
