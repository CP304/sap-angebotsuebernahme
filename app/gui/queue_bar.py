"""Leiste fuer den Arbeitsvorrat.

Sie erscheint nur, wenn tatsaechlich mehrere Angebote offen sind -- bei einem
einzelnen Angebot waere sie nur zusaetzlicher Ballast auf dem Bildschirm.

Bewusst sparsam: eine Zeile mit Fortschritt, dem aktuellen Angebot und drei
Schaltflaechen.  Die vollstaendige Liste liegt hinter einem Klick, nicht
dauerhaft im Blickfeld.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)

from ..services.queue_service import OfferQueue, QueueState
from .style import Colors

logger = logging.getLogger(__name__)

#: Farbe je Bearbeitungsstand
_STATE_COLORS = {
    QueueState.PENDING: Colors.TEXT_MUTED,
    QueueState.LOADED: Colors.ACCENT,
    QueueState.PROCESSED: Colors.GREEN,
    QueueState.FAILED_IMPORT: Colors.RED,
    QueueState.SKIPPED: Colors.GREY,
}


class QueueBar(QFrame):
    """Eine Zeile: Fortschritt, Auswahl, Weiterschalten."""

    entrySelected = Signal(int)     # Index im Arbeitsvorrat
    skipRequested = Signal()
    nextRequested = Signal()
    clearRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._queue: OfferQueue | None = None
        self._loading = False
        self._build()
        self.setVisible(False)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        beschriftung = QLabel("Arbeitsvorrat")
        beschriftung.setObjectName("FieldLabel")
        layout.addWidget(beschriftung)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(150)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        self.selector = QComboBox()
        self.selector.setMinimumWidth(320)
        self.selector.setToolTip("Angebot zum Bearbeiten auswaehlen")
        self.selector.currentIndexChanged.connect(self._selection_changed)
        layout.addWidget(self.selector, 1)

        self.skip_button = QPushButton("Ueberspringen")
        self.skip_button.setToolTip(
            "Dieses Angebot nicht verarbeiten und zum naechsten wechseln")
        self.skip_button.clicked.connect(self.skipRequested.emit)
        layout.addWidget(self.skip_button)

        self.next_button = QPushButton("Naechstes Angebot")
        self.next_button.setToolTip("Zum naechsten offenen Angebot wechseln")
        self.next_button.clicked.connect(self.nextRequested.emit)
        layout.addWidget(self.next_button)

        self.clear_button = QPushButton("Vorrat leeren")
        self.clear_button.setObjectName("Danger")
        self.clear_button.clicked.connect(self.clearRequested.emit)
        layout.addWidget(self.clear_button)

    # ------------------------------------------------------------------
    def bind(self, queue: OfferQueue) -> None:
        self._queue = queue
        self.refresh()

    def refresh(self) -> None:
        """Anzeige an den Stand des Arbeitsvorrats angleichen."""
        queue = self._queue
        if queue is None or queue.total <= 1:
            # Bei einem einzelnen Angebot bleibt die Leiste unsichtbar
            self.setVisible(False)
            return

        self.setVisible(True)
        self._loading = True
        try:
            self.progress.setMaximum(queue.total)
            self.progress.setValue(queue.done)
            self.progress.setFormat(f"{queue.done}/{queue.total}")

            self.selector.clear()
            for index, entry in enumerate(queue.entries):
                symbol = {
                    QueueState.PENDING: "○",
                    QueueState.LOADED: "▶",
                    QueueState.PROCESSED: "✓",
                    QueueState.FAILED_IMPORT: "✗",
                    QueueState.SKIPPED: "–",
                }.get(entry.state, "○")
                text = f"{symbol}  {entry.name}"
                ergebnis = entry.result_text()
                if ergebnis:
                    text += f"   ({ergebnis})"
                self.selector.addItem(text, index)
                self.selector.setItemData(
                    index, _STATE_COLORS.get(entry.state, Colors.TEXT),
                    Qt.ItemDataRole.ToolTipRole)
            if 0 <= queue.current_index < queue.total:
                self.selector.setCurrentIndex(queue.current_index)
        finally:
            self._loading = False

        offen = queue.pending
        self.next_button.setEnabled(offen > 0)
        self.next_button.setText(f"Naechstes Angebot ({offen})" if offen
                                 else "Alle bearbeitet")
        aktuell = queue.current
        self.skip_button.setEnabled(aktuell is not None
                                    and not aktuell.state.is_done)

    def _selection_changed(self, index: int) -> None:
        if self._loading or index < 0:
            return
        self.entrySelected.emit(index)


class QueueSummaryWidget(QWidget):
    """Abschlussuebersicht, wenn der Arbeitsvorrat leer gearbeitet ist."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
