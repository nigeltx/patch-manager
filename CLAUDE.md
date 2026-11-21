# Patch Manager

Windows GUI app (Python 3.12 + PySide6 + python-rtmidi/mido) that keeps a library of synth patches
(Take 5, Sub 37, Syntakt, Deluge) and auditions them on the synths. The design is the spec PDF in
Google Drive: Studio → Software → Patch Manager → Patch_Manager_Spec_v1.x.pdf (latest version).

The owner (Nigel) does not code or debug. Keep the code easy for Claude to change:
plain modules, tests for every core behaviour, no clever metaprogramming.

## Where things are
- `patchmgr/library` — file readers, scanner (Studio → Patches), SQLite db, search
- `patchmgr/midi` — ports hub, test phrases; `patchmgr/drivers` — Take 5, Sub 37, Syntakt
- `patchmgr/ui` — windows; `tests` — `python -m pytest -q` must pass (QT_QPA_PLATFORM=offscreen)
- `packaging` + `.github/workflows/build.yml` — PyInstaller + Inno Setup; every push to main builds an
  installer and publishes a GitHub release
- User data: %LOCALAPPDATA%\PatchManager\library.db (never in the install folder)

## Rules
- This repo is the only copy of the code. The old Drive folder (Software → Patch Manager → App) is retired.
- Bump `patchmgr/__init__.py` `__version__` and `RELEASE_NOTES.txt` for each release.
- After each release, put the Setup .exe and a source zip (`git archive`, named `Patch Manager X.Y.Z - source.zip`)
  in Google Drive at Studio → Software → Patch Manager, and move the previous version's pair into its Archive subfolder.
- Test the UI offscreen and look at screenshots before pushing.
