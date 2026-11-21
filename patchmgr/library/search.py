"""Search (spec 4.6): plain words plus filters, turned into one SQL query over the library."""
from __future__ import annotations

import re
import shlex

SYNTH_ALIASES = {"take5": "take5", "take 5": "take5", "t5": "take5", "sub37": "sub37", "sub 37": "sub37",
                 "s37": "sub37", "moog": "sub37", "deluge": "deluge", "syntakt": "syntakt", "st": "syntakt"}
STATES = {"unheard", "heard", "onsynth", "preview", "dupe", "unrated", "problem"}

BASE = """
SELECT p.id, p.hash, p.synth, p.name, p.fmt, p.kind,
       (SELECT s.pack FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id
         WHERE po.patch_id=p.id ORDER BY CASE s.type WHEN 'paid' THEN 0 WHEN 'free' THEN 1 WHEN 'factory' THEN 2 ELSE 3 END,
         sf.rel LIMIT 1) AS pack,
       (SELECT GROUP_CONCAT(DISTINCT s.type) FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id
         JOIN source s ON s.id=sf.source_id WHERE po.patch_id=p.id) AS types,
       (SELECT COUNT(*) FROM patch_origin po WHERE po.patch_id=p.id) AS n_origins,
       (SELECT GROUP_CONCAT(t.name || CASE WHEN pt.confirmed THEN '' ELSE '?' END, ' ') FROM patch_tag pt
         JOIN tag t ON t.id=pt.tag_id WHERE pt.hash=p.hash) AS tags,
       m.rating, m.notes, m.last_heard,
       EXISTS(SELECT 1 FROM preview pv WHERE pv.hash=p.hash) AS has_preview,
       EXISTS(SELECT 1 FROM slot_state ss WHERE ss.hash=p.hash) AS on_synth
FROM patch p LEFT JOIN meta m ON m.hash=p.hash
"""


class QueryError(ValueError):
    pass


def tokens(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def build(text: str, scope: dict | None = None) -> tuple[str, list]:
    """-> (sql, args). scope narrows to what is selected in the sources pane:
    {'synth': 'take5'} / {'type': 'paid'} / {'synth':…, 'type':…, 'pack':…} / {'crate_id': 3} / {'kind': 'project'}."""
    where, args = [], []
    scope = scope or {}
    if scope.get("synth"):
        where.append("p.synth=?"); args.append(scope["synth"])
    if scope.get("type") or scope.get("pack"):
        cond, a = ["s.type=?"] if scope.get("type") else [], [scope["type"]] if scope.get("type") else []
        if scope.get("pack"):
            cond.append("s.pack=?"); a.append(scope["pack"])
        where.append("EXISTS(SELECT 1 FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s "
                     "ON s.id=sf.source_id WHERE po.patch_id=p.id AND " + " AND ".join(cond) + ")")
        args += a
    if scope.get("crate_id"):
        where.append("p.hash IN (SELECT hash FROM crate_patch WHERE crate_id=?)"); args.append(scope["crate_id"])
    if scope.get("kind"):
        where.append("p.kind=?"); args.append(scope["kind"])
    for tok in tokens(text or ""):
        neg = tok.startswith("-") and len(tok) > 1
        t = tok[1:] if neg else tok
        key, _, val = t.partition(":")
        key = key.lower()
        cond, a = None, []
        if val and key == "synth":
            s = SYNTH_ALIASES.get(val.lower().replace("_", " "))
            if not s:
                raise QueryError(f"unknown synth '{val}' (take5, sub37, deluge, syntakt)")
            cond, a = "p.synth=?", [s]
        elif val and key == "tag":
            cond = ("EXISTS(SELECT 1 FROM patch_tag pt JOIN tag tg ON tg.id=pt.tag_id WHERE pt.hash=p.hash AND tg.name=?)")
            a = [val.lower()]
        elif val and key == "rating":
            m = re.fullmatch(r"(>=|<=|>|<|=)?\s*([0-5])", val)
            if not m:
                raise QueryError("rating needs a number 0–5, like rating:>=4")
            op = m.group(1) or "="
            cond, a = f"COALESCE(m.rating, 0) {op} ?", [int(m.group(2))]
        elif val and key == "source":
            v = val.lower()
            if v not in ("free", "factory", "paid", "unsorted", "backup", "card"):
                raise QueryError("source is free, factory, paid, unsorted, backup or card")
            cond = ("EXISTS(SELECT 1 FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s "
                    "ON s.id=sf.source_id WHERE po.patch_id=p.id AND s.type=?)")
            a = [v]
        elif val and key == "pack":
            cond = ("EXISTS(SELECT 1 FROM patch_origin po JOIN source_file sf ON sf.id=po.file_id JOIN source s "
                    "ON s.id=sf.source_id WHERE po.patch_id=p.id AND s.pack LIKE ?)")
            a = [f"%{val}%"]
        elif val and key == "crate":
            cond = "p.hash IN (SELECT cp.hash FROM crate_patch cp JOIN crate c ON c.id=cp.crate_id WHERE c.name LIKE ?)"
            a = [f"%{val}%"]
        elif not val and key in STATES:
            cond = {
                "unheard": "m.last_heard IS NULL",
                "heard": "m.last_heard IS NOT NULL",
                "onsynth": "EXISTS(SELECT 1 FROM slot_state ss WHERE ss.hash=p.hash)",
                "preview": "EXISTS(SELECT 1 FROM preview pv WHERE pv.hash=p.hash)",
                "dupe": "(SELECT COUNT(*) FROM patch_origin po WHERE po.patch_id=p.id) > 1",
                "unrated": "m.rating IS NULL",
                "problem": "0",
            }[key]
        else:
            cond = ("(p.name LIKE ? OR COALESCE(m.notes,'') LIKE ? OR EXISTS(SELECT 1 FROM patch_origin po JOIN "
                    "source_file sf ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id WHERE po.patch_id=p.id AND "
                    "(s.pack LIKE ? OR po.name LIKE ?)))")
            a = [f"%{t}%"] * 4
        where.append(f"NOT {cond}" if neg else cond)
        args += a
    sql = BASE + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.synth, pack, p.name COLLATE NOCASE"
    return sql, args


def one(hash_: str) -> tuple[str, list]:
    """The list row for one patch, for refreshing it in place."""
    return BASE + " WHERE p.hash=?", [hash_]
