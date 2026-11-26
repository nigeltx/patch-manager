"""Self-update from GitHub releases. The same file is in each of Nigel's apps (Patch Manager, Deluge Sample Manager,
and the Pattern Manager once it is an installed app); only the arguments to UpdateManager differ.

Every push to main publishes a GitHub release with the Setup .exe (packaging/installer.iss). The app reads
https://api.github.com/repos/<repo>/releases/latest (the repo must be public), and when the Setup .exe there is a newer
version than the running one it asks: Update now / Later / Skip this version. Update now downloads the installer to
the temp folder, runs it silently, and quits; the installer closes the app if needed and starts the new version.

How often to check is a setting (Help ▾ › Update settings…): at start and every 6 hours, at start only, or only when
you ask. Help ▾ › Check for updates… always checks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://api.github.com/repos/{repo}/releases/latest"
EVERY = 6 * 3600                      # seconds between checks in "start_and_every" mode
MODES = {"start_and_every": "At start and every 6 hours",
         "start": "At start only",
         "manual": "Only when I ask"}
DEFAULT_MODE = "start_and_every"
SILENT = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]


@dataclass
class Update:
    version: str
    url: str
    size: int
    name: str
    notes: str


def parse_version(v: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def first_notes(body: str, limit=1400) -> str:
    """The newest section of RELEASE_NOTES.txt (the release body holds them all, newest first)."""
    lines, out = (body or "").replace("\r", "").split("\n"), []
    heads = 0
    for ln in lines:
        if re.search(r"Version \d+\.\d+\.\d+\.\s*$", ln):
            heads += 1
            if heads > 1:
                break
        out.append(ln)
    text = "\n".join(out).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def find_update(release: dict, prefix: str, current: str) -> Update | None:
    """The Setup .exe in a release, if it is newer than current. prefix e.g. 'PatchManager-Setup-'."""
    for a in release.get("assets") or []:
        name = a.get("name", "")
        if name.startswith(prefix) and name.lower().endswith(".exe"):
            v = name[len(prefix):-4]
            if parse_version(v) > parse_version(current):
                return Update(v, a.get("browser_download_url", ""), int(a.get("size") or 0), name,
                              first_notes(release.get("body", "")))
    return None


def fetch_latest(repo: str, timeout=10) -> dict:
    req = urllib.request.Request(API.format(repo=repo), headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"{repo.split('/')[-1]}-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def explain(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError) and e.code == 404:
        return "GitHub says there is no such release. The app's repository must be public for updates to work."
    if isinstance(e, urllib.error.HTTPError) and e.code == 403:
        return "GitHub is limiting requests from this PC for the moment. Try again in an hour."
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return "Could not reach GitHub — check the internet connection."
    return f"{e.__class__.__name__}: {e}"


def download(u: Update, progress=None, cancelled=None, folder=None) -> str:
    """Download the installer to the temp folder. -> its path. Raises on a short or cancelled download."""
    folder = folder or tempfile.gettempdir()
    path = os.path.join(folder, u.name)
    part = path + ".part"
    req = urllib.request.Request(u.url, headers={"User-Agent": "updater", "Accept": "application/octet-stream"})
    got = 0
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        while True:
            if cancelled and cancelled():
                raise RuntimeError("cancelled")
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, u.size)
    if u.size and got != u.size:
        raise RuntimeError(f"the download stopped short ({got:,} of {u.size:,} bytes)")
    os.replace(part, path)
    return path


def due(mode: str, last: float, now: float, at_start: bool) -> bool:
    if mode == "manual":
        return False
    if at_start:
        return True
    return mode == "start_and_every" and now - (last or 0) >= EVERY


def run_installer(path: str):
    """Start the installer silently and detached; the caller then quits so it can replace the files."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([path, *SILENT], creationflags=flags, close_fds=True)


# ---------------------------------------------------------------- the Qt side
try:
    from PySide6.QtCore import QObject, QTimer, Qt, Signal
    from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QLabel, QMessageBox,
                                   QProgressDialog, QPushButton, QVBoxLayout)
except ImportError:                     # the core above is usable (and tested) without Qt
    QObject = object


if QObject is not object:
    class UpdateManager(QObject):
        """window: the main window. get/set: read and save a setting by key (the app's own settings store)."""
        _result = Signal(object, object, bool)          # Update | None, error | None, asked by you
        _dl = Signal(int, int)
        _dl_done = Signal(object, object)               # path | None, error | None

        def __init__(self, window, app_name, repo, prefix, version, get, set_):
            super().__init__(window)
            self.window, self.app_name, self.repo, self.prefix, self.version = window, app_name, repo, prefix, version
            self.get, self.set = get, set_
            self.busy = False
            self._result.connect(self._on_result)
            self._dl.connect(self._on_progress)
            self._dl_done.connect(self._on_downloaded)
            self.timer = QTimer(self)
            self.timer.timeout.connect(lambda: self._maybe(False))

        @property
        def mode(self):
            m = self.get("update_check") or DEFAULT_MODE
            return m if m in MODES else DEFAULT_MODE

        def start(self):
            """Call once after the window is shown."""
            QTimer.singleShot(8000, lambda: self._maybe(True))
            self.timer.start(30 * 60 * 1000)

        def _maybe(self, at_start):
            if due(self.mode, float(self.get("update_last_check") or 0), time.time(), at_start):
                self.check(asked=False)

        # ---------------------------------------------------------------- checking
        def check(self, asked=True):
            if self.busy:
                return
            self.busy = True

            def work():
                try:
                    u = find_update(fetch_latest(self.repo), self.prefix, self.version)
                    self._result.emit(u, None, asked)
                except Exception as e:                  # reported in plain words only when you asked
                    self._result.emit(None, e, asked)
            threading.Thread(target=work, daemon=True).start()

        def _on_result(self, u, err, asked):
            self.busy = False
            self.set("update_last_check", str(time.time()))
            if err is not None:
                if asked:
                    QMessageBox.warning(self.window, self.app_name, "Could not check for updates.\n\n" + explain(err))
                return
            if u is None:
                if asked:
                    QMessageBox.information(self.window, self.app_name,
                                            f"{self.app_name} {self.version} is the latest version.")
                return
            if not asked and self.get("update_skip") == u.version:
                return
            if not asked and QApplication.activeModalWidget() is not None:
                QTimer.singleShot(2 * 60 * 1000, lambda: self._on_result(u, None, False))   # wait for the dialog
                return
            self._ask(u)

        def _ask(self, u):
            box = QMessageBox(self.window)
            box.setWindowTitle(f"{self.app_name} {u.version}")
            box.setIcon(QMessageBox.Information)
            box.setText(f"<b>{self.app_name} {u.version} is ready.</b> You have {self.version}.<br><br>"
                        "Update now closes the app, installs the new version and starts it again.")
            box.setDetailedText(u.notes)
            now = box.addButton("Update now", QMessageBox.AcceptRole)
            box.addButton("Later", QMessageBox.RejectRole)
            skip = box.addButton("Skip this version", QMessageBox.DestructiveRole)
            box.setDefaultButton(now)
            box.exec()
            if box.clickedButton() is now:
                self.update(u)
            elif box.clickedButton() is skip:
                self.set("update_skip", u.version)

        # ---------------------------------------------------------------- updating
        def update(self, u):
            if not getattr(sys, "frozen", False):
                QMessageBox.information(self.window, self.app_name, "This copy runs from source code, so it is not "
                                        f"updated in place. The installer is:\n{u.url}")
                return
            self._cancel = False
            self.prog = QProgressDialog(f"Downloading {self.app_name} {u.version}…", "Cancel", 0, max(u.size, 1),
                                        self.window)
            self.prog.setWindowModality(Qt.WindowModal)
            self.prog.setMinimumDuration(0)
            self.prog.canceled.connect(lambda: setattr(self, "_cancel", True))
            self.prog.show()

            def work():
                try:
                    p = download(u, lambda d, t: self._dl.emit(d, t), lambda: self._cancel)
                    self._dl_done.emit(p, None)
                except Exception as e:
                    self._dl_done.emit(None, e)
            threading.Thread(target=work, daemon=True).start()

        def _on_progress(self, d, t):
            if t:
                self.prog.setMaximum(t)
            self.prog.setValue(min(d, self.prog.maximum()))

        def _on_downloaded(self, path, err):
            self.prog.close()
            if err is not None:
                if str(err) != "cancelled":
                    QMessageBox.warning(self.window, self.app_name, "The update could not be downloaded.\n\n"
                                        + explain(err))
                return
            try:
                run_installer(path)
            except OSError as e:
                QMessageBox.warning(self.window, self.app_name, f"Could not start the installer: {e}")
                return
            QApplication.quit()

        # ---------------------------------------------------------------- the setting
        def settings_dialog(self):
            d = QDialog(self.window)
            d.setWindowTitle("Update settings")
            v = QVBoxLayout(d)
            v.addWidget(QLabel(f"<b>{self.app_name} {self.version}</b><br>Check GitHub for a new version:"))
            c = QComboBox()
            for k, text in MODES.items():
                c.addItem(text, k)
            c.setCurrentIndex(list(MODES).index(self.mode))
            v.addWidget(c)
            last = float(self.get("update_last_check") or 0)
            v.addWidget(QLabel("Last checked: " + (time.strftime("%d %b %Y %H:%M", time.localtime(last)) if last
                                                    else "never")))
            now = QPushButton("Check now"); now.clicked.connect(lambda: (d.accept(), self.check(asked=True)))
            v.addWidget(now)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(d.accept); bb.rejected.connect(d.reject)
            v.addWidget(bb)
            if d.exec():
                self.set("update_check", c.currentData())
                self.set("update_skip", "")
