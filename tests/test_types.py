"""BY TYPE (0.5.0): guesses from names, tags and folders; the user's choice wins; search and counts."""
from patchmgr.library import ptypes, search
from patchmgr.library.db import DB
from patchmgr.library.scanner import Scanner
from tests.test_library import t5_program


def test_guesses_from_names():
    g = lambda n, **k: ptypes.guess(n, **k)[0]
    assert g("LD Super Saw 02") == ["Lead"]
    assert g("BS_Deep-Reese") == ["Bass"]
    assert g("Soft Pad") == ["Pad"]
    assert g("PD Blonk") == [ptypes.UNKNOWN]               # Moog's "PD" = public domain, not pad
    assert g("808 Bass Drum") == ["Kick"]
    assert g("Acid Bass Seq") == ["Bass", "Arp/Seq"]
    assert g("Rest Area 51") == [ptypes.UNKNOWN]
    assert g("Thing", tags=["kick"]) == ["Kick"]
    t, why = ptypes.guess("Thing", folders=["Take 5", "Paid", "Pack", "Basses"])
    assert t == ["Bass"] and "Basses" in why
    assert ptypes.guess("Thing", folders=["Sub 37", "Factory"])[0] == [ptypes.UNKNOWN]
    assert ptypes.find("arp") == "Arp/Seq" and ptypes.find("drums") == "Drums" and ptypes.find("zzz") is None


def test_scan_guesses_and_user_choice(tmp_path):
    root = tmp_path / "Patches"
    (root / "Take 5" / "Paid" / "Pack").mkdir(parents=True)
    (root / "Take 5" / "Paid" / "Pack" / "bank.syx").write_bytes(
        b"".join(t5_program(0, p, n) for p, n in enumerate(["LD Saw", "Fat Bass", "Warm Pad", "Mystery", "Kick 1"])))
    dbp = str(tmp_path / "l.db")
    Scanner(dbp, [(str(root), "library")]).run()
    db = DB(dbp)
    c = db.type_counts()
    assert c["Lead"] == 1 and c["Bass"] == 1 and c["Pad"] == 1 and c[ptypes.UNKNOWN] == 1 and c["Drums"] == 1
    names = lambda q, scope=None: sorted(r["name"] for r in db.q(*search.build(q, scope)))
    assert names("type:bass") == ["Fat Bass"]
    assert names("", {"ptype": "Drums"}) == ["Kick 1"]
    assert names("-type:unknown") == ["Fat Bass", "Kick 1", "LD Saw", "Warm Pad"]
    h = db.q(*search.build("Mystery"))[0]["hash"]
    db.set_user_type([h], ["Pluck", "Arp/Seq"])
    row = db.q(*search.one(h))[0]
    assert ptypes.unpack(row["ptypes"]) == ["Pluck", "Arp/Seq"] and row["ptype_user"]
    Scanner(dbp, [(str(root), "library")]).run()               # a rescan keeps the user's choice
    assert names("type:pluck") == ["Mystery"]
    db.clear_user_type([h])
    assert names("type:unknown") == ["Mystery"]
