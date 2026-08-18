"""Schnellerfassung: eine Zeile tippen, Enter, fertig.

Wozu
----
Die Tabelle laesst sich zwar direkt bearbeiten, aber fuer den haeufigsten
Kleinfall -- eine formlose Preismitteilung, aus der nur eine einzige
Position wird -- ist das umstaendlich: Zeile anlegen, Zelle suchen,
tippen, naechste Zelle suchen.  Hier steht alles nebeneinander, die
Tabulatortaste fuehrt durch die Felder, Enter legt die Position an und
setzt den Fokus zurueck auf das erste Feld.  So laesst sich eine
Preisliste in einem Rutsch durchtippen, ohne die Maus anzufassen.

Einfuegen aus Excel
-------------------
Wer eine Zeile aus Excel kopiert, hat Tabulatoren zwischen den Werten.
Wird so etwas in das erste Feld eingefuegt, verteilt sich der Inhalt
automatisch auf die passenden Felder -- das ist der zweithaeufigste Weg,
schnell an eine Position zu kommen.  Getrennt wird nur an Tabulatoren
und Semikolons: das sind eindeutige Trennzeichen.  An Leerzeichen wird
NICHT getrennt, denn "Dichtring NBR 40x52x7" ist eine Bezeichnung und
keine vier Werte -- lieber nichts aufteilen als falsch aufteilen.

Grundsatz wie ueberall: Es wird nichts geraten.  Jeder Wert steht in dem
Feld, in das der Anwender ihn geschrieben hat, und traegt die Herkunft
MANUAL.  Leere Felder bleiben leer.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ..models.enums import FieldOrigin
from ..models.offer_position import OfferPosition
from ..utils.parsing import parse_decimal

logger = logging.getLogger(__name__)

__all__ = ["QuickEntryBar", "split_pasted_row"]

#: Reihenfolge der Felder -- zugleich die Reihenfolge beim Einfuegen
#: einer kopierten Zeile.  (Schluessel, Beschriftung, Breite, Hinweis)
FIELDS: tuple[tuple[str, str, int, str], ...] = (
    ("material_number", "Material", 110, "Materialnummer des Lieferanten oder Ihre"),
    ("description", "Bezeichnung", 220, "Freitext"),
    ("quantity", "Menge", 80, "z. B. 500"),
    ("uom", "ME", 55, "Mengeneinheit, z. B. ST"),
    ("price", "Preis", 90, "z. B. 2,95"),
    ("price_unit", "PE", 50, "Preiseinheit, meist 1 oder 100"),
    ("currency", "Whg", 55, "z. B. EUR"),
)

#: Eindeutige Trennzeichen beim Einfuegen.  Leerzeichen fehlt hier
#: absichtlich -- siehe Modulkopf.
_SEPARATORS = ("\t", ";")


def split_pasted_row(text: str) -> list[str]:
    """Eine eingefuegte Zeile in Werte zerlegen -- oder gar nicht.

    Liefert eine leere Liste, wenn kein eindeutiges Trennzeichen
    vorkommt.  Der Aufrufer laesst den Text dann einfach stehen, wo er
    hingeschrieben wurde.
    """
    zeile = (text or "").splitlines()[0] if text else ""
    for trenner in _SEPARATORS:
        if trenner in zeile:
            return [teil.strip() for teil in zeile.split(trenner)]
    return []


class _PasteAwareLineEdit(QLineEdit):
    """Ein Eingabefeld, das eine eingefuegte Tabellenzeile weitermeldet."""

    rowPasted = Signal(list)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt-Vorgabe
        werte = split_pasted_row(source.text() if source else "")
        if len(werte) > 1:
            self.rowPasted.emit(werte)
            return
        super().insertFromMimeData(source)


class QuickEntryBar(QFrame):
    """Siehe Modulkopf."""

    #: Eine fertig befuellte Position -- das Hauptfenster haengt sie an
    positionEntered = Signal(object)
    #: Klartextmeldung fuer die Statuszeile
    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        titel = QLabel("Schnell erfassen:")
        titel.setObjectName("SubHeading")
        layout.addWidget(titel)

        self.edits: dict[str, QLineEdit] = {}
        for schluessel, beschriftung, breite, hinweis in FIELDS:
            beschriftung_label = QLabel(beschriftung)
            layout.addWidget(beschriftung_label)

            if schluessel == FIELDS[0][0]:
                edit: QLineEdit = _PasteAwareLineEdit()
                edit.rowPasted.connect(self._distribute)
            else:
                edit = QLineEdit()
            edit.setFixedWidth(breite)
            edit.setPlaceholderText(hinweis.split(",")[0])
            edit.setToolTip(hinweis)
            edit.returnPressed.connect(self.commit)
            layout.addWidget(edit)
            self.edits[schluessel] = edit

        layout.addStretch(1)

        self.add_button = QPushButton("Übernehmen")
        self.add_button.setToolTip(
            "Position anlegen (Enter). Der Fokus springt danach zurueck auf "
            "Material, damit die naechste Zeile sofort getippt werden kann.")
        self.add_button.clicked.connect(self.commit)
        layout.addWidget(self.add_button)

        self.clear_button = QPushButton("Leeren")
        self.clear_button.setToolTip("Alle Felder leeren (Esc)")
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)

    # ------------------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt-Vorgabe
        if event.key() == Qt.Key.Key_Escape:
            self.clear()
            return
        super().keyPressEvent(event)

    def focus_first(self) -> None:
        """Fokus auf das erste Feld -- fuer "Von Hand erfassen"."""
        self.edits[FIELDS[0][0]].setFocus()

    def clear(self) -> None:
        for edit in self.edits.values():
            edit.clear()
        self.focus_first()

    def values(self) -> dict[str, str]:
        return {schluessel: edit.text().strip()
                for schluessel, edit in self.edits.items()}

    # ------------------------------------------------------------------
    def _distribute(self, werte: list[str]) -> None:
        """Eine eingefuegte Zeile auf die Felder verteilen."""
        schluessel = [f[0] for f in FIELDS]
        for name, wert in zip(schluessel, werte):
            self.edits[name].setText(wert)
        ueberschuss = len(werte) - len(schluessel)
        if ueberschuss > 0:
            # Nicht kommentarlos verschlucken: der Anwender soll wissen,
            # dass seine Zeile mehr Spalten hatte als hier Platz ist.
            self.message.emit(
                f"{ueberschuss} weitere(r) Wert(e) der eingefuegten Zeile wurden "
                "nicht uebernommen -- bitte pruefen.")
        else:
            self.message.emit("Eingefuegte Zeile auf die Felder verteilt.")

    # ------------------------------------------------------------------
    def commit(self) -> None:
        """Aus den Feldern eine Position bauen und melden."""
        werte = self.values()
        if not any(werte.values()):
            self.message.emit("Nichts eingetragen -- es wurde keine Position angelegt.")
            return

        position = OfferPosition()
        uebernommen: list[str] = []
        unlesbar: list[str] = []

        for schluessel, text in werte.items():
            if not text:
                continue
            wert: object = text
            if schluessel in ("quantity", "price"):
                zahl = parse_decimal(text)
                if zahl is None:
                    # Nicht raten und nicht stillschweigend verwerfen.
                    unlesbar.append(f"{schluessel}='{text}'")
                    continue
                wert = zahl
            elif schluessel == "price_unit":
                try:
                    wert = int(text)
                except ValueError:
                    unlesbar.append(f"Preiseinheit='{text}'")
                    continue
            elif schluessel in ("uom", "currency"):
                wert = text.upper()
            position.set_field(schluessel, wert, FieldOrigin.MANUAL)
            uebernommen.append(schluessel)

        if not uebernommen:
            self.message.emit(
                "Keine verwertbare Angabe: " + ", ".join(unlesbar) +
                ". Bitte Zahlen z. B. als 2,95 eintragen.")
            return

        self.positionEntered.emit(position)
        if unlesbar:
            self.message.emit(
                "Position angelegt. Nicht uebernommen wurde: " + ", ".join(unlesbar))
        else:
            self.message.emit("Position angelegt.")
        logger.info("Schnellerfassung: Position mit %d Feld(ern) angelegt",
                    len(uebernommen))
        self.clear()
