"""Factory presets and what is on each synth (spec 4.7, 4.8, 5.5), without any MIDI or Qt.

A read of a synth is a list of SlotRead, one per slot (or per preset file on the Deluge). From a read the app can:
- review it: which slots hold factory presets (in a factory bank, or matching a maker's Factory file), which are
  yours, and which are blank init sounds;
- keep it as a dated backup source under Studio\\Patches\\_Patch Manager\\Backups\\<synth>\\<folder>;
- apply it: mark factory presets, remember what each slot holds (the green rows), and record the run.
"""
from __future__ import annotations

import os
import re
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field

from .formats import SYNTAKT_BANKS, SYNTH_LABEL

BLANK_REPEATS = 8          # the same sound in this many slots of one read is a blank fill, not a preset


@dataclass
class SlotRead:
    synth: str
    bank: int                       # Take 5 1–32 (17–32 = factory banks F1–F16), Sub 37 1–16, Syntakt 1–8 (A–H), Deluge 0
    slot: int
    place: str                      # as the panel names it: U3-07, F12-01, 10-5, A:002, SYNTHS/…XML
    name: str
    data: bytes                     # what goes in the backup file for this slot
    hash: str
    blank: bool = False
    info: str = ""                  # Syntakt name|size, Deluge size|date — to notice changes without reading again


@dataclass
class ReviewRow:
    read: SlotRead
    status: str                     # 'maker' | 'bank' | 'yours' | 'blank'
    note: str
    tick: bool
    extra: dict = field(default_factory=dict)


def deluge_slot(path: str) -> int:
    return zlib.crc32(path.upper().encode("utf-8")) & 0x7FFFFFFF


# ---------------------------------------------------------------- factory banks setting
def parse_banks(synth: str, text: str) -> set[int]:
    """'F1-F16' / 'U1-U4, F1-F16' (Take 5), '1-9' (Sub 37), 'A' or 'A-B' (Syntakt) → internal bank numbers.
    Raises ValueError with a plain reason."""
    out = set()
    for part in [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]:
        a, _, b = part.partition("-")
        lo, hi = _bank(synth, a.strip()), _bank(synth, (b or a).strip(), prefix=a.strip())
        if hi < lo:
            raise ValueError(f"'{part}' runs backwards")
        out.update(range(lo, hi + 1))
    return out


def _bank(synth, tok, prefix=""):
    t = tok.upper()
    if synth == "syntakt":
        if len(t) == 1 and t in SYNTAKT_BANKS:
            return SYNTAKT_BANKS.index(t) + 1
        raise ValueError(f"'{tok}' is not a Syntakt sound bank (A to H)")
    if synth == "take5":
        m = re.fullmatch(r"([UF]?)(\d{1,2})", t)
        if not m:
            raise ValueError(f"'{tok}' is not a Take 5 bank (U1–U16 or F1–F16)")
        kind = m.group(1) or (prefix[:1].upper() if prefix[:1].upper() in "UF" else "U")
        n = int(m.group(2))
        if not 1 <= n <= 16:
            raise ValueError(f"'{tok}': Take 5 banks run 1 to 16")
        return n + (16 if kind == "F" else 0)
    if synth == "sub37":
        if not t.isdigit() or not 1 <= int(t) <= 16:
            raise ValueError(f"'{tok}' is not a Sub 37 bank (1 to 16)")
        return int(t)
    raise ValueError("the Deluge has no banks — its factory presets are found by matching Synthstrom's files")


def bank_label(synth: str, bank: int) -> str:
    if synth == "take5":
        return f"U{bank}" if bank <= 16 else f"F{bank - 16}"
    if synth == "syntakt":
        return SYNTAKT_BANKS[bank - 1]
    if synth == "deluge":
        return "SYNTHS"
    return str(bank)


# ---------------------------------------------------------------- review
def review(reads: list[SlotRead], factory_banks: set[int], maker: set[str], maker_pack=lambda h: None):
    """The Initialize review list (spec 5.5). Ticked = will be marked factory."""
    counts = Counter(r.hash for r in reads)
    rows = []
    for r in reads:
        if r.blank or (r.synth != "deluge" and counts[r.hash] >= BLANK_REPEATS):
            rows.append(ReviewRow(r, "blank", "Blank starting sound — never marked factory; the slot counts as free", False))
        elif r.hash in maker:
            rows.append(ReviewRow(r, "maker", f"In the maker's set ({maker_pack(r.hash) or 'Factory'})", True))
        elif r.bank in factory_banks:
            note = "In a factory bank" + (" — not in a maker's file in your library" if maker else "")
            rows.append(ReviewRow(r, "bank", note, True))
        else:
            rows.append(ReviewRow(r, "yours", "Yours — not in a factory bank or a maker's set", False))
    return rows


# ---------------------------------------------------------------- backup files
def safe(name: str) -> str:
    s = "".join(c if c.isalnum() or c in " -_.()&+" else "_" for c in name).strip(" .")
    return s[:60] or "sound"


def backup_folder(library_root: str, synth: str, label: str, when=None) -> str:
    stamp = time.strftime("%Y-%m-%d %H%M", time.localtime(when or time.time()))
    return os.path.join(library_root, "_Patch Manager", "Backups", SYNTH_LABEL[synth], f"{stamp} {label}")


def write_backup(folder: str, synth: str, reads: list[SlotRead], whole: bytes | None = None) -> list[str]:
    """Write a read as library files the scanner reads back as a backup source. Returns the files written.
    Take 5: one file per bank (U01.syx … F16.syx). Sub 37: the ALL PRESETS dump as it came. Syntakt: one .stsnd per
    slot. Deluge: each preset file under its own path."""
    os.makedirs(folder, exist_ok=True)
    written = []

    def put(rel, data):
        p = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        written.append(p)

    if synth == "take5":
        by_bank = {}
        for r in sorted(reads, key=lambda r: (r.bank, r.slot)):
            by_bank.setdefault(r.bank, []).append(r.data)
        for bank, msgs in by_bank.items():
            put(f"{bank_label('take5', bank)[0]}{(bank - 1) % 16 + 1:02d}.syx", b"".join(msgs))
    elif synth == "sub37":
        if whole is not None:
            put("ALL PRESETS.syx", whole)
        else:
            for r in reads:
                put(f"{r.bank:02d}-{r.slot:02d} {safe(r.name)}.syx", r.data)
    elif synth == "syntakt":
        for r in reads:
            put(f"{r.place.replace(':', '')} {safe(r.name)}.stsnd", r.data)
    else:
        for r in reads:
            put(r.place, r.data)
    with open(os.path.join(folder, "README.txt"), "w", encoding="utf-8") as f:
        f.write(f"{SYNTH_LABEL[synth]} read by the Patch Manager on {time.strftime('%d %B %Y at %H:%M')}.\n"
                f"{len(reads)} slots. The Patch Manager lists this folder as a backup source.\n")
    return written


# ---------------------------------------------------------------- apply
def apply(db, synth: str, rows: list[ReviewRow], model: str, firmware: str, backup: str) -> int:
    """Mark ticked sounds factory, remember every slot read (the green rows), record the run. -> number marked."""
    blank = [r.read.hash for r in rows if r.status == "blank"]
    maker = db.maker_hashes(synth)
    ticked = {r.read.hash for r in rows if r.tick and r.status != "blank"}
    unticked = {r.read.hash for r in rows if not r.tick and r.status != "blank"} - ticked
    db.set_blank(blank)
    db.set_factory([h for h in ticked if h not in maker], "read")
    db.set_factory([h for h in ticked if h in maker], None)
    db.set_factory([h for h in unticked if h in maker], "no")
    db.set_factory([h for h in unticked if h not in maker], None)
    remember(db, synth, [r.read for r in rows])
    db.record_init(synth, SYNTH_LABEL[synth] if not model else model, firmware, len(rows), len(ticked), backup)
    return len(ticked)


def remember(db, synth, reads: list[SlotRead], replace_all=True, how="read"):
    db.set_slots(synth, [{"bank": r.bank, "slot": r.slot, "place": r.place, "name": r.name, "hash": r.hash,
                          "info": r.info} for r in reads], how=how, replace_all=replace_all)


# ---------------------------------------------------------------- reads into Sources (spec 4.4, 5.3, 5.5)
def sound_file_name(synth: str, place: str, name: str) -> str:
    from .rebuild import EXT
    if synth == "deluge":
        return os.path.basename(place.replace("\\", "/")) or f"{safe(name)}.XML"
    return f"{place.replace(':', '')} {safe(name)}{EXT[synth]}"


def write_sounds(folder: str, synth: str, items) -> list[str]:
    """items: (place, name, data). One single-patch file per sound; a clash gets (2)."""
    from .rebuild import _free, single_file
    os.makedirs(folder, exist_ok=True)
    out = []
    for place, name, data in items:
        p = _free(os.path.join(folder, sound_file_name(synth, place, name)))
        tmp = p + ".pmtmp"
        with open(tmp, "wb") as f:
            f.write(single_file(synth, data))
        os.replace(tmp, p)
        out.append(p)
    return out


def library_hashes(db, synth: str) -> set[str]:
    """Sounds that already have a source in Factory, Paid, Free or User."""
    from .layout import LIB_TYPES
    ph = ",".join("?" * len(LIB_TYPES))
    return {r["hash"] for r in db.q(
        "SELECT DISTINCT p.hash FROM patch p JOIN patch_origin po ON po.patch_id=p.id JOIN source_file sf "
        f"ON sf.id=po.file_id JOIN source s ON s.id=sf.source_id WHERE p.synth=? AND s.type IN ({ph})",
        (synth, *LIB_TYPES))}


def blank_hashes(db, synth: str) -> set[str]:
    """Sounds marked blank in the library — init programs, including the one you made for Clear user slots."""
    return {r["hash"] for r in db.q("SELECT hash FROM patch WHERE synth=? AND blank=1", (synth,))}


def new_sounds(db, synth: str, reads: list[SlotRead], factory_banks: set[int] | None = None) -> list[SlotRead]:
    """Get from synth (spec 5.3): sounds on the synth that are in no source — once each, blanks left out. Slots in the
    synth's factory banks are left out too: those sounds are factory, and Initialize puts them in Sources › Factory."""
    have, seen, out = library_hashes(db, synth) | blank_hashes(db, synth), set(), []
    counts = Counter(r.hash for r in reads)
    fb = factory_banks or set()
    for r in reads:
        if r.blank or r.hash in have or r.hash in seen or (synth != "deluge" and counts[r.hash] >= BLANK_REPEATS) \
                or r.bank in fb:
            continue
        seen.add(r.hash); out.append(r)
    return out


def keep_in_sources(db, root: str, synth: str, items, factory_hashes: set[str], when=None, label=None) -> dict:
    """Split a read of a synth into Sources: factory sounds → Sources\\Factory\\<date> Factory read, sounds of yours that
    are in no source → Sources\\User\\<date> <Synth> read. items: (hash, place, name, data, blank). Each sound once."""
    from .layout import sources_dir, stamp
    have = library_hashes(db, synth) | blank_hashes(db, synth)
    fac, usr, seen = [], [], set()
    counts = Counter(i[0] for i in items)
    for h, place, name, data, blank in items:
        if blank or h in seen or (synth != "deluge" and counts[h] >= BLANK_REPEATS):
            continue
        seen.add(h)
        if h in factory_hashes:
            fac.append((place, name, data))
        elif h not in have:
            usr.append((place, name, data))
    st = label or stamp(when)
    res = {"factory": None, "user": None, "n_factory": len(fac), "n_user": len(usr)}
    if fac:
        res["factory"] = os.path.join(sources_dir(root, synth, "Factory"), f"{st} Factory read")
        write_sounds(res["factory"], synth, fac)
    if usr:
        res["user"] = os.path.join(sources_dir(root, synth, "User"), f"{st} {SYNTH_LABEL[synth]} read")
        write_sounds(res["user"], synth, usr)
    return res
