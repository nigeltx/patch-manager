"""The Syntakt's bank H (spec 7.4, 7.8): Send to slot and Sync with the "For Syntakt" crate.

Banks A–G hold factory sounds and are never written. H001–H127 are yours; H128 is the audition scratch slot and is
left to audition. Every write is one slot at a time, over USB with Elektron's file protocol (drivers/syntakt.py):

  1. list bank H and check the slot still holds what the plan expects (a sound, or nothing);
  2. if it holds a sound, read it, keep it in Syntakt › Backups, and keep it in Sources › User if it is in no source;
  3. write the sound's payload to /soundbanks/H/<slot>;
  4. read it back and compare its identity with what was sent; if it differs, put the old sound back (or clear the
     slot if it was empty) and stop.

Undo puts back what a run wrote over, from its backups; a slot that was empty is cleared again.
"""
from __future__ import annotations

import os
import time

from . import factory
from .factory import backup_folder, sound_file_name
from .formats import make_stsnd, place, read_elektron
from .userslots import RunResult, Step

SYNTH = "syntakt"
BANK = 8                      # H
LETTER = "H"
SCRATCH = 128                 # the audition slot, never used here
SLOTS = list(range(1, SCRATCH))
FOR_CRATE = "For Syntakt"
EMPTY = "(empty)"             # what a step expects when the slot held nothing
READ_KEY = "syntakt_h_read"


def path(slot: int) -> str:
    return f"/soundbanks/{LETTER}/{slot}"


def label(slot: int) -> str:
    return place(SYNTH, BANK, slot)


def identity(name: str, payload: bytes) -> str:
    return read_elektron(make_stsnd(name, payload), "slot.stsnd")[0].sound_hash


def payload_of(data: bytes):
    from ..drivers.syntakt import payload_of as po
    return po(data)


# ---------------------------------------------------------------- what bank H holds
def remember_read(db, reads):
    """Keep a read of bank H: every slot it lists, and forget the slots that were not listed (they are empty)."""
    db.con.execute("DELETE FROM slot_state WHERE synth=? AND bank=?", (SYNTH, BANK))
    db.set_slots(SYNTH, [{"bank": r.bank, "slot": r.slot, "place": r.place, "name": r.name, "hash": r.hash,
                          "info": r.info} for r in reads if r.bank == BANK])
    db.set_setting(READ_KEY, str(time.time()))


def was_read(db) -> bool:
    return bool(db.setting(READ_KEY))


def slot_map(db) -> dict:
    """slot → {name, hash, state}. state: free (empty), yours (a sound with a source), unknown (in no source — kept in
    Sources › User before it is written over). Empty slots are only known after a read of bank H."""
    have = factory.library_hashes(db, SYNTH)
    out = {}
    for r in db.q("SELECT slot, name, hash FROM slot_state WHERE synth=? AND bank=?", (SYNTH, BANK)):
        out[r["slot"]] = {"name": r["name"], "hash": r["hash"], "state": "yours" if r["hash"] in have else "unknown"}
    if was_read(db):
        for s in SLOTS:
            out.setdefault(s, {"name": "", "hash": None, "state": "free"})
    return out


def _why(now):
    if not now:
        return "not read yet (read and kept in Backups first)"
    if now["state"] == "free":
        return "empty"
    if now["state"] == "unknown":
        return f"over '{now['name']}' (kept in Sources › User first)"
    return f"over '{now['name']}' (it is in your library)"


def _expect(now):
    if not now:
        return None
    return EMPTY if now["state"] == "free" else now["hash"]


def send_plan(db, patches: list, start: int) -> tuple[list[Step], list]:
    """Patches to consecutive H slots from start (never H128). -> (steps, did not fit)."""
    sm = slot_map(db)
    i = SLOTS.index(start)
    steps = []
    for k, p in enumerate(patches):
        if i + k >= len(SLOTS):
            return steps, patches[k:]
        s = SLOTS[i + k]
        now = sm.get(s)
        steps.append(Step(BANK, s, bytes(p["data"]), p["hash"], p["name"], _expect(now), _why(now), synth=SYNTH))
    return steps, []


def empty_plan(db, chosen: list[int]) -> tuple[list[Step], int]:
    """Initialize H slots to empty (the Syntakt has no init sound to write): each sound is kept in Backups (and in
    Sources › User if in no source) and the slot cleared. Already-empty slots are left. -> (steps, already empty)."""
    sm = slot_map(db)
    steps, already = [], 0
    for s in sorted(set(chosen)):
        if s not in SLOTS:
            continue
        now = sm.get(s)
        if now and now["state"] == "free":
            already += 1
            continue
        why = f"'{now['name']}'" + (" — kept in Sources › User first" if now["state"] == "unknown" else "") if now \
            else "not read yet (read and kept in Backups first)"
        steps.append(Step(BANK, s, None, "", EMPTY, _expect(now), why, synth=SYNTH))
    return steps, already


def for_crate(db) -> int:
    cid = db.one("SELECT id FROM crate WHERE name=?", (FOR_CRATE,))
    return cid if cid else db.new_crate(FOR_CRATE)


def crate_patches(db, cid) -> list:
    return [dict(r) for r in db.q(
        "SELECT p.hash, p.name, p.data FROM crate_patch cp JOIN patch p ON p.hash=cp.hash "
        "WHERE cp.crate_id=? AND p.synth=? AND p.kind='sound' ORDER BY cp.pos, p.name", (cid, SYNTH))]


def sync_plan(db) -> tuple[list[Step], list, int]:
    """The "For Syntakt" sounds not on the Syntakt yet (in any bank), each into the next empty H slot.
    -> (steps, did not fit, already there)."""
    sm = slot_map(db)
    on = {r["hash"] for r in db.q("SELECT hash FROM slot_state WHERE synth=?", (SYNTH,))}
    want = crate_patches(db, for_crate(db))
    todo = [p for p in want if p["hash"] not in on]
    free = [s for s in SLOTS if sm.get(s, {}).get("state") == "free"]
    steps = [Step(BANK, s, bytes(p["data"]), p["hash"], p["name"], EMPTY, "empty", synth=SYNTH) for s, p in zip(free, todo)]
    return steps, todo[len(steps):], len(want) - len(todo)


# ---------------------------------------------------------------- running a plan
def run(driver, db, root, steps: list[Step], run_label: str, progress=None, cancelled=None) -> RunResult:
    """Write each ticked step (spec 7.4). A step whose data is None clears the slot (Undo of a write into an empty
    slot). Stops at the first failed check."""
    res = RunResult()
    res.run = f"{time.strftime('%Y-%m-%d %H%M%S')} {run_label}"
    res.backup = backup_folder(root, SYNTH, run_label)
    todo = [s for s in steps if s.tick]
    keep_known = factory.library_hashes(db, SYNTH) | factory.blank_hashes(db, SYNTH)
    for i, st in enumerate(todo):
        if cancelled and cancelled():
            res.stopped = True
            break
        where = label(st.prog)
        if progress:
            progress(i, len(todo), f"{where} ← {st.name}")
        with driver.lock:
            listed = {e["slot"]: e for e in driver.list(f"/soundbanks/{LETTER}") if e.get("slot")}
            cur = listed.get(st.prog)
            before, bh, bname = None, None, ""
            if cur:
                before = driver.read(path(st.prog))
                bname = cur["name"]
                bh = identity(bname, before)
        if st.expect and (bh or EMPTY) != st.expect:
            res.skipped.append((where, f"now holds '{bname}', not what the plan expected" if cur else "is now empty"))
            continue
        bpath = ""
        if cur:
            os.makedirs(res.backup, exist_ok=True)
            bfile = make_stsnd(bname, before)
            bpath = os.path.join(res.backup, sound_file_name(SYNTH, where, bname))
            with open(bpath, "wb") as f:
                f.write(bfile)
            if bh not in keep_known:            # in no source: keep it in Sources › User before it leaves the synth
                kept = factory.keep_in_sources(db, root, SYNTH, [(bh, where, bname, bfile, False)], set())
                if kept.get("n_user"):
                    res.kept.append(f"{where} {bname}")
                keep_known.add(bh)
        ok = True
        with driver.lock:
            if st.data is None:
                driver.clear(path(st.prog))
                ok = not any(e.get("slot") == st.prog for e in driver.list(f"/soundbanks/{LETTER}"))
            else:
                name, payload = payload_of(st.data)
                driver.write(path(st.prog), payload)
                ok = identity(name, driver.read(path(st.prog))) == st.hash
            if not ok:                          # put the old sound straight back
                if before is not None:
                    driver.write(path(st.prog), before)
                else:
                    driver.clear(path(st.prog))
        status = "ok" if ok else "failed"
        after = (None if st.data is None else st.hash) if ok else bh
        db.con.execute("INSERT INTO journal(time, synth, bank, slot, before_hash, after_hash, backup_path, step, status) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), SYNTH, BANK, st.prog, bh, after, bpath, res.run, status))
        if after is None:
            db.clear_slots(SYNTH, [(BANK, st.prog)])
        else:
            db.set_slots(SYNTH, [{"bank": BANK, "slot": st.prog, "place": where, "name": st.name if ok else bname,
                                  "hash": after}], how="written")
        if not ok:
            res.failed = (f"{where} did not read back the same as what was sent. The old sound was put back. "
                          "The run stopped here.")
            break
        res.done += 1
    db.con.commit()
    if progress:
        progress(len(todo), len(todo), "done")
    return res


def last_run(db):
    return db.one("SELECT step FROM journal WHERE synth=? AND step NOT LIKE '%Undo%' ORDER BY time DESC LIMIT 1", (SYNTH,))


def undo_steps(db, run_label: str) -> list[Step]:
    """What a run wrote over, newest first: the backed-up sound, or a clear for a slot that was empty. Each is only
    done if the slot still holds what the run wrote there."""
    out = []
    for r in db.q("SELECT * FROM journal WHERE synth=? AND step=? AND status='ok' ORDER BY time DESC", (SYNTH, run_label)):
        expect = r["after_hash"] or EMPTY
        if r["backup_path"]:
            if not os.path.isfile(r["backup_path"]):
                continue
            with open(r["backup_path"], "rb") as f:
                data = f.read()
            name, payload = payload_of(data)
            out.append(Step(BANK, r["slot"], data, identity(name, payload), name, expect, "put back", synth=SYNTH))
        else:
            out.append(Step(BANK, r["slot"], None, "", EMPTY, expect, "empty again", synth=SYNTH))
    return out
