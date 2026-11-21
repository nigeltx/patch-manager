"""Scanning Studio → Patches (spec 4.1). Indexes files where they are; never moves, renames or edits them."""
from __future__ import annotations

import os
import re
import string
import sys
import time
from dataclasses import dataclass

from .db import DB
from .formats import (AUDIO_EXT, PATCH_EXT, SYNTH_FOLDERS, FormatError, read_file)

TYPES = {"free": "free", "factory": "factory", "paid": "paid"}
APP_FOLDER = "_Patch Manager"

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
    """-> (synth, type, pack) from the path below the library root."""
    synth = SYNTH_FOLDERS.get(rel_parts[0].lower()) if rel_parts else None
    if synth is None:
        return None, "unsorted", None
    if len(rel_parts) >= 3 and rel_parts[1].lower() in TYPES:
        t = TYPES[rel_parts[1].lower()]
        pack = rel_parts[2] if len(rel_parts) >= 4 else None
        return synth, t, pack
    return synth, "unsorted", None


def guess_roles(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {ROLE_WORDS[w] for w in words if w in ROLE_WORDS}


NUM_RE = re.compile(r"^\s*(\d{1,4})\b")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class Scanner:
    def __init__(self, db_path: str, roots: list[tuple[str, str]], progress=None, cancelled=None):
        """roots: [(path, kind)] with kind 'library' (Studio → Patches), 'folder' (imported) or 'card' (Deluge card)."""
        self.db = DB(db_path)          # own connection: scanning runs on a worker thread
        self.roots = roots
        self.progress = progress or (lambda p: None)
        self.cancelled = cancelled or (lambda: False)
        self.p = Progress()

    # ---------------------------------------------------------------- walk
    def _walk(self, root, kind):
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            parts = [] if rel_dir == "." else rel_dir.split(os.sep)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "System Volume Information"
                                 and not (kind == "library" and not parts and d == APP_FOLDER))
            if kind == "card" and (not parts and True):
                dirnames[:] = [d for d in dirnames if d.upper() == "SYNTHS"]
            for f in sorted(filenames):
                if f.startswith(".") or f.startswith("~$"):
                    continue
                yield os.path.join(dirpath, f), parts + [f]

    def run(self):
        con = self.db.con
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
            seen.add(path)
            ext = os.path.splitext(path)[1].lower()
            if kind == "library":
                synth, stype, pack = classify(parts)
            elif kind == "card":
                synth, stype, pack = "deluge", "card", (parts[1] if len(parts) > 2 else None)
            else:
                synth, stype, pack = None, "unsorted", os.path.basename(root)
            if ext in AUDIO_EXT:
                audio.append((path, parts, synth, stype, pack))
                continue
            sid = self._source(root, synth, stype, pack)
            rel = "/".join(parts)
            try:
                st = os.stat(path)
            except OSError as e:
                self._file(sid, path, rel, 0, 0, "problem", f"cannot read: {e.strerror}")
                continue
            old = con.execute("SELECT id, size, mtime, status FROM source_file WHERE path=?", (path,)).fetchone()
            if old and old["size"] == st.st_size and abs((old["mtime"] or 0) - st.st_mtime) < 1 and old["status"] != "problem":
                continue
            if ext not in PATCH_EXT:
                self._file(sid, path, rel, st.st_size, st.st_mtime, "skipped", f"not a patch file ({ext or 'no extension'})")
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                patches = read_file(data, path, synth)
            except FormatError as e:
                fid = self._file(sid, path, rel, st.st_size, st.st_mtime, "problem", str(e))
                self.p.problems += 1
                continue
            except OSError as e:
                self._file(sid, path, rel, st.st_size, st.st_mtime, "problem", f"cannot read: {e}")
                self.p.problems += 1
                continue
            if synth and patches and patches[0].synth != synth and kind == "library":
                # e.g. a Sub 37 file filed under Take 5: read it as what it is, but say so
                pass
            fid = self._file(sid, path, rel, st.st_size, st.st_mtime, "ok", None)
            con.execute("DELETE FROM patch_origin WHERE file_id=?", (fid,))
            for p in patches:
                pid = con.execute("SELECT id FROM patch WHERE hash=?", (p.sound_hash,)).fetchone()
                if pid:
                    pid = pid[0]
                else:
                    cur = con.execute("INSERT INTO patch(synth, name, hash, data, fmt, kind, first_seen) VALUES(?,?,?,?,?,?,?)",
                                      (p.synth, p.name, p.sound_hash, p.data, p.fmt, p.kind, time.time()))
                    pid = cur.lastrowid
                    self.p.added += 1
                con.execute("INSERT OR REPLACE INTO patch_origin VALUES(?,?,?,?,?,?)",
                            (pid, fid, p.index, p.name, p.bank, p.slot))
                for t in p.tags:
                    self.db.suggest_tag(p.sound_hash, t, "file")
                if p.synth in ("sub37", "deluge") and p.kind == "sound":
                    for t in guess_roles(p.name):
                        self.db.suggest_tag(p.sound_hash, t, "guess")
            if self.p.done % 50 == 0:
                con.commit()
        con.commit()
        if not self.cancelled():
            self._forget_missing(seen)
            self._link_previews(audio)
        con.commit()
        self.progress(self.p)
        return self.p

    # ---------------------------------------------------------------- helpers
    def _source(self, root, synth, stype, pack):
        con = self.db.con
        con.execute("INSERT OR IGNORE INTO source(root, synth, type, pack, last_scan) VALUES(?,?,?,?,?)",
                    (root, synth or "", stype, pack or "", time.time()))
        return con.execute("SELECT id FROM source WHERE root=? AND synth=? AND type=? AND pack=?",
                           (root, synth or "", stype, pack or "")).fetchone()[0]

    def _file(self, sid, path, rel, size, mtime, status, problem):
        con = self.db.con
        con.execute("INSERT INTO source_file(source_id, path, rel, size, mtime, status, problem) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET source_id=excluded.source_id, rel=excluded.rel, size=excluded.size, "
                    "mtime=excluded.mtime, status=excluded.status, problem=excluded.problem",
                    (sid, path, rel, size, mtime, status, problem))
        return con.execute("SELECT id FROM source_file WHERE path=?", (path,)).fetchone()[0]

    def _forget_missing(self, seen):
        """Files gone from disk lose their origin rows; the patches themselves (and your ratings) stay."""
        con = self.db.con
        roots = [r for r, _ in self.roots]
        for fid, path in con.execute("SELECT id, path FROM source_file").fetchall():
            if path not in seen and any(path.startswith(r) for r in roots):
                con.execute("DELETE FROM source_file WHERE id=?", (fid,))
        con.execute("DELETE FROM source WHERE id NOT IN (SELECT DISTINCT source_id FROM source_file)")

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
