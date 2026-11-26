"""The Take 5's user slots (spec 7.9, 7.10): clear them to an init program, then fill them with your own patches,
packed from U1-01. Every write is one slot at a time: read the slot, check it still holds what the plan expects,
write, read back, compare; a failed compare puts the old program straight back and stops the run.

User banks U1–U16 are never locked, even when they hold copies of factory sounds. Factory banks F1–F16 are never
written here (the driver refuses them).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import ptypes
from .factory import SlotRead, backup_folder, sound_file_name
from .formats import place, read_take5
from .layout import app_dir
from .rebuild import folder_name, single_file

SYNTH = "take5"
USER_BANKS = range(1, 17)
INIT_FILE = "Take 5 init.syx"
INIT_KEY = "take5_init_hash"


# ---------------------------------------------------------------- the init program
def hash_of(msg: bytes) -> tuple[str, str]:
    p = read_take5(msg, "slot.syx")[0]
    return p.sound_hash, p.name


def init_path(root):
    return app_dir(root, INIT_FILE)


def load_init(db, root):
    """-> (data, hash, name) of the saved init program, or None."""
    p = init_path(root) if root else None
    if not p or not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        data = f.read()
    h, name = hash_of(data)
    mark_blank(db, data)
    return data, h, name


def save_init(db, root, msg: bytes):
    """Keep the init program you made on the panel, and mark its sound blank: never one of yours, never in Patches."""
    data = single_file(SYNTH, msg)
    os.makedirs(app_dir(root), exist_ok=True)
    tmp = init_path(root) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, init_path(root))
    mark_blank(db, data)
    return hash_of(data)


def mark_blank(db, data: bytes):
    p = read_take5(data, "init.syx")[0]
    if db.one("SELECT id FROM patch WHERE hash=?", (p.sound_hash,)) is None:
        db.con.execute("INSERT INTO patch(synth, name, hash, data, fmt, kind, first_seen, blank) VALUES(?,?,?,?,?,?,?,1)",
                       (SYNTH, p.name, p.sound_hash, p.data, p.fmt, p.kind, time.time()))
    else:
        db.con.execute("UPDATE patch SET blank=1, factory=NULL WHERE hash=?", (p.sound_hash,))
    db.set_setting(INIT_KEY, p.sound_hash)
    db.con.commit()


# ---------------------------------------------------------------- plans
@dataclass
class Step:
    bank: int
    prog: int
    data: bytes                 # what to write
    hash: str                   # its sound
    name: str
    expect: str | None          # the sound the slot must still hold just before the write (None: don't check)
    why: str = ""
    tick: bool = True

    @property
    def place(self):
        return place(SYNTH, self.bank, self.prog)


def _factory_hashes(db):
    from .db import FACTORY_SQL
    return {r["hash"] for r in db.q("SELECT p.hash FROM patch p WHERE p.synth=? AND " + FACTORY_SQL, (SYNTH,))}


def _source_types(db, h):
    return {r["type"] for r in db.q("SELECT DISTINCT s.type FROM patch p JOIN live_origin po ON po.patch_id=p.id "
                                    "JOIN source_file sf ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id "
                                    "WHERE p.hash=?", (h,))}


def clear_plan(db, reads: list[SlotRead], init: bytes) -> tuple[list[Step], dict]:
    """Every user slot not already holding the init program gets it."""
    ih, iname = hash_of(init)
    fac = _factory_hashes(db)
    steps, counts = [], {"factory copy": 0, "yours": 0, "other": 0, "init already": 0}
    for r in reads:
        if r.bank not in USER_BANKS:
            continue
        if r.hash == ih:
            counts["init already"] += 1
            continue
        types = _source_types(db, r.hash)
        if r.hash in fac:
            why = "a copy of a factory sound"; counts["factory copy"] += 1
        elif types & {"user", "paid", "free"}:
            why = "yours — kept in Sources"; counts["yours"] += 1
        else:
            why = "blank or repeated"; counts["other"] += 1
        steps.append(Step(r.bank, r.slot, init, ih, iname, r.hash, f"{r.name} — {why}"))
    return steps, counts


def your_patches(db) -> list:
    """Take 5 patches whose only source is Sources › User, in Patches order (type folder, then name)."""
    rows = db.q(
        "SELECT p.hash, p.name, p.data, COALESCE(tu.types, tg.types, ',Unknown,') types, "
        "(SELECT folder FROM patch_file pf WHERE pf.hash=p.hash LIMIT 1) folder FROM patch p "
        "LEFT JOIN type_guess tg ON tg.hash=p.hash LEFT JOIN type_user tu ON tu.hash=p.hash "
        "WHERE p.synth=? AND p.kind='sound' AND p.blank=0 "
        "AND EXISTS(SELECT 1 FROM live_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s "
        "  ON s.id=sf.source_id WHERE po.patch_id=p.id AND s.type='user') "
        "AND NOT EXISTS(SELECT 1 FROM live_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s "
        "  ON s.id=sf.source_id WHERE po.patch_id=p.id AND s.type IN ('factory','paid','free'))", (SYNTH,))
    order = [folder_name(t) for t in ptypes.ALL_TYPES + [ptypes.UNKNOWN]]

    out = []
    for r in rows:
        r = dict(r)
        r["folder"] = r["folder"] or folder_name((ptypes.unpack(r["types"]) or [ptypes.UNKNOWN])[0])
        out.append(r)
    return sorted(out, key=lambda r: (order.index(r["folder"]) if r["folder"] in order else 99,
                                      (r["name"] or "").lower(), r["hash"]))


def fill_plan(db, init_hash: str) -> tuple[list[Step], list]:
    """Your patches, packed from the first user slot holding the init program. -> (steps, left over)."""
    slots = db.q("SELECT bank, slot FROM slot_state WHERE synth=? AND bank BETWEEN 1 AND 16 AND hash=? ORDER BY bank, slot",
                 (SYNTH, init_hash))
    on_user = {r["hash"] for r in db.q("SELECT hash FROM slot_state WHERE synth=? AND bank BETWEEN 1 AND 16", (SYNTH,))}
    todo = [r for r in your_patches(db) if r["hash"] not in on_user]
    steps = []
    for s, r in zip(slots, todo):
        steps.append(Step(s["bank"], s["slot"], bytes(r["data"]), r["hash"], r["name"], init_hash, r["folder"]))
    return steps, todo[len(steps):]


# ---------------------------------------------------------------- running a plan
@dataclass
class RunResult:
    done: int = 0
    skipped: list = field(default_factory=list)     # (place, why)
    failed: str = ""
    stopped: bool = False
    backup: str = ""
    run: str = ""


def run(driver, db, root, steps: list[Step], label: str, progress=None, cancelled=None) -> RunResult:
    """Write each ticked step, one slot at a time (spec 7.1). Stops at the first failed check."""
    res = RunResult()
    res.run = f"{time.strftime('%Y-%m-%d %H%M%S')} {label}"
    res.backup = backup_folder(root, SYNTH, label)
    os.makedirs(res.backup, exist_ok=True)
    todo = [s for s in steps if s.tick]
    for i, st in enumerate(todo):
        if cancelled and cancelled():
            res.stopped = True
            break
        if progress:
            progress(i, len(todo), f"{st.place} ← {st.name}")
        before = driver.read_program(st.bank, st.prog)
        if before is None:
            res.failed = f"The Take 5 did not send {st.place} — nothing was written there. Check its USB cable."
            break
        bh, bname = hash_of(before)
        if st.expect and bh != st.expect:
            res.skipped.append((st.place, f"now holds '{bname}', not what the plan expected"))
            continue
        bpath = os.path.join(res.backup, sound_file_name(SYNTH, st.place, bname))
        with open(bpath, "wb") as f:
            f.write(before)
        ok, back = driver.write_program(st.data, st.bank, st.prog)
        status = "ok"
        if not ok:
            driver.write_program(before, st.bank, st.prog)          # put it straight back
            status = "failed"
            res.failed = (f"{st.place} did not read back the same as what was sent. The old program was written back. "
                          "The run stopped here.")
        db.con.execute("INSERT INTO journal(time, synth, bank, slot, before_hash, after_hash, backup_path, step, status) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), SYNTH, st.bank, st.prog, bh,
                                                     st.hash if ok else bh, bpath, res.run, status))
        db.set_slots(SYNTH, [{"bank": st.bank, "slot": st.prog, "place": st.place, "name": st.name if ok else bname,
                              "hash": st.hash if ok else bh}], how="written")
        if not ok:
            break
        res.done += 1
    db.con.commit()
    if progress:
        progress(len(todo), len(todo), "done")
    return res


def last_run(db):
    return db.one("SELECT step FROM journal WHERE synth=? AND step NOT LIKE '%Undo%' ORDER BY time DESC LIMIT 1", (SYNTH,))


def undo_steps(db, run_label: str) -> list[Step]:
    """The slots a run wrote, with their backed-up programs, newest first. Each is only put back if the slot still holds
    what the run wrote there."""
    out = []
    for r in db.q("SELECT * FROM journal WHERE synth=? AND step=? AND status='ok' ORDER BY time DESC", (SYNTH, run_label)):
        if not os.path.isfile(r["backup_path"]):
            continue
        with open(r["backup_path"], "rb") as f:
            data = f.read()
        h, name = hash_of(data)
        out.append(Step(r["bank"], r["slot"], data, h, name, r["after_hash"], "put back"))
    return out
