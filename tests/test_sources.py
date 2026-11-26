"""Sources and Patches (spec 4.4, 4.5, 4.11): Reorganize, zips, fingerprints, Rebuild."""
import io
import os
import zipfile

import pytest

from patchmgr.library import layout, rebuild
from patchmgr.library.db import DB
from patchmgr.library.formats import read_file
from patchmgr.library.scanner import Scanner
from tests.test_library import s37_preset, stsnd, t5_program


def scan(db_path, root):
    return Scanner(db_path, [(str(root), "library")]).run()


def reorg(root, db_path):
    """Scan the old layout (as 0.5 had), then Reorganize with the database, as the app does."""
    scan(db_path, root)
    return layout.reorganize_apply(str(root), layout.reorganize_plan(str(root)), db=DB(db_path))


@pytest.fixture
def old(tmp_path):
    """The library as it is on Drive today: Free/Factory/Paid straight under each synth, factory reads in Backups,
    and the Sub 37's Sources\\Free already moved by hand."""
    root = tmp_path / "Studio" / "Patches"
    t5 = root / "Take 5"
    (t5 / "Factory").mkdir(parents=True)
    (t5 / "Factory" / "Set2 FACTORY.syx").write_bytes(t5_program(16, 0, "Rest Area 51") + t5_program(16, 1, "Dimension 5"))
    (t5 / "Paid" / "Mountains").mkdir(parents=True)
    (t5 / "Paid" / "Mountains" / "Bank 1.syx").write_bytes(t5_program(0, 0, "Matterhorn Pad") + t5_program(0, 1, "Batian Bass"))
    (t5 / "Free").mkdir()
    s37 = root / "Sub 37"
    (s37 / "Sources" / "Free" / "All_Sounds").mkdir(parents=True)
    (s37 / "Sources" / "Free" / "All_Sounds" / "PD Blonk.syx").write_bytes(s37_preset(5))
    (s37 / "Factory").mkdir(parents=True)
    st = root / "Syntakt"
    (st / "Factory" / "Twin Shot").mkdir(parents=True)
    (st / "Factory" / "Twin Shot" / "01.wav").write_bytes(b"RIFF....WAVE")
    (st / "Paid").mkdir()
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("SkinTakt/KICK ONE.stsnd", stsnd("KICK ONE", ["kick"]))
        z.writestr("SkinTakt/LEAD TWO.stsnd", stsnd("LEAD TWO", ["lead"]))
        z.writestr("SkinTakt/demo.wav", b"RIFF")
    (st / "Paid" / "SkinTakt.zip").write_bytes(zb.getvalue())
    bk = root / "_Patch Manager" / "Backups" / "Take 5" / "2026-11-22 2122 Factory read"
    bk.mkdir(parents=True)
    (bk / "U01.syx").write_bytes(t5_program(0, 0, "Matterhorn Pad") + t5_program(0, 2, "My Own Lead"))
    cl = tmp_path / "Studio" / "Software" / "Studio Cleanup"
    cl.mkdir(parents=True)
    (cl / "Cleanup list.txt").write_text("# list\n", encoding="utf-8")
    return root, tmp_path


def test_reorganize_moves_once_and_lists_old_folders(old):
    root, tmp = old
    db_path = str(tmp / "lib.db")
    scan(db_path, root)
    db = DB(db_path)
    h = db.one("SELECT hash FROM patch WHERE name='Matterhorn Pad'")
    db.set_factory([h], "read")                         # as if Initialize had ticked it
    plan = layout.reorganize_plan(str(root))
    moves = {(os.path.relpath(m.src, root), os.path.relpath(m.dst, root)) for m in plan.moves}
    j = os.path.join
    assert (j("Take 5", "Factory", "Set2 FACTORY.syx"), j("Take 5", "Sources", "Factory", "Set2 FACTORY.syx")) in moves
    assert (j("Take 5", "Paid", "Mountains"), j("Take 5", "Sources", "Paid", "Mountains")) in moves
    assert (j("Syntakt", "Factory", "Twin Shot"), j("Syntakt", "Samples", "Twin Shot")) in moves
    assert [os.path.basename(f) for _, f in plan.splits] == ["2026-11-22 2122 Factory read"]
    assert not any("All_Sounds" in s for s, _ in moves)                 # already moved by hand: left alone
    moved, errors, listed = layout.reorganize_apply(str(root), plan, db=db)
    assert moved == len(plan.moves) + 1 and not errors
    fr = root / "Take 5" / "Sources" / "Factory" / "2026-11-22 2122 Factory read"
    ur = root / "Take 5" / "Sources" / "User" / "2026-11-22 2122 Take 5 read"
    assert sorted(p.name for p in fr.iterdir()) == ["U1-01 Matterhorn Pad.syx"]      # factory sounds only
    assert sorted(p.name for p in ur.iterdir()) == ["U1-03 My Own Lead.syx"]         # yours, in no other source
    assert (root / "_Patch Manager" / "Backups" / "Take 5" / "2026-11-22 2122 Factory read").is_dir()  # read kept
    assert (root / "Sub 37" / "Sources" / "User").is_dir() and (root / "Take 5" / "Patches").is_dir()
    cl = (tmp / "Studio" / "Software" / "Studio Cleanup" / "Cleanup list.txt").read_text(encoding="utf-8")
    assert "Patches\\Take 5\\Factory" in cl.replace("/", "\\") and "Patches\\Take 5\\Free" in cl.replace("/", "\\")
    assert layout.reorganize_plan(str(root)).empty                       # a second run has nothing to do


def test_zip_read_in_place_and_fingerprints(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)
    db = DB(db_path)
    names = {r["name"] for r in db.q("SELECT name FROM patch WHERE synth='syntakt'")}
    assert names == {"KICK ONE", "LEAD TWO"}
    z = root / "Syntakt" / "Sources" / "Paid" / "SkinTakt.zip"
    assert db.one("SELECT sha1 FROM source_file WHERE path=?", (str(z),))
    assert (root / "_Patch Manager" / "sources.json").is_file()
    # a pack file changed behind the app's back: flagged, old sounds kept
    f = root / "Take 5" / "Sources" / "Paid" / "Mountains" / "Bank 1.syx"
    f.write_bytes(t5_program(0, 0, "Something Else"))
    os.utime(f, (1, 1))
    p = scan(db_path, root)
    alerts = db.source_alerts()
    assert p.changed == 1 and [a["state"] for a in alerts] == ["changed"]
    assert db.one("SELECT COUNT(*) FROM patch WHERE name='Batian Bass'") == 1
    assert db.one("SELECT COUNT(*) FROM patch WHERE name='Something Else'") == 0
    db.accept_change(alerts[0]["id"])
    scan(db_path, root)
    assert db.one("SELECT COUNT(*) FROM patch WHERE name='Something Else'") == 1 and not db.source_alerts()


def test_moved_file_is_not_missing(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)                                   # same bytes, new places
    assert DB(db_path).source_alerts() == []


def test_rebuild_writes_each_sound_once_by_type(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)
    db = DB(db_path)
    pl = rebuild.plan(db, str(root))
    res = rebuild.apply(db, pl)
    assert not res["errors"]
    t5 = root / "Take 5" / "Patches"
    files = sorted(str(p.relative_to(t5)).replace(os.sep, "/") for p in t5.rglob("*.syx"))
    # Matterhorn Pad is in Paid and in the factory read: one file. Names come from the first of Factory, Paid …
    assert files == ["Bass/Batian Bass.syx", "Lead/My Own Lead.syx", "Pad/Matterhorn Pad.syx",
                     "Unknown/Dimension 5.syx", "Unknown/Rest Area 51.syx"]
    one = read_file((t5 / "Pad" / "Matterhorn Pad.syx").read_bytes(), "x.syx", None)
    assert len(one) == 1 and len(one[0].data) == 4695
    st = root / "Syntakt" / "Patches"
    assert sorted(p.name for p in st.rglob("*.stsnd")) == ["KICK ONE.stsnd", "LEAD TWO.stsnd"]
    assert (st / "Kick" / "KICK ONE.stsnd").is_file()
    s37 = (root / "Sub 37" / "Patches" / "Unknown" / "PD Blonk.syx").read_bytes()
    assert len(s37) == 756 and s37[3] == 0x04
    assert rebuild.plan(db, str(root)).empty                         # rebuilding again changes nothing


def test_rebuild_follows_type_choice_and_handles_hand_files(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)
    db = DB(db_path)
    rebuild.apply(db, rebuild.plan(db, str(root)))
    h = db.one("SELECT hash FROM patch WHERE name='Dimension 5'")
    db.set_user_type([h], ["Lead"])
    layout.save_types(db, str(root))
    t5 = root / "Take 5" / "Patches"
    (t5 / "Pad" / "Hand Made.syx").write_bytes(t5_program(3, 3, "Hand Made Pad"))      # in no source
    (t5 / "Pad" / "notes.txt").write_text("hi")                                         # not a patch
    pl = rebuild.plan(db, str(root))
    assert [(os.path.basename(s), os.path.basename(os.path.dirname(d))) for s, d, _ in pl.moves] == [("Dimension 5.syx", "Lead")]
    assert [os.path.basename(p) for p, _ in pl.hand] == ["Hand Made.syx"]
    assert [os.path.basename(p) for p, _ in pl.removes] == ["notes.txt"]
    rebuild.apply(db, pl)
    assert (t5 / "Lead" / "Dimension 5.syx").is_file() and not (t5 / "Unknown" / "Dimension 5.syx").exists()
    assert (root / "Take 5" / "Sources" / "User" / "Added by hand" / "Hand Made.syx").is_file()
    assert list((root / "_Patch Manager" / "Removed").rglob("notes.txt"))
    scan(db_path, root)                                   # the hand-made sound is now a user source …
    rebuild.apply(db, rebuild.plan(db, str(root)))        # … and comes back into Patches
    assert (t5 / "Pad" / "Hand Made Pad.syx").is_file()
    assert rebuild.plan(db, str(root)).empty
    # a new PC: empty database, same Drive — types.json and the files on disk are enough
    db2 = DB(str(tmp_path / "pc2.db"))
    layout.load_types(db2, str(root))
    scan(db2.path, root)
    assert rebuild.plan(db2, str(root)).empty


def test_missing_source_keeps_patch_until_accepted(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)
    db = DB(db_path)
    rebuild.apply(db, rebuild.plan(db, str(root)))
    (root / "Sub 37" / "Sources" / "Free" / "All_Sounds" / "PD Blonk.syx").unlink()
    scan(db_path, root)
    assert rebuild.plan(db, str(root)).empty                         # still wanted while it is only Missing
    db.accept_missing(db.source_alerts()[0]["id"])
    pl = rebuild.plan(db, str(root))
    assert [os.path.basename(p) for p, _ in pl.removes] == ["PD Blonk.syx"]


def test_user_file_deleted_is_not_an_alert(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    u = root / "Take 5" / "Sources" / "User" / "2026-11-26 0900 Take 5 read"
    u.mkdir(parents=True)
    (u / "U02-03 Mine.syx").write_bytes(t5_program(1, 2, "Mine"))
    scan(db_path, root)
    (u / "U02-03 Mine.syx").unlink()
    scan(db_path, root)
    assert DB(db_path).source_alerts() == []


# ---------------------------------------------------------------- Get from synth and Initialize, through the dialog
def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_get_from_synth_keeps_only_new_sounds_in_user(old, tmp_path):
    from patchmgr.drivers.take5 import Take5
    from patchmgr.ui.synths import ReadDialog, ReadWorker
    from tests.test_factory import FakeTake5
    _qapp()
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)
    db = DB(db_path)

    class Sess:
        drivers = {"take5": Take5(FakeTake5())}

        def ready(self, k):
            return True
    res = ReadWorker("get", "take5", Sess(), db_path, str(root))._run()
    assert res["folder"] is None                                         # Get keeps no backup
    assert len(db.slot_rows("take5")) == 512                              # green rows updated
    d = ReadDialog(None, "get", "take5", Sess(), db, str(root))
    d._done(res)
    n_new = len(d.rows)
    assert n_new == 256 and all(r.tick for r in d.rows)     # user banks only: F1–F16 are factory, left to Initialize
    d.rows[0].tick = False                                                # one left out
    d.apply()
    assert d.kept["n_user"] == n_new - 1 and d.kept["factory"] is None
    user = os.path.join(str(root), "Take 5", "Sources", "User")
    got = [f for _, _, fs in os.walk(user) for f in fs if f.endswith(".syx")]
    assert len(got) == n_new - 1 + 1                                     # + the one split from the old factory read
    # a second Get finds only the sound left out
    d2 = ReadDialog(None, "get", "take5", Sess(), DB(db_path), str(root))
    d2._done(res)
    assert len(d2.rows) == 1


def test_initialize_splits_factory_and_yours_into_sources(old, tmp_path):
    from patchmgr.drivers.take5 import Take5
    from patchmgr.ui.synths import ReadDialog, ReadWorker
    from tests.test_factory import FakeTake5
    _qapp()
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    scan(db_path, root)
    db = DB(db_path)

    class Sess:
        drivers = {"take5": Take5(FakeTake5())}

        def ready(self, k):
            return True
    res = ReadWorker("init", "take5", Sess(), db_path, str(root))._run()
    assert "Backups" in res["folder"]                                     # the raw read is kept as before
    d = ReadDialog(None, "init", "take5", Sess(), db, str(root))
    d.bank_set = {17}                                                     # F1 is factory
    d._done(res)
    d.apply()
    assert d.kept["n_factory"] == 16 and d.kept["n_user"] == 256      # sounds from factory banks are never yours
    fac = d.kept["factory"]
    assert "Sources" in fac and len(os.listdir(fac)) == 16
    assert db.one("SELECT COUNT(*) FROM patch p WHERE p.name LIKE 'U1P%' AND EXISTS(SELECT 1 FROM patch_origin po "
                  "JOIN source_file sf ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id WHERE po.patch_id=p.id "
                  "AND s.type='factory')") == 0                              # your sounds are not factory


def test_cleanup_archive_folders_are_not_read(old, tmp_path):
    root, _ = old
    db_path = str(tmp_path / "lib.db")
    reorg(root, db_path)
    a = root / "Take 5" / "Sources" / "User" / "Archive" / "old read"
    a.mkdir(parents=True)
    (a / "x.syx").write_bytes(t5_program(9, 9, "Archived One"))
    scan(db_path, root)
    assert DB(db_path).one("SELECT COUNT(*) FROM patch WHERE name='Archived One'") == 0
