"""The Sources / Patches layout under Studio → Patches (spec 4.1, 4.4, 4.11).

    Patches\\<Synth>\\Sources\\Factory|Paid|Free|User\\<pack> …   everything as it came; the app never changes it
    Patches\\<Synth>\\Patches\\<Type>\\<name>.<ext>                   one file per unique sound, written by rebuild.py
    Patches\\<Synth>\\Backups\\<date> <what>\\…                       point-in-time dumps of the synth; never a source, not read
    Patches\\<Synth>\\Samples\\…                                      audio packs (not sounds); not read
    Patches\\_Patch Manager\\…                                        the app's own files

Reorganize moves the old layout (Free / Factory / Paid straight under each synth, factory reads under
_Patch Manager\\Backups) into the new one, once; backups go to <Synth>\\Backups. It only moves folders and files; it never changes one.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .formats import AUDIO_EXT, PATCH_EXT, SYNTH_LABEL

APP_FOLDER = "_Patch Manager"
SOURCES, PATCHES, BACKUPS, SAMPLES = "Sources", "Patches", "Backups", "Samples"
SOURCE_TYPES = ["Factory", "Paid", "Free", "User"]           # folder names, in name-priority order (spec 4.5)
LIB_TYPES = tuple(t.lower() for t in SOURCE_TYPES)            # source types that feed Patches
LOCKED_TYPES = LIB_TYPES                                      # fingerprinted; the app never writes them
SYNTH_ORDER = ("take5", "sub37", "syntakt", "deluge")
IGNORED_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}


def synth_dir(root, synth):
    return os.path.join(root, SYNTH_LABEL[synth])


def sources_dir(root, synth, stype=None):
    p = os.path.join(synth_dir(root, synth), SOURCES)
    return os.path.join(p, stype) if stype else p


def patches_dir(root, synth):
    return os.path.join(synth_dir(root, synth), PATCHES)


def backups_dir(root, synth):
    """<Synth>\\Backups: dated dumps of the synth, kept to put it back as it was. Not a source (Nigel, 26 Nov 2026)."""
    return find_child(synth_dir(root, synth), BACKUPS) or os.path.join(synth_dir(root, synth), BACKUPS)


def app_dir(root, *more):
    return os.path.join(root, APP_FOLDER, *more)


def stamp(when=None):
    return time.strftime("%Y-%m-%d %H%M", time.localtime(when or time.time()))


def find_child(parent, name):
    """The child folder called name, whatever its case — or None."""
    try:
        for n in os.listdir(parent):
            if n.lower() == name.lower() and os.path.isdir(os.path.join(parent, n)):
                return os.path.join(parent, n)
    except OSError:
        pass
    return None


def is_new_layout(root, synth):
    return find_child(synth_dir(root, synth), SOURCES) is not None


# ---------------------------------------------------------------- what a folder holds
def _holds(path):
    """('patches' | 'audio' | 'empty' | 'other') for a file or folder."""
    files = []
    if os.path.isfile(path):
        files = [path]
    else:
        for d, _, fs in os.walk(path):
            files += [os.path.join(d, f) for f in fs if f.lower() not in IGNORED_FILES]
    exts = {os.path.splitext(f)[1].lower() for f in files}
    if exts & (PATCH_EXT | {".zip"}):
        return "patches"
    if exts & AUDIO_EXT:
        return "audio"
    return "empty" if not files else "other"


def _empty(path):
    for d, _, fs in os.walk(path):
        if any(f.lower() not in IGNORED_FILES for f in fs):
            return False
    return True


# ---------------------------------------------------------------- reorganize
@dataclass
class Move:
    src: str
    dst: str
    why: str


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)       # (path, reason)
    make: list[str] = field(default_factory=list)                      # folders to create
    old_folders: list[str] = field(default_factory=list)               # to add to the Studio Cleanup list once empty
    splits: list[tuple[str, str]] = field(default_factory=list)        # (synth, factory-read folder in Backups)

    @property
    def empty(self):
        return not (self.moves or self.make or self.splits)


def reorganize_plan(root: str) -> Plan:
    plan = Plan()
    for synth in SYNTH_ORDER:
        sdir = synth_dir(root, synth)
        if not os.path.isdir(sdir):
            continue
        src_root = find_child(sdir, SOURCES) or os.path.join(sdir, SOURCES)
        for t in SOURCE_TYPES:
            p = find_child(src_root, t) if os.path.isdir(src_root) else None
            if p is None:
                plan.make.append(os.path.join(src_root, t))
        if find_child(sdir, PATCHES) is None:
            plan.make.append(os.path.join(sdir, PATCHES))
        if find_child(sdir, BACKUPS) is None:
            plan.make.append(os.path.join(sdir, BACKUPS))
        # Free / Factory / Paid / User straight under the synth folder
        for t in SOURCE_TYPES:
            old = find_child(sdir, t)
            if old is None:
                continue
            target_type = (find_child(src_root, t) if os.path.isdir(src_root) else None) or os.path.join(src_root, t)
            for name in sorted(os.listdir(old)):
                if name.lower() in IGNORED_FILES or name.startswith("."):
                    continue
                src = os.path.join(old, name)
                holds = _holds(src)
                if holds == "audio":
                    dst = os.path.join(sdir, SAMPLES, name)
                    why = "samples, not sounds"
                elif holds == "empty":
                    continue
                else:
                    dst = os.path.join(target_type, name)
                    why = f"{t} source"
                if os.path.exists(dst):
                    plan.skipped.append((src, f"{os.path.relpath(dst, root)} is already there"))
                    continue
                plan.moves.append(Move(src, dst, why))
            plan.old_folders.append(old)
        # backups kept by 0.4–0.7 under _Patch Manager\Backups\<Synth> move to <Synth>\Backups. A factory read
        # holds every slot — your own sounds too — so before it moves it is split: factory sounds into
        # Sources\Factory, yours into Sources\User.
        bdir = os.path.join(root, APP_FOLDER, BACKUPS, SYNTH_LABEL[synth])
        if os.path.isdir(bdir):
            new_b = find_child(sdir, BACKUPS) or os.path.join(sdir, BACKUPS)
            for name in sorted(os.listdir(bdir)):
                src = os.path.join(bdir, name)
                if name.lower() in IGNORED_FILES:
                    continue
                if os.path.isdir(src) and "factory read" in name.lower() \
                        and not os.path.exists(os.path.join(src_root, "Factory", name)):
                    plan.splits.append((synth, src))
                dst = os.path.join(new_b, name)
                if os.path.exists(dst):
                    plan.skipped.append((src, f"{os.path.relpath(dst, root)} is already there"))
                else:
                    plan.moves.append(Move(src, dst, "backup — a dump of the synth, not a source"))
            plan.old_folders.append(bdir)
    old_b = os.path.join(root, APP_FOLDER, BACKUPS)
    if os.path.isdir(old_b):
        plan.old_folders.append(old_b)
    return plan


def reorganize_apply(root: str, plan: Plan, log=lambda s: None, db=None) -> tuple[int, list[str], list[str]]:
    """-> (moved, errors, folders added to the cleanup list). The factory-read splits need the library database
    (which sounds Initialize marked factory); without it they are left for later."""
    moved, errors = 0, []
    for d in plan.make:
        os.makedirs(d, exist_ok=True)
    unsplit = set()
    for synth, folder in plan.splits:
        if db is None:
            errors.append(f"{os.path.relpath(folder, root)}: not split — no library database")
            unsplit.add(folder)
            continue
        try:
            res = split_factory_read(db, root, synth, folder)
            moved += 1
            log(f"split {os.path.relpath(folder, root)}: {res['n_factory']} factory, {res['n_user']} yours")
        except OSError as e:
            errors.append(f"{os.path.relpath(folder, root)}: {e.strerror or e}")
            unsplit.add(folder)
    for m in plan.moves:
        if m.src in unsplit:
            continue                                # moved once it has been split
        try:
            os.makedirs(os.path.dirname(m.dst), exist_ok=True)
            os.rename(m.src, m.dst)
            moved += 1
            log(f"moved {os.path.relpath(m.src, root)} → {os.path.relpath(m.dst, root)}")
        except OSError as e:
            errors.append(f"{os.path.relpath(m.src, root)}: {e.strerror or e}")
    listed = []
    for old in plan.old_folders:
        if os.path.isdir(old) and _empty(old):
            listed.append(old)
    listed = [x for x in listed if not any(x != y and x.startswith(y + os.sep) for y in listed)]
    if listed:
        add_to_cleanup(root, listed)
    return moved, errors, listed


def split_factory_read(db, root, synth, folder) -> dict:
    """A factory read in Backups → its factory sounds in Sources\\Factory\\<same name>, the rest that are in no other
    source in Sources\\User. Factory = what Initialize marked (or a maker's file); blanks are left out."""
    from .db import FACTORY_SQL
    from .factory import keep_in_sources
    from .formats import FormatError, place, read_file
    fac_hashes = {r["hash"] for r in db.q("SELECT p.hash FROM patch p WHERE p.synth=? AND " + FACTORY_SQL, (synth,))}
    items, fac = [], set()
    for d, dirs, files in os.walk(folder):
        dirs.sort()
        for f in sorted(files):
            try:
                with open(os.path.join(d, f), "rb") as fh:
                    patches = read_file(fh.read(), f, synth)
            except (FormatError, OSError):
                continue
            for p in patches:
                if p.kind != "sound":
                    continue
                pl = place(synth, p.bank, p.slot) if p.bank else f
                items.append((p.sound_hash, pl, p.name, p.data, p.blank))
                if p.sound_hash in fac_hashes:
                    fac.add(p.sound_hash)
    name = os.path.basename(folder.rstrip("\\/"))
    label = name[:-len(" Factory read")] if name.lower().endswith(" factory read") else name
    res = keep_in_sources(db, root, synth, items, fac, label=label)
    if res["factory"] is None:                     # mark it done even when nothing was factory
        os.makedirs(os.path.join(sources_dir(root, synth, "Factory"), name), exist_ok=True)
    return res


def cleanup_list_path(root: str) -> str:
    studio = os.path.dirname(os.path.normpath(root))
    return os.path.join(studio, "Software", "Studio Cleanup", "Cleanup list.txt")


def add_to_cleanup(root: str, paths: list[str]) -> bool:
    """Add folders to Studio\\Software\\Studio Cleanup\\Cleanup list.txt (paths relative to Studio). False if the
    cleanup tool is not there."""
    p = cleanup_list_path(root)
    if not os.path.isfile(p):
        return False
    studio = os.path.dirname(os.path.normpath(root))
    with open(p, "r", encoding="utf-8-sig") as f:
        have = {line.strip().lower() for line in f}
    rels = [os.path.relpath(x, studio).replace("/", "\\") for x in paths]
    new = [r for r in rels if r.lower() not in have]
    if new:
        with open(p, "rb") as f:
            raw = f.read()
        with open(p, "ab") as f:
            if raw and not raw.endswith(b"\n"):
                f.write(b"\r\n")
            for r in new:
                f.write(r.encode("utf-8") + b"\r\n")
    return True


# ---------------------------------------------------------------- your type choices, kept in Drive (spec 4.5)
def types_path(root):
    return app_dir(root, "types.json")


def save_types(db, root):
    if not root or not os.path.isdir(root):
        return
    data = {r["hash"]: r["types"] for r in db.q("SELECT hash, types FROM type_user ORDER BY hash")}
    os.makedirs(app_dir(root), exist_ok=True)
    tmp = types_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0)
    os.replace(tmp, types_path(root))


def load_types(db, root) -> int:
    """Bring in choices from types.json that this PC's database does not have yet."""
    p = types_path(root) if root else None
    if not p or not os.path.isfile(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    have = {r["hash"] for r in db.q("SELECT hash FROM type_user")}
    rows = [(h, t) for h, t in data.items() if h not in have and isinstance(t, str)]
    db.con.executemany("INSERT OR REPLACE INTO type_user(hash, types) VALUES(?,?)", rows)
    db.con.commit()
    return len(rows)
