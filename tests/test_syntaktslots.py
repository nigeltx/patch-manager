"""Sending sounds to the Syntakt's bank H, Sync and undo (spec 7.4, 7.8)."""
import threading

import pytest

from patchmgr.library import layout, syntaktslots as ss
from patchmgr.library.db import DB
from patchmgr.library.factory import SlotRead
from patchmgr.library.formats import make_stsnd, place, read_elektron
from patchmgr.library.scanner import Scanner


def body(name, slot=1, n=80):
    b = bytearray((len(name) * 7 + i) % 251 for i in range(n))
    b[23], b[24] = 7, slot - 1                                # where it was saved from: not part of its identity
    return bytes(b)


class FakeSyntakt:
    """The driver calls the sync code uses: list, read, write, clear, lock — over a dict of bank H."""
    def __init__(self, bad_slot=None):
        self.h = {}                                           # slot → (name, payload)
        self.lock = threading.RLock()
        self.bad_slot = bad_slot
        self.writes = []

    def list(self, path):
        assert path == "/soundbanks/H"
        return [{"name": n, "slot": s, "size": len(p)} for s, (n, p) in sorted(self.h.items())]

    def read(self, path):
        return self.h[int(path.rsplit("/", 1)[1])][1]

    def write(self, path, payload):
        s = int(path.rsplit("/", 1)[1])
        assert s < 128 and path.startswith("/soundbanks/H/")
        self.writes.append(s)
        p = bytearray(payload)
        p[24] = s - 1                                         # the Syntakt records the slot it now lives in
        if s == self.bad_slot:
            p[40] ^= 0xFF
        name = self._name_of(payload)
        self.h[s] = (name, bytes(p))

    def _name_of(self, payload):
        return self.names.get(bytes(payload[:23]) + bytes(payload[25:]), "?")

    def clear(self, path):
        self.h.pop(int(path.rsplit("/", 1)[1]), None)

    def read_banks(self):
        return [SlotRead("syntakt", 8, s, place("syntakt", 8, s), n, make_stsnd(n, p),
                         read_elektron(make_stsnd(n, p), "x.stsnd")[0].sound_hash) for s, (n, p) in sorted(self.h.items())]


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Patches"
    for t in layout.SOURCE_TYPES:
        (root / "Syntakt" / "Sources" / t).mkdir(parents=True)
    mine = root / "Syntakt" / "Sources" / "User" / "mine"
    mine.mkdir()
    for n in ["DARK PAD", "ACID BASS", "TICK"]:
        (mine / f"{n}.stsnd").write_bytes(make_stsnd(n, body(n)))
    dbp = str(tmp_path / "lib.db")
    Scanner(dbp, [(str(root), "library")]).run()
    return root, DB(dbp)


def _synth(db, h=None, bad_slot=None):
    s = FakeSyntakt(bad_slot)
    s.names = {}
    for r in db.q("SELECT name, data FROM patch WHERE synth='syntakt'"):
        n, p = ss.payload_of(bytes(r["data"]))
        s.names[p[:23] + p[25:]] = n
    for slot, (n, p) in (h or {}).items():
        s.names[p[:23] + p[25:]] = n
        s.h[slot] = (n, p)
    return s


def _mine(db):
    return {r["name"]: {"hash": r["hash"], "name": r["name"], "data": r["data"]}
            for r in db.q("SELECT hash, name, data FROM patch WHERE synth='syntakt'")}


def test_send_to_an_h_slot_keeps_what_was_there_and_undoes(lib):
    root, db = lib
    synth = _synth(db, {3: ("PANEL SOUND", body("PANEL SOUND", 3))})
    ss.remember_read(db, synth.read_banks())
    sm = ss.slot_map(db)
    assert sm[3]["state"] == "unknown" and sm[4]["state"] == "free" and 128 not in sm
    m = _mine(db)
    steps, left = ss.send_plan(db, [m["DARK PAD"], m["ACID BASS"]], 3)
    assert [(s.prog, s.why) for s in steps] == [(3, "over 'PANEL SOUND' (kept in Sources › User first)"), (4, "empty")]
    res = ss.run(synth, db, str(root), steps, "Send to slot")
    assert res.done == 2 and not res.failed and res.kept == ["H:003 PANEL SOUND"]
    assert list((root / "Syntakt" / "Sources" / "User").rglob("*PANEL SOUND*"))
    assert synth.h[3][0] == "DARK PAD" and synth.h[4][0] == "ACID BASS"
    assert ss.slot_map(db)[4]["state"] == "yours"
    # near the end: H127 is the last slot; H128 is never used
    steps, left = ss.send_plan(db, [m["DARK PAD"], m["TICK"]], 127)
    assert [s.prog for s in steps] == [127] and [p["name"] for p in left] == ["TICK"]
    # undo: H3 gets the panel sound back, H4 is empty again
    undo = ss.undo_steps(db, res.run)
    r2 = ss.run(synth, db, str(root), undo, "Undo Send to slot")
    assert r2.done == 2
    assert synth.h[3][0] == "PANEL SOUND" and 4 not in synth.h
    assert ss.slot_map(db)[4]["state"] == "free"


def test_a_changed_slot_is_skipped_and_a_bad_read_back_restores(lib):
    root, db = lib
    synth = _synth(db, bad_slot=6)
    ss.remember_read(db, synth.read_banks())
    m = _mine(db)
    steps, _ = ss.send_plan(db, [m["DARK PAD"], m["ACID BASS"], m["TICK"]], 5)
    synth.h[5] = ("NEW ON PANEL", body("NEW ON PANEL", 5))      # made on the panel after the read
    synth.names[synth.h[5][1][:23] + synth.h[5][1][25:]] = "NEW ON PANEL"
    res = ss.run(synth, db, str(root), steps, "Send to slot")
    assert res.skipped and res.skipped[0][0] == "H:005"
    assert res.done == 0 and "H:006" in res.failed
    assert 6 not in synth.h                                    # it was empty: cleared again
    assert 7 not in synth.h                                    # the run stopped


def test_sync_fills_empty_h_slots_with_the_crate(lib):
    root, db = lib
    m = _mine(db)
    synth = _synth(db, {1: ("ACID BASS", ss.payload_of(bytes(m["ACID BASS"]["data"]))[1]),
                        2: ("SOMETHING", body("SOMETHING", 2))})
    ss.remember_read(db, synth.read_banks())
    cid = ss.for_crate(db)
    db.crate_add(cid, [m["ACID BASS"]["hash"], m["DARK PAD"]["hash"], m["TICK"]["hash"]])
    steps, left, there = ss.sync_plan(db)
    assert [(s.prog, s.name) for s in steps] == [(3, "DARK PAD"), (4, "TICK")] and there == 1
    res = ss.run(synth, db, str(root), steps, "Sync")
    assert res.done == 2 and synth.h[2][0] == "SOMETHING"
    assert ss.sync_plan(db)[0] == []


def test_initialize_h_slots_to_empty_and_undo(lib):
    root, db = lib
    m = _mine(db)
    synth = _synth(db, {5: ("PANEL SOUND", body("PANEL SOUND", 5)),
                        6: ("DARK PAD", ss.payload_of(bytes(m["DARK PAD"]["data"]))[1])})
    ss.remember_read(db, synth.read_banks())
    steps, already = ss.empty_plan(db, [5, 6, 7, 128])
    assert [s.prog for s in steps] == [5, 6] and already == 1          # H7 empty; H128 never offered
    assert "kept in Sources › User first" in steps[0].why
    res = ss.run(synth, db, str(root), steps, "Initialize slots")
    assert res.done == 2 and not synth.h and res.kept == ["H:005 PANEL SOUND"]
    assert ss.slot_map(db)[5]["state"] == "free"
    ss.run(synth, db, str(root), ss.undo_steps(db, res.run), "Undo Initialize slots")
    assert synth.h[5][0] == "PANEL SOUND" and synth.h[6][0] == "DARK PAD"
