"""The library database (spec 8.4). One SQLite file on the PC; it can always be rebuilt by rescanning,
except for what you add yourself (ratings, tags, notes, crates), which is keyed by the sound's hash and so
survives a rescan or a pack being moved."""
from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY, root TEXT NOT NULL, synth TEXT, type TEXT NOT NULL, pack TEXT,
    last_scan REAL, UNIQUE(root, synth, type, pack));
CREATE TABLE IF NOT EXISTS source_file (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE, rel TEXT NOT NULL, size INTEGER, mtime REAL, status TEXT NOT NULL, problem TEXT);
CREATE TABLE IF NOT EXISTS patch (
    id INTEGER PRIMARY KEY, synth TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
    data BLOB NOT NULL, fmt TEXT, kind TEXT NOT NULL DEFAULT 'sound', first_seen REAL);
CREATE TABLE IF NOT EXISTS patch_origin (
    patch_id INTEGER NOT NULL REFERENCES patch(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, name TEXT, bank INTEGER, slot INTEGER, PRIMARY KEY(file_id, idx));
CREATE INDEX IF NOT EXISTS origin_patch ON patch_origin(patch_id);
CREATE TABLE IF NOT EXISTS tag (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, grp TEXT);
CREATE TABLE IF NOT EXISTS patch_tag (
    hash TEXT NOT NULL, tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    origin TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(hash, tag_id));
CREATE TABLE IF NOT EXISTS meta (
    hash TEXT PRIMARY KEY, rating INTEGER, notes TEXT, last_heard REAL, times_heard INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS crate (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, query TEXT);
CREATE TABLE IF NOT EXISTS crate_patch (
    crate_id INTEGER NOT NULL REFERENCES crate(id) ON DELETE CASCADE, hash TEXT NOT NULL, pos INTEGER,
    PRIMARY KEY(crate_id, hash));
CREATE TABLE IF NOT EXISTS preview (
    hash TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, PRIMARY KEY(hash, path));
CREATE TABLE IF NOT EXISTS slot_state (synth TEXT, bank INTEGER, slot INTEGER, hash TEXT, read_at REAL,
    PRIMARY KEY(synth, bank, slot));
CREATE TABLE IF NOT EXISTS journal (op_id INTEGER PRIMARY KEY, time REAL, synth TEXT, bank INTEGER, slot INTEGER,
    before_hash TEXT, after_hash TEXT, backup_path TEXT, step TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT);
"""

STARTER_TAGS = {
    "role": ["bass", "lead", "pad", "pluck", "keys", "seq", "fx", "drum"],
    "style": ["psytrance", "psychill", "psybient", "techno", "trance", "ambient"],
    "character": ["dark", "bright", "warm", "wide", "dry", "evolving"],
}


def default_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "PatchManager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "library.db")


class DB:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        for grp, names in STARTER_TAGS.items():
            for n in names:
                self.con.execute("INSERT OR IGNORE INTO tag(name, grp) VALUES(?, ?)", (n, grp))
        self.con.commit()

    # ---------------------------------------------------------------- generic
    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return r[0] if r else None

    def setting(self, key, default=None):
        v = self.one("SELECT value FROM setting WHERE key=?", (key,))
        return default if v is None else v

    def set_setting(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO setting VALUES(?, ?)", (key, str(value)))
        self.con.commit()

    # ---------------------------------------------------------------- tags
    def tag_id(self, name, grp=None):
        name = name.strip().lower()
        self.con.execute("INSERT OR IGNORE INTO tag(name, grp) VALUES(?, ?)", (name, grp))
        return self.one("SELECT id FROM tag WHERE name=?", (name,))

    def tags_for(self, h):
        return self.q("SELECT t.name, t.grp, pt.origin, pt.confirmed FROM patch_tag pt JOIN tag t ON t.id=pt.tag_id "
                      "WHERE pt.hash=? ORDER BY pt.confirmed DESC, t.name", (h,))

    def set_tag(self, h, name, on=True):
        tid = self.tag_id(name)
        if on:
            self.con.execute("INSERT INTO patch_tag(hash, tag_id, origin, confirmed) VALUES(?, ?, 'you', 1) "
                             "ON CONFLICT(hash, tag_id) DO UPDATE SET confirmed=1", (h, tid))
        else:
            self.con.execute("DELETE FROM patch_tag WHERE hash=? AND tag_id=?", (h, tid))
        self.con.commit()

    def suggest_tag(self, h, name, origin):
        tid = self.tag_id(name)
        self.con.execute("INSERT OR IGNORE INTO patch_tag(hash, tag_id, origin, confirmed) VALUES(?, ?, ?, 0)",
                         (h, tid, origin))

    def all_tags(self):
        return self.q("SELECT t.name, t.grp, COUNT(pt.hash) n FROM tag t LEFT JOIN patch_tag pt ON pt.tag_id=t.id "
                      "GROUP BY t.id ORDER BY t.grp IS NULL, t.grp, t.name")

    # ---------------------------------------------------------------- meta
    def meta(self, h):
        r = self.q("SELECT rating, notes, last_heard, times_heard FROM meta WHERE hash=?", (h,))
        return r[0] if r else None

    def set_rating(self, h, stars):
        self.con.execute("INSERT INTO meta(hash, rating) VALUES(?, ?) ON CONFLICT(hash) DO UPDATE SET rating=excluded.rating",
                         (h, stars or None))
        self.con.commit()

    def set_notes(self, h, text):
        self.con.execute("INSERT INTO meta(hash, notes) VALUES(?, ?) ON CONFLICT(hash) DO UPDATE SET notes=excluded.notes",
                         (h, text))
        self.con.commit()

    def mark_heard(self, h):
        self.con.execute("INSERT INTO meta(hash, last_heard, times_heard) VALUES(?, ?, 1) ON CONFLICT(hash) DO UPDATE "
                         "SET last_heard=excluded.last_heard, times_heard=COALESCE(times_heard, 0)+1", (h, time.time()))
        self.con.commit()

    # ---------------------------------------------------------------- crates
    def crates(self):
        return self.q("SELECT c.id, c.name, c.query, COUNT(cp.hash) n FROM crate c LEFT JOIN crate_patch cp "
                      "ON cp.crate_id=c.id GROUP BY c.id ORDER BY c.query IS NOT NULL, c.name COLLATE NOCASE")

    def new_crate(self, name, query=None):
        self.con.execute("INSERT INTO crate(name, query) VALUES(?, ?)", (name.strip(), query))
        self.con.commit()
        return self.one("SELECT id FROM crate WHERE name=?", (name.strip(),))

    def delete_crate(self, cid):
        self.con.execute("DELETE FROM crate WHERE id=?", (cid,))
        self.con.commit()

    def rename_crate(self, cid, name):
        self.con.execute("UPDATE crate SET name=? WHERE id=?", (name.strip(), cid))
        self.con.commit()

    def crate_add(self, cid, hashes):
        pos = self.one("SELECT COALESCE(MAX(pos), 0) FROM crate_patch WHERE crate_id=?", (cid,)) or 0
        for h in hashes:
            pos += 1
            self.con.execute("INSERT OR IGNORE INTO crate_patch VALUES(?, ?, ?)", (cid, h, pos))
        self.con.commit()

    def crate_remove(self, cid, hashes):
        self.con.executemany("DELETE FROM crate_patch WHERE crate_id=? AND hash=?", [(cid, h) for h in hashes])
        self.con.commit()

    def crates_of(self, h):
        return [r[0] for r in self.q("SELECT c.name FROM crate_patch cp JOIN crate c ON c.id=cp.crate_id "
                                     "WHERE cp.hash=? ORDER BY c.name", (h,))]

    # ---------------------------------------------------------------- patches
    def origins(self, patch_id):
        return self.q("SELECT sf.path, sf.rel, s.type, s.pack, s.root, po.idx, po.bank, po.slot, po.name "
                      "FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id "
                      "WHERE po.patch_id=? ORDER BY s.type, sf.rel, po.idx", (patch_id,))

    def previews(self, h):
        return self.q("SELECT path, kind FROM preview WHERE hash=? ORDER BY kind", (h,))

    def problems(self):
        return self.q("SELECT sf.path, sf.rel, sf.problem, s.synth FROM source_file sf JOIN source s ON s.id=sf.source_id "
                      "WHERE sf.status='problem' ORDER BY sf.rel")
