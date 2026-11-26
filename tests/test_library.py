"""Tests for the library core. Run:  python -m pytest tests   (from the App folder).
They build small synthetic files, so they need no synth and no Drive access."""
import io
import json
import os
import zipfile

import pytest

from patchmgr.library import search
from patchmgr.library.db import DB
from patchmgr.library.formats import (FormatError, pack_ms, read_file, split_sysex, t5_to_edit, unpack_ms)
from patchmgr.library.scanner import Scanner, classify


def t5_program(bank, prog, name):
    raw = bytearray(4096)
    raw[195:195 + len(name)] = name.encode()
    packed = pack_ms(bytes(raw))
    body = packed + bytes(4688 - len(packed))
    return b"\xF0\x01\x35\x02" + bytes([bank, prog]) + body + b"\xF7"


def s37_preset(seed):
    data = bytes((seed + i) % 128 for i in range(756 - 11))
    return b"\xF0\x04\x0C\x04\x07\x00\x00" + data + b"\x00\x00\x00\xF7"


def stsnd(name, tags):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("manifest.json", json.dumps({"FileType": "Sound", "Payload": name, "MetaInfo": {"Tags": tags}}))
        z.writestr(name, b"\x01\x02" + name.encode())
    return b.getvalue()


def test_pack_roundtrip():
    raw = bytes(range(256)) * 3
    assert unpack_ms(pack_ms(raw))[:len(raw)] == raw


def test_take5_bank_and_names():
    data = t5_program(0, 0, "Matterhorn") + t5_program(0, 1, "Batian")
    ps = read_file(data, "x.syx", None)
    assert [p.name for p in ps] == ["Matterhorn", "Batian"]
    assert (ps[1].bank, ps[1].slot) == (1, 2)
    edit = read_file(t5_to_edit(ps[0].data), "e.syx", None)[0]
    assert edit.fmt == "edit buffer" and edit.sound_hash == ps[0].sound_hash


def test_sub37_identity_ignores_header():
    a = s37_preset(5)
    b = bytearray(a); b[3] = 0x02; b[5] = 0x01                  # stored-preset header, other slot
    pa, pb = read_file(a, "PD Blonk.syx", None)[0], read_file(bytes(b), "x.syx", None)[0]
    assert pa.name == "PD Blonk" and pa.sound_hash == pb.sound_hash


def test_problems_are_whole_file():
    with pytest.raises(FormatError):
        read_file(t5_program(0, 0, "A")[:-1], "cut.syx", None)          # no F7
    with pytest.raises(FormatError):
        read_file(b"junk", "j.syx", None)
    with pytest.raises(FormatError):
        split_sysex(b"\xF0\x01\x90\xF7")                                # status byte inside


def test_syntakt_tags():
    p = read_file(stsnd("RAINPAD", ["PAD", "DARK"]), "G003 RAINPAD.stsnd", None)[0]
    assert p.name == "RAINPAD" and p.tags == ["Pad", "Dark"]


def test_classify():
    assert classify(["Take 5", "Paid", "Mountains", "b1.syx"]) == ("take5", "paid", "Mountains")
    assert classify(["Sub 37", "Free", "x.syx"]) == ("sub37", "free", None)
    assert classify(["Sub 37", "loose.syx"]) == ("sub37", "unsorted", None)


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Patches"
    (root / "Take 5" / "Paid" / "Mountains").mkdir(parents=True)
    (root / "Take 5" / "Factory" / "Set").mkdir(parents=True)
    (root / "Sub 37" / "Paid" / "Pack" / "WAV" / "Dry").mkdir(parents=True)
    (root / "Syntakt" / "Paid" / "Skin").mkdir(parents=True)
    bank = t5_program(0, 0, "Matterhorn") + t5_program(0, 1, "Batian")
    (root / "Take 5" / "Paid" / "Mountains" / "Bank 1.syx").write_bytes(bank)
    (root / "Take 5" / "Factory" / "Set" / "All.syx").write_bytes(bank + t5_program(0, 2, "Talo"))
    (root / "Sub 37" / "Paid" / "Pack" / "001 AT Bass - Odds.syx").write_bytes(s37_preset(1))
    (root / "Sub 37" / "Paid" / "Pack" / "002 AT Lead - Moon.syx").write_bytes(s37_preset(2))
    (root / "Sub 37" / "Paid" / "Pack" / "WAV" / "Dry" / "001 AT Bass - Odds 122 g#m DRY.wav").write_bytes(b"RIFF")
    (root / "Sub 37" / "Paid" / "Pack" / "readme.pdf").write_bytes(b"%PDF")
    (root / "Syntakt" / "Paid" / "Skin" / "G001 SHAKE.stsnd").write_bytes(stsnd("SHAKE", ["HI-HAT"]))
    (root / "Take 5" / "Paid" / "broken.syx").write_bytes(b"\xF0\x01\x35\x02")
    db_path = str(tmp_path / "t.db")
    Scanner(db_path, [(str(root), "library")]).run()
    return DB(db_path), root, db_path


def test_scan_dedupes_and_links(lib):
    db, root, _ = lib
    assert db.one("SELECT COUNT(*) FROM patch WHERE synth='take5'") == 3           # Matterhorn, Batian, Talo
    m = db.q("SELECT id FROM patch WHERE name='Matterhorn'")[0][0]
    assert len(db.origins(m)) == 2                                                  # Paid bank + Factory set
    h = db.one("SELECT hash FROM patch WHERE name='001 AT Bass - Odds'")
    assert [p["kind"] for p in db.previews(h)] == ["pack dry"]
    assert [r["rel"] for r in db.problems()] == ["Take 5/Paid/broken.syx"]
    assert db.one("SELECT status FROM source_file WHERE rel LIKE '%readme.pdf'") == "skipped"
    assert {t["name"] for t in db.tags_for(h)} == {"bass"}                          # guessed, unconfirmed


def test_user_data_survives_rescan_and_deletion(lib):
    db, root, db_path = lib
    h = db.one("SELECT hash FROM patch WHERE name='Batian'")
    db.set_rating(h, 5); db.set_tag(h, "pad"); cid = db.new_crate("Keepers"); db.crate_add(cid, [h])
    (root / "Take 5" / "Paid" / "Mountains" / "Bank 1.syx").unlink()
    Scanner(db_path, [(str(root), "library")]).run()
    assert db.meta(h)["rating"] == 5
    assert db.crates_of(h) == ["Keepers"]
    pid = db.one("SELECT id FROM patch WHERE hash=?", (h,))
    alerts = db.source_alerts()                     # a pack file gone from Sources is flagged, not forgotten (spec 4.4)
    assert [(a["state"], a["rel"].split("/")[-1]) for a in alerts] == [("missing", "Bank 1.syx")]
    assert len(db.origins(pid)) == 2
    db.accept_missing(alerts[0]["id"])
    assert len(db.origins(pid)) == 1                                                # Factory copy remains


@pytest.mark.parametrize("q,n", [("", 6), ("synth:take5", 3), ("tag:bass", 1), ("preview", 1), ("dupe", 2),
                                 ("source:factory", 3), ("-synth:take5", 3), ("odds", 1), ('pack:"pack"', 2),
                                 ("rating:0", 6), ("unheard", 6)])
def test_search(lib, q, n):
    db, _, _ = lib
    sql, args = search.build(q)
    assert len(db.q(sql, args)) == n


def test_search_errors():
    with pytest.raises(search.QueryError):
        search.build("synth:juno")
    with pytest.raises(search.QueryError):
        search.build("rating:lots")
