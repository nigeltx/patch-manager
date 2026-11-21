"""Preview playback on the PC (spec 4.5): WAV, AIFF, FLAC and MP3 through the default output."""
from __future__ import annotations

import threading

try:
    import soundfile as sf
    import sounddevice as sd
    AVAILABLE, WHY = True, ""
except Exception as e:                       # noqa: BLE001 — missing PortAudio or the wheels
    sf = sd = None
    AVAILABLE, WHY = False, f"preview playback is unavailable ({e.__class__.__name__}: {e})"


class Player:
    def __init__(self):
        self.path = None
        self._lock = threading.Lock()

    def play(self, path: str):
        if not AVAILABLE:
            raise RuntimeError(WHY)
        self.stop()
        data, rate = sf.read(path, dtype="float32", always_2d=True)
        with self._lock:
            sd.play(data, rate)
            self.path = path

    def stop(self):
        if AVAILABLE:
            with self._lock:
                sd.stop()
                self.path = None

    def playing(self) -> bool:
        if not AVAILABLE or not self.path:
            return False
        try:
            return sd.get_stream().active
        except Exception:                    # noqa: BLE001
            return False
