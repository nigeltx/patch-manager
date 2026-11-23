"""Best-guess patch types (Bass, Lead, Pad, Kick …) from the patch name, its tags and its folders — the same idea as
the Deluge Sample Manager's BY TYPE.

How a guess is made, in plain steps:
1. The name is split into words: "LD_SuperSaw-02" → "ld super saw 02".
2. The patch name is checked first. If it names a type, that is the answer.
3. Then the tags that came with the file (Syntakt sounds carry tags such as "kick" or "bass").
4. Then the folders, from the one holding the file upwards (pack folders such as ".../Basses/…").
   The synth folder and Free / Factory / Paid say nothing about type and are skipped.
The user's own choice (Set type…) is kept apart from the guess and always wins (see db.py).
Change RULES_VERSION whenever the rules change, so every guess is worked out again.
"""
from __future__ import annotations

import re

RULES_VERSION = 1

DRUMS = ["Kick", "Snare", "Clap", "Hat", "Cymbal", "Tom", "Percussion"]
MUSIC = ["Bass", "Lead", "Pad", "Keys", "Pluck", "Arp/Seq", "Strings", "Brass", "Bells", "FX"]
ALL_TYPES = MUSIC + DRUMS
UNKNOWN = "Unknown"

# (type, pattern). Patterns match whole words in the lower-case, space-separated text.
# "pd" is left out on purpose: Moog's free Sub 37 presets are all named "PD …" (public domain), not pads.
RULES = [
    ("Kick", r"kicks?|kik|kck|bd|bass ?drums?|kickdrum"),
    ("Snare", r"snares?|snr|sd|rims?|rimshots?"),
    ("Clap", r"claps?|cp|snaps?"),
    ("Hat", r"(hi ?)?hats?|hh|hihats?|closed ?hats?|open ?hats?"),
    ("Cymbal", r"cymbals?|cym|crash(es)?|rides?|splash|china"),
    ("Tom", r"toms?|floor ?toms?"),
    ("Percussion", r"percs?|percussions?|shakers?|congas?|bongos?|tambs?|tambourines?|cowbells?|claves?|"
                   r"wood ?blocks?|blocks?|triangles?|timbales?|djembes?|tablas?|cajon|gongs?|zaps?"),
    ("Bass", r"bass(es)?|basslines?|bs|sub ?bass|subs?|reese|acid ?bass|303"),
    ("Lead", r"leads?|ld|lds|solos?|sync ?leads?"),
    ("Pad", r"pads?|drones?|atmos|atmospheres?|atmospherics?|ambien(ce|t)s?|textures?|soundscapes?|choirs?|swells?"),
    ("Keys", r"keys|pianos?|pno|ep|e ?pianos?|rhodes|organs?|org|clavs?|wurli(tzer)?|harpsichords?"),
    ("Pluck", r"plucks?|plk|plks|pizz(icato)?"),
    ("Arp/Seq", r"arps?|arpeggios?|arpeggiated|seqs?|sequences?|sequenced|ostinatos?"),
    ("Strings", r"strings?|str|violins?|cellos?|ensembles?|orchestra(l)?"),
    ("Brass", r"brass|horns?|trumpets?|trombones?|sax(ophone)?s?"),
    ("Bells", r"bells?|mallets?|marimbas?|vibes|vibraphones?|glock(enspiel)?s?|chimes?|kalimbas?|music ?box"),
    ("FX", r"fx|sfx|effects?|noises?|risers?|impacts?|sweeps?|whoosh(es)?|lasers?|sirens?|glitch(es)?|"
           r"transitions?|uplifters?|downlifters?|sci ?fi"),
]
_COMPILED = [(t, re.compile(rf"(?<![a-z0-9])(?:{p})(?![a-z])")) for t, p in RULES]
_GENRES = re.compile(r"(?<![a-z])(drum (and |n )?bass|dnb|d n b|drumstep)(?![a-z])")      # genres, not types
_BASS_DRUM = re.compile(r"(?<![a-z])bass ?drums?(?![a-z])")
_SKIP_FOLDERS = {"free", "factory", "paid", "take 5", "sub 37", "syntakt", "deluge", "synths", "_patch manager",
                 "backups"}


def words(text: str) -> str:
    """'LD_SuperSaw-02.syx' → 'ld super saw 02'."""
    text = re.sub(r"\.(syx|xml|stsnd|stprj|mid)$", "", text, flags=re.I)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)           # camelCase → camel Case
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)            # Bass01 → Bass 01
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"[^A-Za-z0-9/]+", " ", text)
    return " ".join(text.lower().split())


def types_in(text: str) -> list[str]:
    text = _GENRES.sub(" ", text)
    found = [t for t, rx in _COMPILED if rx.search(text)]
    if "Kick" in found and "Bass" in found and not re.search(r"(?<![a-z])bass(es)?(?![a-z])", _BASS_DRUM.sub(" ", text)):
        found.remove("Bass")                     # "bass drum" is a kick
    return found


def guess(name: str, tags: list[str] | None = None, folders: list[str] | None = None) -> tuple[list[str], str]:
    """(types, why) for one patch."""
    t = types_in(words(name))
    if t:
        return t, "name"
    for tag in tags or []:
        t = types_in(words(tag))
        if t:
            return t, f"tag “{tag}”"
    for f in reversed(folders or []):
        if f.lower() in _SKIP_FOLDERS:
            continue
        t = types_in(words(f))
        if t:
            return t, f"folder “{f}”"
    return [UNKNOWN], "no type word found in its name, tags or folders"


def how(row) -> str:
    """'set by you' / 'guessed from the name' / 'no type word found in its name, tags or folders' for a list row."""
    if row.get("ptype_user"):
        return "set by you"
    why = row.get("ptype_why") or "name"
    return why if why.startswith("no ") else f"guessed from the {why}"


def pack(types: list[str]) -> str:
    """Stored form: ',Bass,Lead,' so a query can look for ',Bass,'."""
    return "," + ",".join(types) + "," if types else ""


def unpack(s: str | None) -> list[str]:
    return [t for t in (s or "").split(",") if t]


def find(word: str) -> str | None:
    """'bass' / 'arp' / 'arp/seq' / 'hats' → the type it names, for search (type:bass)."""
    w = word.strip().lower()
    for t in ALL_TYPES + [UNKNOWN]:
        if w == t.lower() or w in t.lower().split("/"):
            return t
    hits = types_in(words(w))
    return hits[0] if len(hits) == 1 else ("Drums" if w in ("drums", "drum") else None)
