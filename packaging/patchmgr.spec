# PyInstaller build for Windows: one folder, no console window.
# Run from the repo root:  pyinstaller packaging/patchmgr.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(ROOT, "patchmgr", "icon.ico"), "patchmgr")]
datas += collect_data_files("soundfile") + collect_data_files("sounddevice")
binaries = collect_dynamic_libs("soundfile") + collect_dynamic_libs("sounddevice") + collect_dynamic_libs("rtmidi")

a = Analysis([os.path.join(ROOT, "run_patchmgr.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=["_soundfile_data", "_sounddevice_data", "rtmidi", "mido.backends.rtmidi"],
             excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PatchManager", console=False,
          icon=os.path.join(ROOT, "patchmgr", "icon.ico"))
coll = COLLECT(exe, a.binaries, a.datas, name="PatchManager")
