"""Duplicates in Sources (spec 4.12)."""
import os

import pytest

from patchmgr.library import duplicates, layout, rebuild, search
from patchmgr.library.db import DB
from patchmgr.library.scanner import Scanner
from tests.test_library import t5_program


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Studio" / "Patches"
    src = root / "Take 5" / "Sources"
    for t in layout.SOURCE_TYPES:
        (src / t).mkdir(parents=True)
    m = src / "Paid" / "Mountains"
    m.mkdir()
    (m / "all banks.syx").write_bytes(b"".join(t5_program(0, i, n) for i, n in enumerate(["Alp", "Ben", "Cho", "Dom"])))
    (m / "Bank 1.syx").write_bytes(t5_program(0, 0, "Alp") + t5_program(0, 1, "Ben"))
    f = src / "Factory" / "read"
    f.mkdir()
    (f / "F1-01 Alp.syx").write_bytes(t5_program(16, 0, "Alp"))
    cl = tmp_path / "Studio" / "Software" / "Studio Cleanup"
    cl.mkdir(parents=True)
    (cl / "Cleanup list.txt").write_text("# list\n", encoding="utf-8")
    dbp = str(tmp_path / "lib.db")
    Scanner(dbp, [(str(root), "library")]).run()
    return root, DB(dbp), tmp_path


def names(gs):
    return {g.name: len(g.copies) for g in gs}


def test_groups_pairs_and_keeping(lib):
    root, db, tmp = lib
    gs = duplicates.groups(db)
    assert names(gs) == {"Alp": 3, "Ben": 2}
    alp = next(g for g in gs if g.name == "Alp")
    assert alp.copies[0].stype == "factory" and alp.state() == "keeping all"
    assert db.one("SELECT is_factory FROM (" + search.build("Alp", {})[0] + ")", search.build("Alp", {})[1]) == 1
    ps = duplicates.pairs(gs)
    top = ps[0]
    assert len(top.shared) == 2 and {os.path.basename(top.a), os.path.basename(top.b)} == {"all banks.syx", "Bank 1.syx"}
    side = "a" if top.a.endswith("all banks.syx") else "b"
    duplicates.keep_side(db, top, side)                      # keep the all-banks copies of Alp and Ben
    gs = duplicates.groups(db)
    alp = next(g for g in gs if g.name == "Alp")
    assert alp.state() == "keeping 2 of 3"                   # only Bank 1's copy was set aside; Factory still counts
    assert db.q(*search.build("Alp", {}))[0]["is_factory"]
    assert [os.path.basename(p) for p in duplicates.set_aside_files(db)] == ["Bank 1.syx"]
    # keep the Paid copy of Alp only: the factory copy is set aside too
    paid = next(c for c in alp.copies if c.rel.endswith("all banks.syx"))
    duplicates.keep_one(db, alp.hash, paid.rel, paid.idx)
    row = db.q(*search.build("Alp", {}))[0]
    assert not row["is_factory"] and row["n_origins"] == 1
    gone = [os.path.basename(p) for p in duplicates.set_aside_files(db)]
    assert sorted(gone) == ["Bank 1.syx", "F1-01 Alp.syx"]
    assert duplicates.move_out(str(root), duplicates.set_aside_files(db))
    cl = (tmp / "Studio" / "Software" / "Studio Cleanup" / "Cleanup list.txt").read_text(encoding="utf-8")
    assert "Bank 1.syx" in cl
    # nothing in Sources was touched
    assert (root / "Take 5" / "Sources" / "Paid" / "Mountains" / "Bank 1.syx").is_file()
    # keep all again
    duplicates.keep_all(db, alp.hash)
    assert db.q(*search.build("Alp", {}))[0]["is_factory"]


def test_choices_follow_to_another_pc_and_patches_stay_unique(lib, tmp_path):
    root, db, tmp = lib
    g = next(g for g in duplicates.groups(db) if g.name == "Ben")
    c = g.copies[-1]
    duplicates.keep_one(db, g.hash, c.rel, c.idx)
    duplicates.save(db, str(root))
    db2 = DB(str(tmp_path / "pc2.db"))
    Scanner(db2.path, [(str(root), "library")]).run()
    assert duplicates.load(db2, str(root)) == 1
    g2 = next(g for g in duplicates.groups(db2) if g.name == "Ben")
    assert g2.keep == (c.rel, c.idx) and g2.state().startswith("keeping")
    rebuild.apply(db, rebuild.plan(db, str(root)))
    files = sorted(p.name for p in (root / "Take 5" / "Patches").rglob("*.syx"))
    assert files == ["Alp.syx", "Ben.syx", "Cho.syx", "Dom.syx"]


def test_a_choice_whose_copy_has_gone_counts_every_copy_again(lib):
    root, db, tmp = lib
    g = next(g for g in duplicates.groups(db) if g.name == "Ben")
    c = next(c for c in g.copies if c.rel.endswith("Bank 1.syx"))
    duplicates.keep_one(db, g.hash, c.rel, c.idx)
    assert db.q(*search.build("Ben", {}))[0]["n_origins"] == 1
    (root / "Take 5" / "Sources" / "Paid" / "Mountains" / "Bank 1.syx").unlink()
    Scanner(db.path, [(str(root), "library")]).run()
    db.accept_missing(db.source_alerts()[0]["id"])
    assert db.q(*search.build("Ben", {}))[0]["n_origins"] == 1      # the all-banks copy counts again
