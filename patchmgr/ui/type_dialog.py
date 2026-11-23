"""Set type…: choose the type(s) of one or more patches, or go back to the app's guess (like the Sample Manager)."""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QLabel, QPushButton, QVBoxLayout

from ..library import ptypes


class TypeDialog(QDialog):
    """result_value: ("set", types) / ("guess",) / None when cancelled."""

    def __init__(self, parent, count: int, current: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Set type")
        self.result_value = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Type for {count:,} patch{'es' if count != 1 else ''}. Tick one or more."))
        self.boxes = {}
        for title, names in (("Sounds", ptypes.MUSIC), ("Drums", ptypes.DRUMS)):
            g = QGroupBox(title)
            grid = QGridLayout(g)
            for k, t in enumerate(names):
                cb = QCheckBox(t)
                cb.setChecked(t in current)
                self.boxes[t] = cb
                grid.addWidget(cb, k // 4, k % 4)
            lay.addWidget(g)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        back = QPushButton("Go back to the app's guess")
        back.setToolTip("Forget your choice for these patches and use the guess from the name, tags and folders")
        bb.addButton(back, QDialogButtonBox.ResetRole)
        back.clicked.connect(self._back)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self):
        chosen = [t for t, cb in self.boxes.items() if cb.isChecked()]
        self.result_value = ("set", chosen or [ptypes.UNKNOWN])
        self.accept()

    def _back(self):
        self.result_value = ("guess",)
        self.accept()
