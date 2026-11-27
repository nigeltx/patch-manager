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
        self.edit = None

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
        if msg[3] == 0x06 and self.edit is not None:            # the edit buffer: what the panel is playing
            m = b"\xF0\x01\x35\x03" + self.edit[6:]
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


def _cleared(lib):
    """A Take 5 whose user slots hold the init program, read into slot_state."""
    root, db, dbp = lib
    synth = WritableTake5()
    t5 = Take5(synth)
    synth.slots[(15, 15)] = t5_program(15, 15, "Basic Program")
    userslots.save_init(db, str(root), t5.read_program(16, 16))
    init = userslots.load_init(db, str(root))[0]
    for b in range(16):
        for p in range(16):
            synth.slots[(b, p)] = program_to(init, b + 1, p + 1)
    reads = t5.read_slots(banks=userslots.USER_BANKS)
    db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "hash": r.hash, "place": r.place, "name": r.name}
                           for r in reads])
    db.con.commit()
    return root, db, synth, t5


def _mine(db):
    return {p["name"]: p for p in userslots.your_patches(db)}


def test_send_to_slot_from_a_chosen_slot(lib):
    root, db, synth, t5 = _cleared(lib)
    mine = _mine(db)
    sm = userslots.slot_map(db)
    assert all(v["state"] == "free" for v in sm.values()) and len(sm) == 256
    # something made on the panel in U3-05, not in any source yet
    synth.slots[(2, 4)] = t5_program(2, 4, "Panel Sound")
    h, _ = userslots.hash_of(t5.read_program(3, 5))
    db.set_slots("take5", [{"bank": 3, "slot": 5, "hash": h, "place": "U3-05", "name": "Panel Sound"}])
    assert userslots.slot_map(db)[(3, 5)]["state"] == "unknown"
    steps, left = userslots.send_plan(db, [mine["Acid Bass"], mine["Zed Pad"]], (3, 5))
    assert [s.place for s in steps] == ["U3-05", "U3-06"] and not left
    assert "kept in Sources › User first" in steps[0].why and steps[1].why == "free"
    res = userslots.run(t5, db, str(root), steps, "Send to slot")
    assert res.done == 2 and res.kept == ["U3-05 Panel Sound"]
    kept = list((root / "Take 5" / "Sources" / "User").rglob("*Panel Sound*"))
    assert kept, "the panel sound was kept in Sources › User before it was written over"
    assert t5_payload(synth.slots[(2, 4)]) == t5_payload(bytes(mine["Acid Bass"]["data"]))
    # near the end: what doesn't fit is reported
    steps, left = userslots.send_plan(db, [mine["Acid Bass"], mine["Zed Pad"]], (16, 16))
    assert [s.place for s in steps] == ["U16-16"] and [p["name"] for p in left] == ["Zed Pad"]
    # undo puts the panel sound back
    undo = userslots.undo_steps(db, res.run)
    userslots.run(t5, db, str(root), undo, "Undo Send to slot")
    assert t5_payload(synth.slots[(2, 4)]) == t5_payload(t5_program(2, 4, "Panel Sound"))


def test_sync_sends_the_crate_into_free_slots_only(lib):
    root, db, synth, t5 = _cleared(lib)
    mine = _mine(db)
    # U1-01 already holds Acid Bass; U1-02 holds a sound of the user's that is not in the crate
    synth.slots[(0, 0)] = program_to(bytes(mine["Acid Bass"]["data"]), 1, 1)
    synth.slots[(0, 1)] = program_to(bytes(mine["Other Thing"]["data"]), 1, 2)
    reads = t5.read_slots(banks=[1])
    db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "hash": r.hash, "place": r.place, "name": r.name}
                           for r in reads])
    cid = userslots.for_crate(db)
    assert userslots.for_crate(db) == cid                           # made once
    db.crate_add(cid, [mine["Acid Bass"]["hash"], mine["Zed Pad"]["hash"]])
    steps, left, there = userslots.sync_plan(db)
    assert [(s.place, s.name) for s in steps] == [("U1-03", "Zed Pad")] and there == 1 and not left
    res = userslots.run(t5, db, str(root), steps, "Sync")
    assert res.done == 1
    assert userslots.sync_plan(db)[0] == []                         # in step: nothing to send again
    assert t5_payload(synth.slots[(0, 1)]) == t5_payload(bytes(mine["Other Thing"]["data"]))   # not written over


def test_the_init_program_is_a_patch_too(lib):
    """Nigel, 27 Nov 2026: the Basic Program should be available as a patch, while a slot holding it stays free."""
    from patchmgr.library import rebuild
    root, db, dbp = lib
    synth = WritableTake5()
    t5 = Take5(synth)
    synth.slots[(15, 15)] = t5_program(15, 15, "Basic Program")
    ih, _ = userslots.save_init(db, str(root), t5.read_program(16, 16))
    src = userslots.init_source_path(str(root), "Basic Program")
    assert os.path.isfile(src) and "Factory" in src
    assert userslots.keep_init_as_patch(str(root)) is None              # only once
    Scanner(dbp, [(str(root), "library")]).run()
    db = DB(dbp)
    assert ih in factory.library_hashes(db, "take5")
    assert ih in {w.hash for w in rebuild.wanted(db, "take5")}           # it goes into Patches
    assert ih not in {p["hash"] for p in userslots.your_patches(db)}     # but Fill never sends it
    pid = db.one("SELECT id FROM patch WHERE hash=?", (ih,))
    assert db.is_factory(pid)                                          # a factory patch (Nigel, 27 Nov 2026)
    db.set_slots("take5", [{"bank": 2, "slot": 2, "hash": ih, "place": "U2-02", "name": "Basic Program"}])
    assert userslots.slot_map(db)[(2, 2)]["state"] == "free"              # a slot holding it is still free


def test_initialize_picked_slots_from_the_panel(lib):
    """Nigel, 27 Nov 2026: set any slots he picks to Basic Program, taken from the panel without saving it."""
    root, db, dbp = lib
    synth = WritableTake5()
    t5 = Take5(synth)
    synth.edit = t5_program(0, 0, "Basic Program")                  # Init Program chosen on the panel
    e = t5.read_edit_buffer()
    ih, name = userslots.save_init(db, str(root), e)
    assert name == "Basic Program"
    init = userslots.load_init(db, str(root))[0]
    synth.slots[(4, 1)] = program_to(init, 5, 2)                   # U5-02 already empty
    reads = t5.read_slots(banks=[5])
    db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "hash": r.hash, "place": r.place, "name": r.name}
                           for r in reads])
    steps, already = userslots.init_plan(db, [(5, 1), (5, 2), (5, 3), (5, 3)], init)
    assert [s.place for s in steps] == ["U5-01", "U5-03"] and already == 1
    res = userslots.run(t5, db, str(root), steps, "Initialize slots")
    assert res.done == 2
    assert t5_payload(synth.slots[(4, 0)]) == t5_payload(init) and t5_payload(synth.slots[(4, 2)]) == t5_payload(init)
    assert t5_payload(synth.slots[(4, 3)]) != t5_payload(init)        # U5-04 not picked: untouched
    assert userslots.slot_map(db)[(5, 1)]["state"] == "free"
    assert userslots.last_run(db) == res.run                        # Undo the last write… finds it


def test_targets_presets_lists_only_what_is_on_the_synth(lib):
    """Nigel, 27 Nov 2026: <Synth> › Targets › Presets shows only the patches on that synth now."""
    from patchmgr.library import search
    root, db, dbp = lib
    mine = {r["name"]: r for r in db.q("SELECT name, hash FROM patch WHERE synth='take5'")}
    db.set_slots("take5", [{"bank": 1, "slot": 1, "hash": mine["Acid Bass"]["hash"], "place": "U1-01", "name": "Acid Bass"}])
    rows = db.q(*search.build("", {"synth": "take5", "onsynth": True}))
    assert [r["name"] for r in rows] == ["Acid Bass"]
    assert len(db.q(*search.build("", {"synth": "take5"}))) > 1


def test_bank_view_layouts_match_the_synths():
    from patchmgr.ui.bankview import layout_for
    rows, cols, cells = layout_for("take5")
    assert rows[0] == "U1" and rows[16] == "F1" and len(rows) == 32 and len(cols) == 16 and len(cells) == 512
    assert (16, 0, 17, 1) in cells                                  # F1-01 is bank 17, program 1
    rows, cols, cells = layout_for("sub37")
    assert rows[9] == "Bank 10" and len(cells) == 256
    rows, cols, cells = layout_for("syntakt", 8)
    assert rows[0] == "H001" and rows[7] == "H113" and len(cells) == 128 and cells[-1] == (7, 15, 8, 128)
