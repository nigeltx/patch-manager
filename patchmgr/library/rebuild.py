"""Rebuild Patches from Sources (spec 4.5).

Patches\\<Synth>\\Patches holds every sound found in Sources once, as a single-patch file, in a folder for its primary
type. rebuild.plan() works out what the folder should hold and compares it with what is there; nothing changes until
apply(). Rebuilding twice in a row changes nothing the second time.

- add:     a sound with no file yet
- move:    a file whose type or name changed (the same bytes, a new place)
- remove:  a file whose sound is no longer in any source, a duplicate, or a file that is not a patch → moved to
           _Patch Manager\\Removed\\<date>, never deleted
- hand:    a file you put there yourself whose sound is in no source → moved to Sources\\User\\Added by hand, so the
           next scan adds it as a user source and the next rebuild brings it back
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import ptypes
from .formats import FormatError, SYNTH_LABEL, read_file
from .layout import (IGNORED_FILES, LIB_TYPES, SYNTH_ORDER, app_dir, find_child, patches_dir, sources_dir, stamp,
                     synth_dir, PATCHES)

EXT = {"take5": ".syx", "sub37": ".syx", "syntakt": ".stsnd", "deluge": ".XML"}
PRIORITY = {t: i for i, t in enumerate(LIB_TYPES)}           # factory, paid, free, user
BAD_CHARS = '<>:"/\\|?*'
HAND_FOLDER = "Added by hand"


def safe_name(name: str) -> str:
    s = "".join("-" if (c in BAD_CHARS or ord(c) < 32) else c for c in (name or "")).strip(" .")
    return s[:80] or "Untitled"


def folder_name(ptype: str) -> str:
    return ptype.replace("/", "-")                           # Arp/Seq → Arp-Seq


def single_file(synth: str, data: bytes) -> bytes:
    """The one-sound file written to Patches (spec Table 4.5)."""
    if synth == "take5" and len(data) == 4693 and data[3] == 0x03:          # edit buffer → program dump, slot 1-1
        return b"\xF0\x01\x35\x02\x00\x00" + data[4:-1] + b"\xF7"
    if synth == "sub37" and len(data) == 756 and data[3] == 0x02:           # stored preset → Moog's library form
        b = bytearray(data); b[3] = 0x04
        return bytes(b)
    return data


@dataclass
class Want:
    hash: str
    synth: str
    name: str
    folder: str
    data: bytes


@dataclass
class Plan:
    adds: list = field(default_factory=list)        # (path, Want)
    moves: list = field(default_factory=list)       # (src, dst, hash)
    removes: list = field(default_factory=list)     # (path, why)
    hand: list = field(default_factory=list)        # (path, dst)
    keep: dict = field(default_factory=dict)        # path → hash, already right
    root: str = ""

    @property
    def empty(self):
        return not (self.adds or self.moves or self.removes or self.hand)

    def summary(self):
        return (f"{len(self.adds)} to add, {len(self.moves)} to move, {len(self.removes)} to take out, "
                f"{len(self.hand)} hand-made to keep in Sources › User")


def wanted(db, synth: str) -> list[Want]:
    """Every sound that has a source in Factory, Paid, Free or User (not blank), with its name and primary type."""
    ph = ",".join("?" * len(LIB_TYPES))
    rows = db.q(
        "SELECT p.hash, p.name, p.data, COALESCE(tu.types, tg.types, ',Unknown,') types, "
        "(SELECT po.name FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id "
        f" WHERE po.patch_id=p.id AND s.type IN ({ph}) ORDER BY CASE s.type "
        + " ".join(f"WHEN '{t}' THEN {i}" for t, i in PRIORITY.items()) +
        " END, sf.rel, po.idx LIMIT 1) oname "
        "FROM patch p LEFT JOIN type_guess tg ON tg.hash=p.hash LEFT JOIN type_user tu ON tu.hash=p.hash "
        "WHERE p.synth=? AND p.kind='sound' AND p.blank=0 AND EXISTS(SELECT 1 FROM patch_origin po JOIN source_file sf "
        f"ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id WHERE po.patch_id=p.id AND s.type IN ({ph}))",
        (*LIB_TYPES, synth, *LIB_TYPES))
    out = []
    for r in rows:
        types = ptypes.unpack(r["types"]) or [ptypes.UNKNOWN]
        name = (r["oname"] or r["name"] or "").strip()
        out.append(Want(r["hash"], synth, name, folder_name(types[0]), bytes(r["data"])))
    return out


def targets(wants: list[Want]) -> dict[str, Want]:
    """rel path (Folder/Name.ext) → Want. Two sounds with one name in one folder get (2), (3) — always in the same
    order, so a rebuild is repeatable."""
    out, used = {}, set()
    for w in sorted(wants, key=lambda w: (w.folder.lower(), safe_name(w.name).lower(), w.hash)):
        base, n = safe_name(w.name), 1
        while True:
            fname = (base if n == 1 else f"{base} ({n})") + EXT[w.synth]
            key = f"{w.folder}/{fname}".lower()
            if key not in used:
                used.add(key)
                out[f"{w.folder}/{fname}"] = w
                break
            n += 1
    return out


def _disk(db, pdir: str, synth: str):
    """rel path → (abs path, sound hash or None). Uses what rebuild last wrote when size and date still match."""
    out = {}
    if not os.path.isdir(pdir):
        return out
    rec = {r["path"]: r for r in db.q("SELECT * FROM patch_file WHERE synth=?", (synth,))}
    for d, dirs, files in os.walk(pdir):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.lower() in IGNORED_FILES or f.startswith((".", "~$")) or f.endswith(".pmtmp"):
                continue
            p = os.path.join(d, f)
            rel = os.path.relpath(p, pdir).replace(os.sep, "/")
            st = os.stat(p)
            r = rec.get(p)
            if r and r["size"] == st.st_size and abs((r["mtime"] or 0) - st.st_mtime) < 1:
                out[rel] = (p, r["hash"])
                continue
            try:
                with open(p, "rb") as fh:
                    ps = read_file(fh.read(), p, synth)
                h = ps[0].sound_hash if len(ps) == 1 and ps[0].kind == "sound" else None
            except (FormatError, OSError):
                h = None
            out[rel] = (p, h)
    return out


def plan(db, root: str, synths=SYNTH_ORDER) -> Plan:
    pl = Plan(root=root)
    for synth in synths:
        if not os.path.isdir(synth_dir(root, synth)):
            continue
        pdir = find_child(synth_dir(root, synth), PATCHES) or patches_dir(root, synth)
        want = targets(wanted(db, synth))
        want_lower = {k.lower(): k for k in want}
        disk = _disk(db, pdir, synth)
        wanted_hashes = {w.hash for w in want.values()}
        used_src, right = set(), set()
        # already right
        for rel, (p, h) in disk.items():
            k = want_lower.get(rel.lower())
            if k and k not in right and want[k].hash == h:
                pl.keep[p] = h
                used_src.add(rel); right.add(k)
        # moves for sounds already on disk somewhere else, adds for the rest
        by_hash = {}
        for rel, (p, h) in disk.items():
            if rel not in used_src and h:
                by_hash.setdefault(h, []).append(rel)
        for k, w in want.items():
            if k in right:
                continue
            dst = os.path.join(pdir, *k.split("/"))
            src = by_hash.get(w.hash)
            if src:
                rel = src.pop(0)
                used_src.add(rel)
                pl.moves.append((disk[rel][0], dst, w.hash))
            else:
                pl.adds.append((dst, w))
        # what is left on disk
        for rel, (p, h) in disk.items():
            if rel in used_src:
                continue
            if h is None:
                pl.removes.append((p, "not a single patch for this synth"))
            elif h in wanted_hashes:
                pl.removes.append((p, "a second copy of a sound already in Patches"))
            elif db.one("SELECT blank FROM patch WHERE hash=?", (h,)):
                pl.removes.append((p, "a blank init sound"))
            elif db.one("SELECT 1 FROM patch WHERE hash=?", (h,)) and not _hand_file(p, db, h):
                pl.removes.append((p, "its sound is no longer in any source"))
            else:
                dst = os.path.join(sources_dir(root, synth, "User"), HAND_FOLDER, os.path.basename(p))
                pl.hand.append((p, dst))
    return pl


def _hand_file(path, db, h) -> bool:
    """True if this file is not one rebuild wrote (so its sound came from you, not from a source that has gone)."""
    return not db.one("SELECT 1 FROM patch_file WHERE path=? AND hash=?", (path, h))


def _free(path):
    """path, or 'name (2).ext' … if something is already there."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def apply(db, pl: Plan, log=lambda s: None) -> dict:
    root = pl.root
    removed_dir = app_dir(root, "Removed", stamp())
    done = {"added": 0, "moved": 0, "removed": 0, "hand": 0, "errors": []}

    def out_of_the_way(p, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(p, _free(dst))

    for p, dst in pl.hand:
        try:
            out_of_the_way(p, dst); done["hand"] += 1
            db.con.execute("DELETE FROM patch_file WHERE path=?", (p,))
            log(f"kept in Sources › User: {os.path.basename(p)}")
        except OSError as e:
            done["errors"].append(f"{p}: {e.strerror or e}")
    for p, why in pl.removes:
        try:
            synth_rel = os.path.relpath(p, root)
            out_of_the_way(p, os.path.join(removed_dir, synth_rel))
            db.con.execute("DELETE FROM patch_file WHERE path=?", (p,)); done["removed"] += 1
            log(f"took out {synth_rel} ({why})")
        except OSError as e:
            done["errors"].append(f"{p}: {e.strerror or e}")
    tmp = []
    for i, (src, dst, h) in enumerate(pl.moves):                # two steps, so moves that swap places are safe
        try:
            t = f"{src}.{i}.pmtmp"
            os.replace(src, t); tmp.append((t, src, dst, h))
        except OSError as e:
            done["errors"].append(f"{src}: {e.strerror or e}")
    for t, src, dst, h in tmp:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(t, dst)
            db.con.execute("DELETE FROM patch_file WHERE path=?", (src,))
            _record(db, dst, h); done["moved"] += 1
        except OSError as e:
            done["errors"].append(f"{dst}: {e.strerror or e}")
    for dst, w in pl.adds:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            t = dst + ".pmtmp"
            with open(t, "wb") as f:
                f.write(single_file(w.synth, w.data))
            os.replace(t, dst)
            _record(db, dst, w.hash, w.synth, w.folder); done["added"] += 1
        except OSError as e:
            done["errors"].append(f"{dst}: {e.strerror or e}")
    for p, h in pl.keep.items():
        if not db.one("SELECT 1 FROM patch_file WHERE path=?", (p,)):
            _record(db, p, h)
    _prune_empty(root)
    db.con.commit()
    return done


def _record(db, path, h, synth=None, folder=None):
    st = os.stat(path)
    if synth is None:
        synth = db.one("SELECT synth FROM patch WHERE hash=?", (h,)) or ""
    if folder is None:
        folder = os.path.basename(os.path.dirname(path))
    db.con.execute("INSERT OR REPLACE INTO patch_file(path, synth, hash, folder, size, mtime, written_at) "
                   "VALUES(?,?,?,?,?,?,?)", (path, synth, h, folder, st.st_size, st.st_mtime, time.time()))


def _prune_empty(root):
    for synth in SYNTH_ORDER:
        pdir = find_child(synth_dir(root, synth), PATCHES) if os.path.isdir(synth_dir(root, synth)) else None
        if not pdir:
            continue
        for d in sorted((x for x, _, _ in os.walk(pdir)), key=len, reverse=True):
            if d != pdir:
                try:
                    left = [f for f in os.listdir(d) if f.lower() not in IGNORED_FILES]
                    if not left:
                        for f in os.listdir(d):
                            os.remove(os.path.join(d, f))
                        os.rmdir(d)
                except OSError:
                    pass


def counts(db, synth: str) -> dict:
    """{folder: (patches, on the synth)} from what rebuild last wrote — for the left pane (spec 4.5 Green)."""
    rows = db.q("SELECT pf.folder, COUNT(*) n, SUM(EXISTS(SELECT 1 FROM slot_state ss WHERE ss.hash=pf.hash "
                "AND ss.synth=pf.synth)) on_synth FROM patch_file pf WHERE pf.synth=? GROUP BY pf.folder", (synth,))
    return {r["folder"]: (r["n"], r["on_synth"] or 0) for r in rows}


def label(synth):
    return SYNTH_LABEL[synth]
