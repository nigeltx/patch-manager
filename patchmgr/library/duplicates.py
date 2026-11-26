"""Duplicates in Sources (spec 4.12). Duplicates are allowed and never removed by the app. On demand, every sound found
in more than one place in Sources is listed, and for each one you choose:

- Keep all (the default): every copy counts.
- Keep this one: that copy is the source of truth; the others are *set aside* — left in their files, untouched, but no
  longer counted anywhere (the live_origin view in db.py leaves them out).
- By pair of files: keep one file's copies of the sounds the two share; only the other file's copies are set aside.

What is stored is the set-aside copies (table dup_aside, and _Patch Manager\\duplicates.json): the sound and the copy's
place — the file's path below Studio → Patches and its position in the file — so another PC makes the same choices.
If every remaining copy of a sound has gone, its set-aside copies count again: a sound never loses its last source.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .layout import LIB_TYPES, add_to_cleanup, app_dir
from .formats import SYNTH_LABEL, place

ZIP_SEP = "::"
TYPE_ORDER = {t: i for i, t in enumerate(LIB_TYPES)}


@dataclass
class Copy:
    rel: str            # the file below Studio → Patches (zip members: zip::member)
    idx: int            # position in the file
    stype: str          # factory, paid, free, user
    pack: str
    where: str          # "bank 2 #3", "U1-04", ""
    n_in_file: int      # how many sounds the file holds
    path: str           # the file on disk
    kept: bool = True   # False = set aside

    @property
    def key(self):
        return (self.rel, self.idx)

    def label(self):
        f = self.rel.split("/")
        short = "/".join(f[3:]) if len(f) > 3 and f[1].lower() == "sources" else self.rel
        return f"{self.stype.title()} › {short.replace(ZIP_SEP, ' › ')}" + (f"  ({self.where})" if self.where else "")


@dataclass
class Group:
    hash: str
    synth: str
    name: str
    copies: list[Copy] = field(default_factory=list)
    keep: tuple | None = None       # (rel, idx) when exactly one copy counts

    def state(self):
        kept = [c for c in self.copies if c.kept]
        if len(kept) == len(self.copies):
            return "keeping all"
        if len(kept) == 1:
            return f"keeping {kept[0].label()}"
        return f"keeping {len(kept)} of {len(self.copies)}"


def groups(db, synth=None) -> list[Group]:
    ph = ",".join("?" * len(LIB_TYPES))
    args = list(LIB_TYPES)
    sw = ""
    if synth:
        sw = " AND p.synth=?"; args.append(synth)
    rows = db.q(
        "SELECT p.hash, p.synth, p.name, sf.rel, sf.path, po.idx, po.bank, po.slot, s.type, s.pack, "
        "(SELECT COUNT(*) FROM patch_origin x WHERE x.file_id=sf.id) n_in_file, "
        "EXISTS(SELECT 1 FROM live_origin lo WHERE lo.file_id=po.file_id AND lo.idx=po.idx) live "
        "FROM patch p JOIN patch_origin po ON po.patch_id=p.id JOIN source_file sf ON sf.id=po.file_id "
        f"JOIN source s ON s.id=sf.source_id WHERE s.type IN ({ph}) AND p.kind='sound' AND p.blank=0{sw} "
        "AND p.id IN (SELECT po2.patch_id FROM patch_origin po2 JOIN source_file sf2 ON sf2.id=po2.file_id "
        f"  JOIN source s2 ON s2.id=sf2.source_id WHERE s2.type IN ({ph}) GROUP BY po2.patch_id HAVING COUNT(*) > 1) "
        "ORDER BY p.synth, p.name, p.hash", (*args, *LIB_TYPES))
    out, by = [], {}
    for r in rows:
        g = by.get(r["hash"])
        if g is None:
            g = by[r["hash"]] = Group(r["hash"], r["synth"], r["name"])
            out.append(g)
        where = place(r["synth"], r["bank"], r["slot"]) if r["bank"] and r["synth"] != "deluge" else ""
        g.copies.append(Copy(r["rel"], r["idx"], r["type"], r["pack"], where, r["n_in_file"], r["path"], bool(r["live"])))
    for g in out:
        g.copies.sort(key=lambda c: (TYPE_ORDER.get(c.stype, 9), -c.n_in_file, c.rel, c.idx))
        kept = [c for c in g.copies if c.kept]
        g.keep = kept[0].key if len(kept) == 1 else None
    return out


def set_aside(db, h, rel, idx):
    db.con.execute("INSERT OR IGNORE INTO dup_aside(hash, rel, idx) VALUES(?,?,?)", (h, rel, idx))


def keep_one(db, h, rel, idx):
    """That copy is the source of truth; every other copy of the sound is set aside."""
    rows = db.q("SELECT sf.rel, po.idx FROM patch p JOIN patch_origin po ON po.patch_id=p.id JOIN source_file sf "
                "ON sf.id=po.file_id WHERE p.hash=?", (h,))
    db.con.execute("DELETE FROM dup_aside WHERE hash=?", (h,))
    for r in rows:
        if (r["rel"], r["idx"]) != (rel, idx):
            set_aside(db, h, r["rel"], r["idx"])
    db.con.commit()


def keep_all(db, h):
    db.con.execute("DELETE FROM dup_aside WHERE hash=?", (h,))
    db.con.commit()


# ---------------------------------------------------------------- file pairs
@dataclass
class Pair:
    a: str              # rel of the first file
    b: str
    shared: list        # [(group, copy in a, copy in b)]

    def label(self, rel):
        f = rel.split("/")
        return "/".join(f[3:]).replace(ZIP_SEP, " › ") if len(f) > 3 else rel


def pairs(gs: list[Group]) -> list[Pair]:
    """Files that share sounds, two at a time, most shared first."""
    found = {}
    for g in gs:
        cs = g.copies
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                a, b = cs[i], cs[j]
                if a.rel == b.rel:
                    continue
                if (b.rel, a.rel) in found:
                    a, b = b, a
                found.setdefault((a.rel, b.rel), []).append((g, a, b))
    return sorted((Pair(a, b, s) for (a, b), s in found.items()), key=lambda p: (-len(p.shared), p.a, p.b))


def keep_side(db, pair: Pair, side: str):
    """side 'a', 'b' or 'both' for every sound the two files share. Only the two files' copies change: keeping A sets
    B's copy aside (and brings A's back); copies in other files are left as they are."""
    for g, ca, cb in pair.shared:
        for c, aside in ((ca, side == "b"), (cb, side == "a")):
            if aside:
                set_aside(db, g.hash, c.rel, c.idx)
            else:
                db.con.execute("DELETE FROM dup_aside WHERE hash=? AND rel=? AND idx=?", (g.hash, c.rel, c.idx))
    db.con.commit()


# ---------------------------------------------------------------- files with nothing left
def set_aside_files(db, gs: list[Group] | None = None) -> list[str]:
    """Files in Sources whose every sound is set aside — they do nothing any more. -> absolute paths (a zip once)."""
    rows = db.q("SELECT sf.id, sf.path, (SELECT COUNT(*) FROM patch_origin po WHERE po.file_id=sf.id) n, "
                "(SELECT COUNT(*) FROM live_origin lo WHERE lo.file_id=sf.id) live FROM source_file sf "
                "JOIN source s ON s.id=sf.source_id WHERE s.type IN (%s)" % ",".join("?" * len(LIB_TYPES)), LIB_TYPES)
    zips = {}
    out = []
    for r in rows:
        if ZIP_SEP in r["path"]:
            z = r["path"].split(ZIP_SEP)[0]
            n, live = zips.get(z, (0, 0))
            zips[z] = (n + r["n"], live + r["live"])
        elif r["n"] and not r["live"]:
            out.append(r["path"])
    out += [z for z, (n, live) in zips.items() if n and not live]
    return sorted(out)


def move_out(root, paths) -> bool:
    """Put whole set-aside files on the Studio Cleanup list (Clean up Studio.cmd moves them to Archive)."""
    return add_to_cleanup(root, paths)


# ---------------------------------------------------------------- kept in Drive
def json_path(root):
    return app_dir(root, "duplicates.json")


def save(db, root):
    if not root or not os.path.isdir(root):
        return
    data = {}
    for r in db.q("SELECT * FROM dup_aside ORDER BY hash, rel, idx"):
        data.setdefault(r["hash"], []).append([r["rel"], r["idx"]])
    os.makedirs(app_dir(root), exist_ok=True)
    tmp = json_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0)
    os.replace(tmp, json_path(root))


def load(db, root) -> int:
    p = json_path(root) if root else None
    if not p or not os.path.isfile(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    have = {r["hash"] for r in db.q("SELECT DISTINCT hash FROM dup_aside")}
    rows = [(h, c[0], c[1]) for h, v in data.items() if h not in have and isinstance(v, list)
            for c in v if isinstance(c, list) and len(c) == 2]
    db.con.executemany("INSERT OR IGNORE INTO dup_aside(hash, rel, idx) VALUES(?,?,?)", rows)
    db.con.commit()
    return len({r[0] for r in rows})


def synth_label(s):
    return SYNTH_LABEL.get(s, s)
