# Patch Manager

Windows GUI app (Python 3.12 + PySide6 + python-rtmidi/mido) that keeps a library of synth patches
(Take 5, Sub 37, Syntakt, Deluge) and auditions them on the synths. The design is the spec PDF in
Google Drive: Studio → Software → Patch Manager → Patch_Manager_Spec_v1.x.pdf (latest version).

The owner (Nigel) does not code or debug. Keep the code easy for Claude to change:
plain modules, tests for every core behaviour, no clever metaprogramming.

## Where things are
- `patchmgr/library` — file readers, scanner (Studio → Patches, zips in place, fingerprints), SQLite db, search;
  `layout.py` Sources/Patches folders + Reorganize, `rebuild.py` Patches from Sources (spec v1.10 section 4)
- `patchmgr/midi` — ports hub, test phrases; `patchmgr/drivers` — Take 5 (incl. user-slot writes), Sub 37, Syntakt, Deluge
- `patchmgr/library/duplicates.py` + DuplicatesDialog — duplicates in Sources: set-aside copies (table dup_aside,
  view live_origin = the copies that count; read queries use live_origin) (spec v1.12 4.12)
- `patchmgr/library/userslots.py` + `ui/userslots.py` — Take 5 Clear / Fill user slots, Send to slot (grid), Sync with the
  "For Take 5" crate, Initialize picked slots to the init (Basic Program, taken from the edit buffer), undo (spec v1.12 5.2, 7.1, 7.8–7.10); run() keeps a sound in no source in Sources › User before writing over it
- `patchmgr/updater.py` — self-update from GitHub releases (same file in each of Nigel's apps; repo must be public)
- `patchmgr/ui` — windows; `tests` — `python -m pytest -q` must pass (QT_QPA_PLATFORM=offscreen)
- `packaging` + `.github/workflows/build.yml` — PyInstaller + Inno Setup; every push to main builds an
  installer and publishes a GitHub release
- User data: %LOCALAPPDATA%\PatchManager\library.db (never in the install folder)

## Rules
- Each synth folder: Sources (Factory|Paid|Free|User), Patches (<Type>), Backups (dated dumps — never a source, never scanned).
- Sources are never written except new reads added by the app;
  Patches (<Synth>\Patches\<Type>) is written only by rebuild.py, after showing its plan.
- This repo is the only copy of the code. The old Drive folder (Software → Patch Manager → App) is retired.
- Bump `patchmgr/__init__.py` `__version__` and `RELEASE_NOTES.txt` for each release.
- After each release, put the Setup .exe and a source zip (`git archive`, named `Patch Manager X.Y.Z - source.zip`)
  in Google Drive at Studio → Software → Patch Manager, and move the previous version's pair into its Archive subfolder.
- Test the UI offscreen and look at screenshots before pushing.
