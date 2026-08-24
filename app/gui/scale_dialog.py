"""Mengenstaffel einer Position von Hand pflegen.

Wozu
----
Staffeln entstehen bisher nur auf einem Weg: mehrere Zeilen desselben
Materials im Angebot werden beim Verarbeiten zu einer Staffel
zusammengefasst.  Das deckt den Normalfall ab -- aber nicht:

* eine Staffel, die im Angebot als Fliesstext steht ("ab 500 Stueck
  12,85") und deshalb nicht als eigene Zeile erkannt wurde,
* eine Stufe, die der Einkaeufer nachverhandelt hat,
* eine bestehende SAP-Staffel, die uebernommen oder angepasst werden
  soll, statt sie mit einem einzelnen Preis zu ueberschreiben.

Genau das war der Grund, warum eine bestehende Staffel gefaehrlich sein
konnte: ohne sie zu sehen, haette man sie ueberschrieben.  Deshalb steht
sie hier daneben und laesst sich mit einem Klick uebernehmen.

Grundsatz wie ueberall: Es wird nichts geraten.  Unvollstaendige Zeilen
werden nicht uebernommen, sondern benannt.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.offer_position import OfferPosition
from ..utils.parsing import (format_decimal,
                             format_decimal_fuer_eingabe,
                             parse_decimal)
from .style import Colors

logger = logging.getLogger(__name__)

__all__ = ["ScaleDialog", "pruefe_stufen"]

#: So viele Zeilen stehen bereit -- SAP nimmt im Konditionsbild nicht
#: beliebig viele, und eine endlose Tabelle hilft niemandem.
_ZEILEN = 12


def pruefe_stufen(rohzeilen: list[tuple[str, str]]) -> tuple[
        list[tuple[Decimal, Decimal]], list[str]]:
    """Eingetippte Zeilen in eine Staffel verwandeln -- oder Fehler nennen.

    Liefert ``(stufen, fehler)``.  Ist ``fehler`` nicht leer, wird nichts
    uebernommen: eine halb verstandene Staffel ist schlimmer als keine,
    denn sie steht anschliessend so in SAP.

    Geprueft wird, was in SAP zu einem Fehler oder -- schlimmer -- zu
    einem falschen Preis fuehren wuerde: doppelte Ab-Mengen, negative
    Werte, Text in Zahlenfeldern.  Dass die Preise mit steigender Menge
    fallen, wird NICHT verlangt: steigende Staffeln kommen vor (kleine
    Abnahmemengen aus Lagerbestand, groessere aus Fertigung).
    """
    stufen: list[tuple[Decimal, Decimal]] = []
    fehler: list[str] = []
    gesehen: set[Decimal] = set()

    for nummer, (menge_text, preis_text) in enumerate(rohzeilen, start=1):
        menge_text = (menge_text or "").strip()
        preis_text = (preis_text or "").strip()
        if not menge_text and not preis_text:
            continue
        if not menge_text or not preis_text:
            fehler.append(f"Zeile {nummer}: "
                          + ("es fehlt der Preis" if menge_text
                             else "es fehlt die Ab-Menge"))
            continue
        menge = parse_decimal(menge_text)
        preis = parse_decimal(preis_text)
        if menge is None:
            fehler.append(f"Zeile {nummer}: „{menge_text}“ ist keine Menge")
            continue
        if preis is None:
            fehler.append(f"Zeile {nummer}: „{preis_text}“ ist kein Preis")
            continue
        if menge < 0 or preis < 0:
            fehler.append(f"Zeile {nummer}: negative Werte sind nicht moeglich")
            continue
        if menge in gesehen:
            fehler.append(f"Zeile {nummer}: die Ab-Menge {format_decimal(menge, 3)} "
                          "kommt mehrfach vor")
            continue
        gesehen.add(menge)
        stufen.append((menge, preis))

    if len(stufen) == 1:
        fehler.append("Eine Staffel braucht mindestens zwei Stufen. Fuer einen "
                      "einzelnen Preis genuegt das Preisfeld der Position.")
    return sorted(stufen, key=lambda stufe: stufe[0]), fehler


class ScaleDialog(QDialog):
    """Siehe Modulkopf."""

    def __init__(self, position: OfferPosition, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.position = position
        self.stufen: list[tuple[Decimal, Decimal]] = []
        self.setWindowTitle("Mengenstaffel pflegen")
        self.resize(560, 520)
        self._build()
        self._fuellen()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        kopf = QLabel(
            f"<b>{self.position.display_name}</b>"
            + (f" — {self.position.description}" if self.position.description else ""))
        kopf.setTextFormat(Qt.TextFormat.RichText)
        kopf.setWordWrap(True)
        layout.addWidget(kopf)

        hinweis = QLabel(
            "Die unterste Stufe ist zugleich der Grundpreis der Position. "
            "Leere Zeilen werden uebergangen.")
        hinweis.setObjectName("Muted")
        hinweis.setWordWrap(True)
        layout.addWidget(hinweis)

        self.table = QTableWidget(_ZEILEN, 2)
        self.table.setHorizontalHeaderLabels(["Ab Menge", "Preis"])
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 160)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        # -- Der SAP-Bestand daneben ---------------------------------------
        self.sap_label = QLabel("")
        self.sap_label.setWordWrap(True)
        self.sap_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.sap_label)

        self.uebernehmen_button = QPushButton("SAP-Staffel uebernehmen")
        self.uebernehmen_button.setToolTip(
            "Die in SAP hinterlegte Staffel in die Tabelle uebernehmen -- "
            "dann anpassen, statt sie zu ueberschreiben")
        self.uebernehmen_button.clicked.connect(self._sap_uebernehmen)
        self.uebernehmen_button.setVisible(False)
        layout.addWidget(self.uebernehmen_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.fehler_label = QLabel("")
        self.fehler_label.setWordWrap(True)
        self.fehler_label.setStyleSheet(f"color: {Colors.RED};")
        layout.addWidget(self.fehler_label)

        knoepfe = QHBoxLayout()
        leeren = QPushButton("Tabelle leeren")
        leeren.clicked.connect(self._leeren)
        knoepfe.addWidget(leeren)
        knoepfe.addStretch(1)
        layout.addLayout(knoepfe)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Uebernehmen")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Abbrechen")
        self.buttons.accepted.connect(self._pruefen_und_schliessen)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    # ------------------------------------------------------------------
    def _fuellen(self) -> None:
        """Vorhandene Staffel eintragen -- oder den Einzelpreis als erste Stufe."""
        vorhanden = self.position.sorted_scales()
        if not vorhanden and self.position.price is not None:
            # Kein leeres Blatt: der bekannte Preis ist die erste Stufe,
            # daneben tippt sich die zweite schneller.
            menge = self.position.quantity if self.position.quantity is not None \
                else Decimal("1")
            vorhanden = [(Decimal(menge), Decimal(self.position.price))]

        for zeile, (menge, preis) in enumerate(vorhanden[:_ZEILEN]):
            self.table.setItem(zeile, 0, QTableWidgetItem(
                format_decimal_fuer_eingabe(menge)))
            self.table.setItem(zeile, 1, QTableWidgetItem(
                format_decimal_fuer_eingabe(preis, 4)))

        self._sap_bestand_zeigen()

    def _sap_bestand_zeigen(self) -> None:
        """Was in SAP steht -- damit niemand blind ueberschreibt."""
        record = self.position.sap_info_record
        if record is None or not getattr(record, "scales_read", False):
            self.sap_label.setText(
                "<i>Der SAP-Stand wurde fuer diese Position noch nicht "
                "gelesen — eine dort vorhandene Staffel ist hier deshalb "
                "nicht zu sehen.</i>")
            return
        if not record.scales:
            self.sap_label.setText(
                "In SAP ist derzeit <b>keine Staffel</b> hinterlegt.")
            return
        text = "; ".join(f"ab {format_decimal(menge, 3)}: {format_decimal(preis)}"
                         for menge, preis in record.scales)
        self.sap_label.setText(f"In SAP steht heute: <b>{text}</b>")
        self.uebernehmen_button.setVisible(True)

    def _sap_uebernehmen(self) -> None:
        record = self.position.sap_info_record
        if record is None or not record.scales:
            return
        self._leeren()
        for zeile, (menge, preis) in enumerate(record.scales[:_ZEILEN]):
            self.table.setItem(zeile, 0, QTableWidgetItem(
                format_decimal_fuer_eingabe(Decimal(menge))))
            self.table.setItem(zeile, 1, QTableWidgetItem(
                format_decimal_fuer_eingabe(Decimal(preis), 4)))

    def _leeren(self) -> None:
        for zeile in range(_ZEILEN):
            for spalte in range(2):
                self.table.setItem(zeile, spalte, QTableWidgetItem(""))

    # ------------------------------------------------------------------
    def eingetippte_zeilen(self) -> list[tuple[str, str]]:
        zeilen = []
        for zeile in range(self.table.rowCount()):
            menge = self.table.item(zeile, 0)
            preis = self.table.item(zeile, 1)
            zeilen.append((menge.text() if menge else "",
                           preis.text() if preis else ""))
        return zeilen

    def _pruefen_und_schliessen(self) -> None:
        stufen, fehler = pruefe_stufen(self.eingetippte_zeilen())
        if fehler:
            # Nicht schliessen: der Anwender soll die Stelle sehen, um die
            # es geht, statt eine halb verstandene Staffel zu speichern.
            self.fehler_label.setText("<br>".join(fehler))
            return
        self.stufen = stufen
        self.accept()

    def uebernehmen(self) -> list[str]:
        """Das Ergebnis in die Position schreiben.  Liefert Klartextmeldungen."""
        meldungen: list[str] = []
        if not self.stufen:
            if self.position.scale_quantities:
                self.position.scale_quantities = []
                meldungen.append("Mengenstaffel entfernt -- es gilt wieder der "
                                 "einzelne Preis.")
            return meldungen

        self.position.scale_quantities = list(self.stufen)
        # Der Grundpreis ist die unterste Stufe -- so schreibt die
        # Anwendung ihn auch nach SAP.  Bliebe der alte Preis stehen,
        # widerspraechen sich Positionspreis und Staffel.
        self.position.price = self.stufen[0][1]
        meldungen.append(f"Mengenstaffel mit {len(self.stufen)} Stufen "
                         f"uebernommen: {self.position.scale_display()}")
        logger.info("Staffel von Hand gepflegt fuer %s: %s",
                    self.position.material_number, self.position.scale_display())
        return meldungen
