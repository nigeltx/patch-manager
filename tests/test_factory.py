"""Milestone 3: reading the synths, factory presets, green rows, backups (spec 4.7, 4.8, 5.5).
Synthetic data and fake devices only — no synth, no Drive."""
import json
import os
import sqlite3

import pytest

from patchmgr.drivers.deluge import Deluge, parse_reply
from patchmgr.drivers.sub37 import Sub37, slots_from_all
from patchmgr.drivers.take5 import Take5
from patchmgr.library import factory, search
from patchmgr.library.db import DB
from patchmgr.library.formats import (make_stsnd, pack_ms, place, read_elektron, read_file, s37_name, s37_split_all,
                                      unpack_ms)
from patchmgr.library.scanner import Scanner
from tests.test_library import t5_program


# ---------------------------------------------------------------- Sub 37 ALL PRESETS (bench test 18)
def s37_name_bytes(name):
    raw = name.encode("latin-1").ljust(20)
    out = bytearray()
    for k in range(0, 20, 2):
        w = raw[k] << 8 | raw[k + 1]
        out += bytes([0x40 | (w >> 12), (w >> 6) & 0x3F, w & 0x3F])
    return bytes(out)


def s37_record(name, seed):
    data = bytearray((seed * 7 + i) % 64 for i in range(745))
    data[:16] = bytes(16)
    data[16:46] = s37_name_bytes(name)
    return bytes(data) + bytes([seed % 128, 1, 2])          # 745 data + 3 checksum = 748


def s37_all(names):
    body = bytearray()
    for n, name in enumerate(names):
        rec = bytearray(s37_record(name, n))
        rec[7] = 0x60 if n == 0 else 0x00
        body += rec + bytes(48)
    return b"\xF0\x04\x0C\x01\x07\x00\x00" + bytes(body) + bytes(15) + b"\x01\xF7"


NAMES = [f"Factory {i}" for i in range(144)] + ["TR01 Bass", "PS01 Roll Bass"] + ["Init Preset"] * 110


def test_sub37_all_presets_splits_into_slots():
    allp = s37_all(NAMES)
    assert len(allp) == 203800
    pieces = s37_split_all(allp)
    assert len(pieces) == 256 and all(len(p) == 756 for p in pieces)
    assert pieces[148][5:7] == bytes([0x01, 0x14]) and pieces[148][14] == 0x60
    assert s37_name(pieces[144]) == "TR01 Bass"
    ps = read_file(allp, "ALL PRESETS.syx", None)
    sounds = [p for p in ps if p.kind == "sound"]
    assert len(sounds) == 256 and ps[-1].kind == "backup"
    assert (sounds[144].bank, sounds[144].slot, sounds[144].name) == (10, 1, "TR01 Bass")
    assert sounds[255].blank and not sounds[0].blank
    # the same slot as a single CUR PRESET dump has the same identity
    single = read_file(pieces[0], "cur.syx", None)[0]
    assert single.sound_hash == sounds[0].sound_hash


def test_sub37_waits_for_all_presets():
    class Dev:
        def wait_for(self, test, timeout, progress=None, cancelled=None):
            m = s37_all(NAMES)
            return m if test(m) else None
    s = Sub37(Dev())
    data = s.wait_all_presets(timeout=1)
    reads = slots_from_all(data)
    assert len(reads) == 256 and reads[0].place == "1-1" and reads[255].place == "16-16"


# ---------------------------------------------------------------- Take 5 reads
class FakeTake5:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.sent = []

    def request(self, msg, test, timeout=2.0):
        self.sent.append(bytes(msg))
        if msg[3] == 0x05:
            bank, prog = msg[4], msg[5]
            if (bank + 1, prog + 1) in self.missing:
                return None
            name = f"F{bank - 15}P{prog + 1}" if bank >= 16 else f"U{bank + 1}P{prog + 1}"
            reply = t5_program(bank, prog, name)
            return reply if test(reply) else None
        return None


def test_take5_reads_user_and_factory_banks():
    t = Take5(FakeTake5())
    reads = t.read_slots(banks=[1, 17])
    assert len(reads) == 32
    assert reads[0].place == "U1-01" and reads[16].place == "F1-01" and reads[16].name == "F1P1"
    assert t.dev.sent[0] == bytes([0xF0, 0x01, 0x35, 0x05, 0, 0, 0xF7])


def test_take5_read_stops_when_a_program_never_comes():
    with pytest.raises(RuntimeError):
        Take5(FakeTake5(missing={(1, 3)})).read_slots(banks=[1])


# ---------------------------------------------------------------- factory banks and the review
def test_parse_banks():
    assert factory.parse_banks("take5", "F1-F16") == set(range(17, 33))
    assert factory.parse_banks("take5", "U1-U8, F1-F16") == set(range(1, 9)) | set(range(17, 33))
    assert factory.parse_banks("sub37", "1-9") == set(range(1, 10))
    assert factory.parse_banks("syntakt", "A") == {1}
    assert factory.parse_banks("syntakt", "A-B") == {1, 2}
    for synth, bad in (("sub37", "17"), ("syntakt", "J"), ("take5", "U20"), ("sub37", "9-1")):
        with pytest.raises(ValueError):
            factory.parse_banks(synth, bad)


def test_review_ticks_factory_banks_and_makers_set_only():
    reads = slots_from_all(s37_all(NAMES))
    rows = factory.review(reads, factory.parse_banks("sub37", "1-9"), maker={reads[145].hash})
    st = {r.read.place: (r.status, r.tick) for r in rows}
    assert st["1-1"] == ("bank", True) and st["9-16"] == ("bank", True)
    assert st["10-1"] == ("yours", False)                      # TR01 Bass: bank 10 is Nigel's own
    assert st["10-2"] == ("maker", True)                       # matches a maker's file wherever it sits
    assert st["16-16"] == ("blank", False)                     # Init Preset


def test_review_treats_a_repeated_sound_as_blank():
    reads = [factory.SlotRead("take5", 1, p, place("take5", 1, p), "Same", b"", "h") for p in range(1, 10)]
    assert all(r.status == "blank" for r in factory.review(reads, {1}, set()))


# ---------------------------------------------------------------- backup → library → apply → search
def test_initialize_end_to_end(tmp_path):
    root = tmp_path / "Patches"
    (root / "Take 5" / "Factory" / "Set 2").mkdir(parents=True)
    # the maker's file holds U2-01's sound
    (root / "Take 5" / "Factory" / "Set 2" / "set2.syx").write_bytes(t5_program(1, 0, "U2P1"))
    dbp = str(tmp_path / "lib.db")
    Scanner(dbp, [(str(root), "library")]).run()
    t = Take5(FakeTake5())
    reads = t.read_slots(banks=[1, 2, 17])
    folder = factory.backup_folder(str(root), "take5", "Factory read")
    factory.write_backup(folder, "take5", reads)
    assert sorted(os.listdir(folder)) == ["F01.syx", "README.txt", "U01.syx", "U02.syx"]
    assert os.path.dirname(folder) == str(root / "Take 5" / "Backups")      # beside Sources and Patches
    Scanner(dbp, [(str(root), "library")]).run()
    db = DB(dbp)
    assert db.q("SELECT 1 FROM source WHERE type='backup'") == []         # a backup is never a source
    rows = factory.review(reads, factory.parse_banks("take5", "F1-F16"), db.maker_hashes("take5"), db.maker_pack)
    assert sum(r.tick for r in rows) == 17                    # bank F1 plus U2-01 from the maker's file
    # as the dialog does: the sounds go into Sources first, then they are marked
    items = [(r.read.hash, r.read.place, r.read.name, r.read.data, False) for r in rows]
    kept = factory.keep_in_sources(db, str(root), "take5", items, {r.read.hash for r in rows if r.tick})
    assert kept["n_factory"] == 17 and kept["n_user"] == 31   # U1 and U2 less the maker's U2-01
    Scanner(dbp, [(str(root), "library")]).run()
    n = factory.apply(db, "take5", rows, "Take 5", "2.1.0", folder)
    assert n == 17
    fac = db.q(*search.build("factory", {}))
    assert {r["name"] for r in fac} == {f"F1P{p}" for p in range(1, 17)} | {"U2P1"}
    on = db.q(*search.build("onsynth", {}))
    assert len(on) == 48
    row = db.q(*search.build("U1P3", {}))[0]
    assert row["on_synth"] and not row["is_factory"] and row["places"] == "U1-03"
    assert db.last_init("take5")["marked_factory"] == 17
    # unticking a maker's sound overrides the maker's set for it
    for r in rows:
        r.tick = r.read.name != "U2P1" and r.tick
    factory.apply(db, "take5", rows, "Take 5", "2.1.0", folder)
    assert "U2P1" not in {r["name"] for r in db.q(*search.build("factory", {}))}


def test_old_library_gets_new_columns(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE patch (id INTEGER PRIMARY KEY, synth TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT "
                      "NULL UNIQUE, data BLOB NOT NULL, fmt TEXT, kind TEXT NOT NULL DEFAULT 'sound', first_seen REAL);"
                      "CREATE TABLE slot_state (synth TEXT, bank INTEGER, slot INTEGER, hash TEXT, read_at REAL, "
                      "PRIMARY KEY(synth, bank, slot));")
    con.commit(); con.close()
    db = DB(str(p))
    assert {"factory", "blank"} <= {r[1] for r in db.q("PRAGMA table_info(patch)")}
    assert {"place", "info"} <= {r[1] for r in db.q("PRAGMA table_info(slot_state)")}


# ---------------------------------------------------------------- Syntakt and Deluge
def test_stsnd_from_the_pool_has_the_same_identity_as_a_pack_file():
    payload = b"\xAC\x11\xD3\x03" + bytes(range(100))
    a = read_elektron(make_stsnd("BLOMP", payload), "a.stsnd")[0]
    b = read_elektron(make_stsnd("BLOMP", payload), "b.stsnd")[0]
    assert a.sound_hash == b.sound_hash and a.name == "BLOMP"
    assert place("syntakt", 1, 2) == "A:002"


class FakeDeluge:
    """Answers the Deluge file protocol from an in-memory card."""

    def __init__(self, files):
        self.files = files            # {path: bytes}
        self.open = {}

    def _reply(self, mid, obj, data=None):
        body = json.dumps(obj).encode()
        return b"\xF0\x00\x21\x7B\x01\x05" + bytes([mid]) + body + ((b"\x00" + pack_ms(data)) if data else b"") + b"\xF7"

    def request(self, msg, test, timeout=2.0):
        msg = bytes(msg)
        if msg[1] == 0x7E:
            r = bytes([0xF0, 0x7E, 0x7F, 0x06, 0x02, 0x00, 0x21, 0x7B, 0x01, 0, 0, 0, 1, 3, 0, 0, 0xF7])
            return r if test(r) else None
        mid = msg[6]
        cut = msg.find(b"\x00", 7)
        cmd = json.loads(msg[7:cut if cut > 0 else -1])
        (name, args), = cmd.items()
        if name == "session":
            r = self._reply(0, {"^session": {"sid": 1, "midMin": 9, "midMax": 15}})
        elif name == "dir":
            p = args["path"].rstrip("/")
            kids = sorted({k[len(p) + 1:].split("/")[0] for k in self.files if k.startswith(p + "/")})
            items = [{"name": n, "size": len(self.files.get(f"{p}/{n}", b"")), "date": 1, "time": 2,
                      "attr": 0 if f"{p}/{n}" in self.files else 0x10} for n in kids]
            r = self._reply(mid, {"^dir": {"list": items[args["offset"]:args["offset"] + args["lines"]], "err": 0}})
        elif name == "open":
            fid = len(self.open) + 1
            self.open[fid] = args["path"]
            r = self._reply(mid, {"^open": {"fid": fid, "size": len(self.files[args["path"]]), "err": 0}})
        elif name == "read":
            d = self.files[self.open[args["fid"]]][args["addr"]:args["addr"] + args["size"]]
            r = self._reply(mid, {"^read": {"fid": args["fid"], "addr": args["addr"], "size": len(d), "err": 0}}, d)
        else:
            r = self._reply(mid, {"^" + name: {"err": 0}})
        return r if test(r) else None


def xml(name, n=900):
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n<sound name="' + name.encode() + b'">' + b"x" * n + b"</sound>\n")


def test_deluge_reads_every_preset_over_usb():
    files = {f"/SYNTHS/{i:03d} Preset {i}.XML": xml(f"P{i}", 700 + i) for i in range(23)}
    files["/SYNTHS/PACK/Deep Bass.XML"] = xml("Deep", 1300)
    files["/SYNTHS/_AUDITION/Trial.XML"] = xml("Trial")
    d = Deluge(FakeDeluge(files))
    assert d.connect() == "1.3.0"
    reads = d.read_slots()
    assert len(reads) == 24                                   # paged listing, subfolder read, _AUDITION skipped
    deep = next(r for r in reads if r.place == "SYNTHS/PACK/Deep Bass.XML")
    assert deep.data == files["/SYNTHS/PACK/Deep Bass.XML"]
    again = d.read_slots(known={r.place: (r.info, r.hash, r.name) for r in reads})
    assert [r.hash for r in again] == [r.hash for r in reads] and all(r.data == b"" for r in again)


def test_deluge_reply_parsing():
    data = bytes(range(256)) * 2
    m = b"\xF0\x00\x21\x7B\x01\x05\x09" + b'{"^read": {"fid": 1,\n "err": 0,}}' + b"\x00" + pack_ms(data) + b"\xF7"
    mid, obj, d = parse_reply(m)
    assert mid == 9 and obj["^read"]["fid"] == 1 and d[:len(data)] == data


def test_read_worker_backs_up_and_remembers(tmp_path):
    from patchmgr.ui.synths import ReadWorker

    class Sess:
        drivers = {"take5": Take5(FakeTake5())}

        def ready(self, k):
            return True
    root = tmp_path / "Patches"; root.mkdir()
    dbp = str(tmp_path / "lib.db")
    DB(dbp)
    w = ReadWorker("backup", "take5", Sess(), dbp, str(root))
    res = w._run()
    assert len(res["reads"]) == 512 and os.path.isdir(res["folder"])
    db = DB(dbp)
    assert len(db.slot_rows("take5")) == 512
    assert res["folder"].startswith(str(root / "Take 5" / "Backups"))
    assert db.one("SELECT COUNT(*) FROM patch WHERE synth='take5'") == 0        # a backup is not read into the library


def test_syntakt_reads_banks_and_skips_unchanged_slots():
    from patchmgr.drivers.syntakt import Syntakt
    s = Syntakt(None)
    pool = {"A": [{"name": "BLOMP", "slot": 2, "size": 198}, {"name": "JAB", "slot": 7, "size": 200}], "H": []}
    reads_done = []
    s.list = lambda path: pool.get(path.rsplit("/", 1)[1], [])
    s.read = lambda path: (reads_done.append(path), b"\xAC\x11\xD3\x03" + path.encode())[1]
    first = s.read_banks("ABH")
    assert [r.place for r in first] == ["A:002", "A:007"] and len(reads_done) == 2
    known = {(r.bank, r.slot): (r.info, r.hash, r.name) for r in first}
    pool["A"][1] = {"name": "JAB 2", "slot": 7, "size": 201}
    second = s.read_banks("ABH", known=known)
    assert len(reads_done) == 3 and second[0].hash == first[0].hash and second[1].name == "JAB 2"


def test_deluge_port_pairs_put_the_plain_ports_first():
    from patchmgr.drivers.deluge import port_pairs
    ins = ["KeyLab 61 mk3 MIDI", "MIDIIN2 (Deluge) 7", "Deluge 6", "MIDIIN3 (Deluge) 8"]
    outs = ["MIDIOUT2 (Deluge) 8", "Deluge 7", "Take 5 5"]
    pairs = port_pairs(ins, outs)
    assert pairs[0] == ("Deluge 6", "Deluge 7") and len(pairs) == 6


def test_deluge_preflight_finds_the_port_that_answers(monkeypatch):
    from patchmgr.drivers import deluge as dl
    from patchmgr.midi import hub as midihub
    card = FakeDeluge({"/SYNTHS/A.XML": xml("A")})

    class Dev:
        def __init__(self, key, i, o, *a):
            self.key, self.in_name, self.out_name = key, i, o

        def request(self, msg, test, timeout=2.0):
            if self.in_name != "MIDIIN2 (Deluge)":             # on this PC only the second port answers
                return None
            if bytes(msg) == dl.PING:
                r = dl.HDR + b"\x7F\x00\xF7"
                return r if test(r) else None
            return card.request(msg, test, timeout)

        def close(self):
            pass

    class Hub:
        devices = {}

        def close(self, k):
            self.devices.pop(k, None)
    monkeypatch.setattr(midihub, "list_ports", lambda: (["Deluge", "MIDIIN2 (Deluge)"], ["Deluge"]))
    monkeypatch.setattr(midihub, "Device", Dev)
    logs = []
    d = dl.Deluge(None, hub=Hub(), log=logs.append)
    p = d.preflight()
    assert p.ok and d.dev.in_name == "MIDIIN2 (Deluge)" and p.os_version == "1.3.0"
    assert any("answers on" in t for t in logs)
    monkeypatch.setattr(midihub, "list_ports", lambda: (["Deluge"], ["Deluge"]))
    p = dl.Deluge(None, hub=Hub(), log=logs.append).preflight()
    assert not p.ok and "Tried: Deluge / Deluge: no answer" in p.lines[0][1]


def test_syntakt_factory_banks_default_to_a_to_g(tmp_path):
    db = DB(str(tmp_path / "l.db"))
    assert factory.parse_banks("syntakt", db.factory_banks("syntakt")) == set(range(1, 8))
    db.set_setting("factory_banks_syntakt", "A")              # saved by 0.4.0/0.4.1
    assert db.factory_banks("syntakt") == "A-G"


def test_syntakt_identity_ignores_the_slot_but_not_the_name():
    """26 Nov: G020 and its copy in H127 differed only in bytes 23-24 (bank, slot − 1)."""
    body = bytearray(b"\xAC\x11\xD3\x03" + bytes(range(60)))
    g020 = bytes(body[:23]) + bytes([6, 19]) + bytes(body[25:])
    h127 = bytes(body[:23]) + bytes([7, 126]) + bytes(body[25:])
    a = read_elektron(make_stsnd("BASS SLAP MY B", g020), "a.stsnd")[0]
    b = read_elektron(make_stsnd("BASS SLAP MY B", h127), "b.stsnd")[0]
    c = read_elektron(make_stsnd("MY SLAP", h127), "c.stsnd")[0]
    edited = bytearray(h127); edited[40] ^= 1
    d = read_elektron(make_stsnd("BASS SLAP MY B", bytes(edited)), "d.stsnd")[0]
    assert a.sound_hash == b.sound_hash                       # a copy in another slot: the same sound
    assert c.sound_hash != a.sound_hash                       # renamed: yours
    assert d.sound_hash != a.sound_hash                       # edited: yours


def test_old_syntakt_identities_are_migrated(tmp_path):
    p = str(tmp_path / "lib.db")
    db = DB(p)
    data = make_stsnd("BLOMP", b"\xAC\x11\xD3\x03" + bytes(range(60)))
    db.con.execute("INSERT INTO patch(synth, name, hash, data, fmt, kind) VALUES('syntakt','BLOMP','oldhash',?, 'stsnd','sound')",
                   (data,))
    db.con.execute("INSERT INTO meta(hash, rating) VALUES('oldhash', 4)")
    db.con.execute("INSERT INTO type_user(hash, types) VALUES('oldhash', ',Bass,')")
    db.con.execute("UPDATE setting SET value='1' WHERE key='identity_syntakt'")
    db.con.commit()
    db2 = DB(p)
    new = read_elektron(data, "x.stsnd")[0].sound_hash
    assert db2.one("SELECT hash FROM patch WHERE name='BLOMP'") == new
    assert db2.one("SELECT rating FROM meta WHERE hash=?", (new,)) == 4
    assert db2.one("SELECT types FROM type_user WHERE hash=?", (new,)) == ",Bass,"


def test_get_never_keeps_a_copy_of_a_factory_slot(tmp_path):
    """A user slot holding an exact copy of a factory-bank sound is not yours, even before any factory source exists."""
    dbp = str(tmp_path / "lib.db")
    db = DB(dbp)
    t = Take5(FakeTake5())
    reads = t.read_slots(banks=[1, 17])
    copy = reads[16]                                          # F1-01
    reads[0] = factory.SlotRead("take5", 1, 1, "U1-01", copy.name, copy.data, copy.hash)
    db.set_slots("take5", [{"bank": r.bank, "slot": r.slot, "hash": r.hash} for r in reads])
    new = factory.new_sounds(db, "take5", reads, {17})
    assert len(new) == 15 and copy.hash not in {r.hash for r in new}
