"""Verwaltungsfenster.

Historie, Zuordnungen, SAP-Feld-IDs, Einstellungen und Protokoll sind wichtig,
aber sie gehoeren nicht in die taegliche Arbeitsflaeche.  Ein Einkaeufer, der
ein Angebot verarbeitet, braucht sie nicht -- er wuerde nur von ihnen
abgelenkt.

Deshalb liegen sie in einem eigenen Fenster, das ueber das Menue geoeffnet
wird.  Das Hauptfenster bleibt dadurch auf genau eine Aufgabe konzentriert:
Angebot pruefen und uebernehmen.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QTabWidget, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)


class AdminWindow(QDialog):
    """Nicht-modales Fenster fuer alle Verwaltungsseiten."""

    def __init__(self, pages: list[tuple[str, QWidget]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Verwaltung")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(1180, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.tabs = QTabWidget()
        for title, widget in pages:
            self.tabs.addTab(widget, title)
        layout.addWidget(self.tabs)

    def show_page(self, title: str) -> None:
        """Fenster oeffnen und die gewuenschte Seite anzeigen."""
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == title:
                self.tabs.setCurrentIndex(index)
                break
        self.show()
        self.raise_()
        self.activateWindow()
