"""Scanning Studio → Patches (spec 4.1, 4.2, 4.4). Indexes files where they are; never moves, renames or edits them.

Both layouts are read: the new one (<Synth>\\Sources\\Factory|Paid|Free|User\\<pack>) and the old one
(<Synth>\\Free|Factory|Paid\\<pack>), so the app works before and after Reorganize. <Synth>\\Patches is written by
the app and read by rebuild.py, not here. Zips are read in place. Every file in a source is fingerprinted (size and
SHA-1); a file that changes or disappears behind the app's back is flagged, not silently used (spec 4.4).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import string
import sys
import time
import zipfile
from dataclasses import dataclass

from .db import DB
from .formats import (AUDIO_EXT, PATCH_EXT, SYNTH_FOLDERS, FormatError, read_file)
from .layout import APP_FOLDER, IGNORED_FILES, LIB_TYPES, LOCKED_TYPES, PATCHES, SAMPLES, SOURCES, app_dir

TYPES = {t: t for t in LIB_TYPES}
ZIP_SEP = "::"
ZIP_MAX_DEPTH = 3

ROLE_WORDS = {
    "bass": "bass", "sub": "bass", "808": "drum", "lead": "lead", "pad": "pad", "pads": "pad", "pluck": "pluck",
    "keys": "keys", "key": "keys", "piano": "keys", "organ": "keys", "ep": "keys", "rhodes": "keys",
    "seq": "seq", "sequence": "seq", "arp": "seq", "fx": "fx", "sfx": "fx", "drone": "fx", "noise": "fx",
    "drum": "drum", "kick": "drum", "snare": "drum", "hat": "drum", "hihat": "drum", "perc": "drum", "tom": "drum",
    "clap": "drum", "bell": "keys", "brass": "lead", "strings": "pad",
}


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    current: str = ""
    added: int = 0
    problems: int = 0
    changed: int = 0
    missing: int = 0
    sources_changed: bool = False       # a file was added to, changed in or removed from Sources: offer a rebuild


def sha1_file(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_library_root() -> str | None:
    """Studio → Patches in Google Drive for desktop, wherever Windows mounted it."""
    candidates = []
    if sys.platform == "win32":
        for letter in string.ascii_uppercase[2:]:
            candidates += [f"{letter}:\\My Drive\\Studio\\Patches", f"{letter}:\\Studio\\Patches"]
    home = os.path.expanduser("~")
    candidates += [os.path.join(home, "My Drive", "Studio", "Patches"),
                   os.path.join(home, "Google Drive", "My Drive", "Studio", "Patches")]
    return next((c for c in candidates if os.path.isdir(c)), None)


def find_deluge_cards() -> list[str]:
    """Removable drives with SYNTHS and SAMPLES at the root (same rule as the Deluge Sample Manager)."""
    found = []
    if sys.platform == "win32":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:          # DRIVE_REMOVABLE
                    if os.path.isdir(root + "SYNTHS") and os.path.isdir(root + "SAMPLES"):
                        found.append(root)
    return found


def classify(rel_parts: list[str]):
    """-> (synth, type, pack) from the path below the library root (the last part is the file name)."""
    if rel_parts[:2] == [APP_FOLDER, "Backups"] and len(rel_parts) >= 5:
        return SYNTH_FOLDERS.get(rel_parts[2].lower()), "backup", rel_parts[3]
    synth = SYNTH_FOLDERS.get(rel_parts[0].lower()) if rel_parts else None
    if synth is None:
        return None, "unsorted", None
    sub = rel_parts[1].lower() if len(rel_parts) >= 3 else ""
    if sub == SOURCES.lower():
        if len(rel_parts) >= 4 and rel_parts[2].lower() in TYPES:
            pack = rel_parts[3] if len(rel_parts) >= 5 else None
            return synth, TYPES[rel_parts[2].lower()], pack
        return synth, "unsorted", None
    if sub in (PATCHES.lower(), SAMPLES.lower()):
        return synth, "skip", None
    if sub in TYPES:                                              # the old layout, before Reorganize
        pack = rel_parts[2] if len(rel_parts) >= 4 else None
        return synth, TYPES[sub], pack
    return synth, "unsorted", None


def guess_roles(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {ROLE_WORDS[w] for w in words if w in ROLE_WORDS}


NUM_RE = re.compile(r"^\s*(\d{1,4})\b")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def zip_members(zf: zipfile.ZipFile, depth=0, prefix=""):
    """(member path, bytes) for every patch file in a zip, and in zips inside it."""
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        base = name.rsplit("/", 1)[-1]
        if base.startswith(".") or "__MACOSX/" in name:
            continue
        ext = os.path.splitext(base)[1].lower()
        if ext in PATCH_EXT:
            yield prefix + name, zf.read(info)
        elif ext == ".zip" and depth < ZIP_MAX_DEPTH:
            try:
                inner = zipfile.ZipFile(io.BytesIO(zf.read(info)))
            except zipfile.BadZipFile:
                continue
            yield from zip_members(inner, depth + 1, prefix + name + ZIP_SEP)


class Scanner:
    def __init__(self, db_path: str, roots: list[tuple[str, str]], progress=None, cancelled=None):
        """roots: [(path, kind)] with kind 'library' (Studio → Patches), 'folder' (imported) or 'card' (Deluge card)."""
        self.db = DB(db_path)          # own connection: scanning runs on a worker thread
        self.roots = roots
        self.progress = progress or (lambda p: None)
        self.cancelled = cancelled or (lambda: False)
        self.p = Progress()
        self.new_shas = set()          # fingerprints of files first seen in this scan: a "missing" file may have moved
        self.known = {}                # rel → [size, sha1] from _Patch Manager\sources.json (another PC's fingerprints)

    # ---------------------------------------------------------------- walk
    def _walk(self, root, kind):
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            parts = [] if rel_dir == "." else rel_dir.split(os.sep)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "System Volume Information")
            if kind == "library" and parts[:1] == [APP_FOLDER]:
                # only the dated backup folders under _Patch Manager\Backups\<synth> are read
                keep = {1: {"Backups"}}.get(len(parts))
                if keep is not None:
                    dirnames[:] = [d for d in dirnames if d in keep]
                if len(parts) < 4:
                    filenames = []
            if kind == "library" and len(parts) == 1 and SYNTH_FOLDERS.get(parts[0].lower()):
                dirnames[:] = [d for d in dirnames if d.lower() not in (PATCHES.lower(), SAMPLES.lower())]
            if kind == "card" and not parts:
                dirnames[:] = [d for d in dirnames if d.upper() == "SYNTHS"]
            for f in sorted(filenames):
                if f.startswith(".") or f.startswith("~$") or f.lower() in IGNORED_FILES or f.endswith(".tmp"):
                    continue
                yield os.path.join(dirpath, f), parts + [f]

    def run(self):
        con = self.db.con
        lib_root = next((r for r, k in self.roots if k == "library"), None)
        if lib_root:
            try:
                with open(app_dir(lib_root, "sources.json"), encoding="utf-8") as f:
                    self.known = json.load(f)
            except (OSError, ValueError):
                self.known = {}
        jobs = []
        for root, kind in self.roots:
            if os.path.isdir(root):
                jobs += [(root, kind, p, parts) for p, parts in self._walk(root, kind)]
        self.p.total = len(jobs)
        seen = set()
        audio = []
        for root, kind, path, parts in jobs:
            if self.cancelled():
                break
            self.p.done += 1
            self.p.current = parts[-1]
            if self.p.done % 10 == 0 or self.p.done == self.p.total:
                self.progress(self.p)
            ext = os.path.splitext(path)[1].lower()
            if kind == "library":
                synth, stype, pack = classify(parts)
            elif kind == "card":
                synth, stype, pack = "deluge", "card", (parts[1] if len(parts) > 2 else None)
            else:
                synth, stype, pack = None, "unsorted", os.path.basename(root)
            if stype == "skip":
                continue
            seen.add(path)
            if ext in AUDIO_EXT:
                audio.append((path, parts, synth, stype, pack))
                continue
            locked = kind == "library" and stype in LOCKED_TYPES
            sid = self._source(root, synth, stype, pack)
            rel = "/".join(parts)
            try:
                st = os.stat(path)
            except OSError as e:
                self._file(sid, path, rel, 0, 0, "problem", f"cannot read: {e.strerror}")
                continue
            old = con.execute("SELECT id, size, mtime, status, sha1, state, new_sha1 FROM source_file WHERE path=?",
                              (path,)).fetchone()
            if old and old["size"] == st.st_size and abs((old["mtime"] or 0) - st.st_mtime) < 1 \
                    and old["status"] != "problem" and old["state"] != "missing":
                if ext == ".zip":
                    seen.update(self._members_of(path))
                continue
            if ext not in PATCH_EXT and ext != ".zip":
                self._file(sid, path, rel, st.st_size, st.st_mtime, "skipped", f"not a patch file ({ext or 'no extension'})")
                continue
            try:
                h = sha1_file(path)
            except OSError as e:
                self._file(sid, path, rel, st.st_size, st.st_mtime, "problem", f"cannot read: {e.strerror or e}")
                self.p.problems += 1
                continue
            # ---- fingerprints (spec 4.4)
            if old and old["state"] == "missing":
                con.execute("UPDATE source_file SET state='ok' WHERE id=?", (old["id"],))
            if locked and old and old["sha1"]:
                if h == old["sha1"]:                       # touched but the same bytes: nothing to read again
                    con.execute("UPDATE source_file SET size=?, mtime=?, state='ok', new_sha1=NULL WHERE id=?",
                                (st.st_size, st.st_mtime, old["id"]))
                    if ext == ".zip":
                        seen.update(self._members_of(path))
                    continue
                if old["status"] != "problem":             # changed behind the app's back: keep the sounds read before
                    con.execute("UPDATE source_file SET size=?, mtime=?, state='changed', new_sha1=? WHERE id=?",
                                (st.st_size, st.st_mtime, h, old["id"]))
                    self.p.changed += 1
                    if ext == ".zip":
                        seen.update(self._members_of(path))
                    continue
            state, new_sha = "ok", None
            if locked and not old:
                self.new_shas.add(h)
                self.p.sources_changed = True
                k = self.known.get(rel) if lib_root and root == lib_root else None
                if k and k[1] != h:                        # another PC saw different bytes here
                    state, new_sha = "changed", h
                    self.p.changed += 1
            sha_kept = (k[1] if state == "changed" else h) if locked and not old else h
            if ext == ".zip":
                self._read_zip(sid, path, rel, st, synth, sha_kept if locked else h, state, new_sha, seen)
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                patches = read_file(data, path, synth)
            except FormatError as e:
                self._file(sid, path, rel, st.st_size, st.st_mtime, "problem", str(e), sha_kept, state, new_sha)
                self.p.problems += 1
                continue
            except OSError as e:
                self._file(sid, path, rel, st.st_size, st.st_mtime, "problem", f"cannot read: {e}")
                self.p.problems += 1
                continue
            fid = self._file(sid, path, rel, st.st_size, st.st_mtime, "ok", None, sha_kept, state, new_sha)
            self._origins(fid, patches)
            if locked and old:
                self.p.sources_changed = True
            if self.p.done % 50 == 0:
                con.commit()
        con.commit()
        if not self.cancelled():
            self._forget_missing(seen)
            self._link_previews(audio)
            if lib_root:
                self._save_fingerprints(lib_root)
        con.commit()
        self.db.update_type_guesses()
        self.progress(self.p)
        return self.p

    # ---------------------------------------------------------------- zips (spec 4.2)
    def _members_of(self, path):
        pre = path + ZIP_SEP
        return [r[0] for r in self.db.con.execute("SELECT path FROM source_file WHERE substr(path, 1, ?)=?",
                                                  (len(pre), pre))]

    def _read_zip(self, sid, path, rel, st, synth, sha, state, new_sha, seen):
        con = self.db.con
        try:
            zf = zipfile.ZipFile(path)
            members = list(zip_members(zf))
        except (zipfile.BadZipFile, OSError, RuntimeError) as e:
            self._file(sid, path, rel, st.st_size, st.st_mtime, "problem", f"a zip that cannot be opened ({e})",
                       sha, state, new_sha)
            self.p.problems += 1
            return
        self._file(sid, path, rel, st.st_size, st.st_mtime, "zip", None, sha, state, new_sha)
        for old in self._members_of(path):           # members that are no longer in the zip
            if old not in {path + ZIP_SEP + m for m, _ in members}:
                con.execute("DELETE FROM source_file WHERE path=?", (old,))
        for mname, data in members:
            mpath = path + ZIP_SEP + mname
            mrel = rel + ZIP_SEP + mname
            seen.add(mpath)
            try:
                patches = read_file(data, mname, synth)
            except FormatError as e:
                self._file(sid, mpath, mrel, len(data), st.st_mtime, "problem", str(e))
                self.p.problems += 1
                continue
            fid = self._file(sid, mpath, mrel, len(data), st.st_mtime, "ok", None)
            self._origins(fid, patches)

    # ---------------------------------------------------------------- helpers
    def _origins(self, fid, patches):
        con = self.db.con
        con.execute("DELETE FROM patch_origin WHERE file_id=?", (fid,))
        for p in patches:
            pid = con.execute("SELECT id FROM patch WHERE hash=?", (p.sound_hash,)).fetchone()
            if pid:
                pid = pid[0]
            else:
                cur = con.execute("INSERT INTO patch(synth, name, hash, data, fmt, kind, first_seen, blank) "
                                  "VALUES(?,?,?,?,?,?,?,?)",
                                  (p.synth, p.name, p.sound_hash, p.data, p.fmt, p.kind, time.time(), int(p.blank)))
                pid = cur.lastrowid
                self.p.added += 1
            if p.blank:
                con.execute("UPDATE patch SET blank=1 WHERE id=?", (pid,))
            con.execute("INSERT OR REPLACE INTO patch_origin VALUES(?,?,?,?,?,?)",
                        (pid, fid, p.index, p.name, p.bank, p.slot))
            for t in p.tags:
                self.db.suggest_tag(p.sound_hash, t, "file")
            if p.synth in ("sub37", "deluge") and p.kind == "sound":
                for t in guess_roles(p.name):
                    self.db.suggest_tag(p.sound_hash, t, "guess")

    def _source(self, root, synth, stype, pack):
        con = self.db.con
        con.execute("INSERT OR IGNORE INTO source(root, synth, type, pack, last_scan) VALUES(?,?,?,?,?)",
                    (root, synth or "", stype, pack or "", time.time()))
        return con.execute("SELECT id FROM source WHERE root=? AND synth=? AND type=? AND pack=?",
                           (root, synth or "", stype, pack or "")).fetchone()[0]

    def _file(self, sid, path, rel, size, mtime, status, problem, sha1=None, state="ok", new_sha1=None):
        con = self.db.con
        con.execute("INSERT INTO source_file(source_id, path, rel, size, mtime, status, problem, sha1, state, new_sha1) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET source_id=excluded.source_id, "
                    "rel=excluded.rel, size=excluded.size, mtime=excluded.mtime, status=excluded.status, "
                    "problem=excluded.problem, sha1=COALESCE(excluded.sha1, source_file.sha1), state=excluded.state, "
                    "new_sha1=excluded.new_sha1",
                    (sid, path, rel, size, mtime, status, problem, sha1, state, new_sha1))
        return con.execute("SELECT id FROM source_file WHERE path=?", (path,)).fetchone()[0]

    def _forget_missing(self, seen):
        """A file gone from a locked source is kept and flagged Missing (its sounds stay) unless the same bytes turned
        up somewhere else in this scan — then it was moved, and is forgotten quietly. Anything else that is gone is
        forgotten; the patches themselves (and your ratings) stay (spec 4.4)."""
        con = self.db.con
        roots = [r for r, _ in self.roots]
        rows = con.execute("SELECT sf.id, sf.path, sf.sha1, sf.state, s.type FROM source_file sf "
                           "JOIN source s ON s.id=sf.source_id").fetchall()
        gone_zips = []
        for r in rows:
            path = r["path"]
            if path in seen or not any(path.startswith(x) for x in roots):
                continue
            if ZIP_SEP in path:
                continue                                    # members follow their zip, below
            if r["type"] in LOCKED_TYPES and r["type"] != "user" and r["sha1"] and r["sha1"] not in self.new_shas:
                if r["state"] != "missing":
                    con.execute("UPDATE source_file SET state='missing' WHERE id=?", (r["id"],))
                    self.p.missing += 1
                continue
            if r["type"] in LOCKED_TYPES:
                self.p.sources_changed = True
            con.execute("DELETE FROM source_file WHERE id=?", (r["id"],))
            gone_zips.append(path + ZIP_SEP)
        for r in rows:
            path = r["path"]
            if ZIP_SEP in path and path not in seen and any(path.startswith(z) for z in gone_zips):
                con.execute("DELETE FROM source_file WHERE id=?", (r["id"],))
        con.execute("DELETE FROM source WHERE id NOT IN (SELECT DISTINCT source_id FROM source_file)")

    def _save_fingerprints(self, root):
        """_Patch Manager\\sources.json: rel → [size, sha1] for every file in Sources, so another PC knows them."""
        rows = self.db.con.execute(
            "SELECT sf.rel, sf.size, sf.sha1 FROM source_file sf JOIN source s ON s.id=sf.source_id "
            "WHERE sf.sha1 IS NOT NULL AND s.root=? AND s.type IN (%s) ORDER BY sf.rel" % ",".join("?" * len(LOCKED_TYPES)),
            (root, *LOCKED_TYPES)).fetchall()
        data = {r["rel"]: [r["size"], r["sha1"]] for r in rows}
        if data == self.known:
            return
        try:
            os.makedirs(app_dir(root), exist_ok=True)
            tmp = app_dir(root, "sources.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=0, sort_keys=True)
            os.replace(tmp, app_dir(root, "sources.json"))
        except OSError:
            pass

    def _link_previews(self, audio):
        """Pack recordings → patches in the same pack, by leading number, else by name (spec 4.5)."""
        con = self.db.con
        con.execute("DELETE FROM preview WHERE kind != 'captured'")
        by_pack = {}
        for path, parts, synth, stype, pack in audio:
            by_pack.setdefault((synth, stype, pack), []).append((path, parts))
        for (synth, stype, pack), files in by_pack.items():
            rows = con.execute(
                "SELECT p.hash, po.name FROM patch p JOIN patch_origin po ON po.patch_id=p.id JOIN source_file sf "
                "ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id WHERE s.synth=? AND s.type=? AND s.pack=? "
                "AND p.kind='sound'", (synth or "", stype, pack or "")).fetchall()
            if not rows:
                continue
            by_num, by_name = {}, {}
            for h, name in rows:
                m = NUM_RE.match(name or "")
                if m:
                    by_num.setdefault(int(m.group(1)), h)
                by_name.setdefault(norm(name or ""), h)
            for path, parts in files:
                stem = os.path.splitext(parts[-1])[0]
                low = "/".join(parts).lower()
                kind = "pack dry" if ("/dry/" in "/" + low or " dry" in stem.lower()) else \
                       "pack wet" if ("/wet/" in "/" + low or " wet" in stem.lower()) else "pack demo"
                m = NUM_RE.match(stem)
                h = by_num.get(int(m.group(1))) if m and len(by_num) > 1 else None
                if h is None:
                    ns = norm(stem)
                    h = by_name.get(ns) or next((hh for nm, hh in by_name.items() if nm and len(nm) > 3 and ns.startswith(nm)), None)
                if h:
                    con.execute("INSERT OR IGNORE INTO preview VALUES(?,?,?)", (h, path, kind))
