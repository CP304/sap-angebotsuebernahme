"""SAP-Feld-IDs aus einer Scripting-Aufzeichnung uebernehmen.

Warum es diese Seite gibt
-------------------------
Die Feld-IDs des SAP GUI unterscheiden sich je nach Release und
Customizing.  Sie lassen sich weder mitliefern noch erraten -- eine
geratene ID schreibt im Zweifel in das falsche Feld, und das faellt erst
auf, wenn der Fehler schon im System steht.  Sie muessen also aus der
eigenen Anlage kommen, und der einzige verlaessliche Weg dorthin ist die
Aufzeichnung im SAP GUI.

Der Anwender muss dafuer aber NICHT wissen, wie die Felder heissen.  Das
ist der Zweck dieser Seite: Sie liest die Aufzeichnung, zeigt jede
gefundene Zeile im Klartext ("da stand 100234 drin") und laesst den
Anwender aus einer Liste waehlen, was das war.  Aus welcher Transaktion
die Aufzeichnung stammt, liest die Seite selbst aus der Datei.

Die Aufzeichnung verlaesst den Rechner nicht: sie wird hier eingefuegt
oder geoeffnet und sofort verarbeitet.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..services.vbs_parser import (
    TRANSACTION_NAMES,
    VbsField,
    detect_transaction,
    parse_vbs_recording,
)
from ..utils.textkodierung import decode_bytes

logger = logging.getLogger(__name__)

__all__ = ["VbsImporterWidget", "FIELD_MAPPINGS", "vorschlag_fuer"]

#: Auswahlliste: interner Schluessel und Klartext.  Bewusst in der
#: Reihenfolge, in der die Felder in einer Maske typischerweise stehen.
FIELD_MAPPINGS: list[tuple[str, str]] = [
    ("", "-- bitte auswaehlen --"),
    ("vendor_number", "Lieferantennummer"),
    ("material_number", "Materialnummer"),
    ("vendor_material_number", "Materialnummer des Lieferanten"),
    ("description", "Bezeichnung / Kurztext"),
    ("purchasing_org", "Einkaufsorganisation"),
    ("plant", "Werk"),
    ("quantity", "Menge"),
    ("uom", "Mengeneinheit (ST, KG ...)"),
    ("price", "Preis"),
    ("price_unit", "Preiseinheit (PE)"),
    ("currency", "Waehrung"),
    ("valid_from", "Gueltig ab"),
    ("valid_to", "Gueltig bis"),
    ("delivery_date", "Liefertermin"),
    ("lead_time_days", "Lieferzeit in Tagen"),
    ("min_order_qty", "Mindestbestellmenge"),
    ("info_category", "Infosatzart (Normal/Lohn ...)"),
    ("contract_type", "Belegart Kontrakt"),
    ("target_value", "Zielwert Kontrakt"),
    ("cost_center", "Kostenstelle"),
    ("gl_account", "Sachkonto"),
    ("_ignore", "Nicht benoetigt (ueberspringen)"),
]

#: Wortteile in einer SAP-Feld-ID, die ihre Bedeutung recht sicher
#: verraten.  Das ist NUR ein Vorschlag fuer die Vorauswahl -- bestaetigt
#: wird er vom Anwender.  Reihenfolge zaehlt: das Erste, das passt,
#: gewinnt, deshalb stehen die spezielleren Muster oben.
_ID_HINWEISE: tuple[tuple[str, str], ...] = (
    ("IDNLF", "vendor_material_number"),
    ("LIFNR", "vendor_number"),
    ("MATNR", "material_number"),
    ("EKORG", "purchasing_org"),
    ("WERKS", "plant"),
    ("MEINS", "uom"),
    ("BPRME", "uom"),
    ("PEINH", "price_unit"),
    ("NETPR", "price"),
    ("KBETR", "price"),
    ("WAERS", "currency"),
    ("DATAB", "valid_from"),
    ("DATBI", "valid_to"),
    ("APLFZ", "lead_time_days"),
    ("MINBM", "min_order_qty"),
    ("KTMNG", "quantity"),
    ("MENGE", "quantity"),
    ("KOSTL", "cost_center"),
    ("SAKTO", "gl_account"),
    ("KTWRT", "target_value"),
    ("BSART", "contract_type"),
    ("TXZ01", "description"),
    ("MAKTX", "description"),
    ("EINDT", "delivery_date"),
)


def vorschlag_fuer(field: VbsField) -> str:
    """Vorschlag fuer die Bedeutung eines Feldes -- oder nichts.

    Es wird ausschliesslich anhand des SAP-Feldnamens vorgeschlagen, denn
    der ist aussagekraeftig (``EINA-LIFNR`` ist die Lieferantennummer).
    Vom eingetippten WERT wird bewusst nicht auf die Bedeutung
    geschlossen: "1000" kann eine Einkaufsorganisation, ein Werk oder eine
    Menge sein, und ein falscher Vorschlag, den jemand ungeprueft
    bestaetigt, ist schlimmer als gar keiner.
    """
    kennung = field.field_id.upper()
    for muster, schluessel in _ID_HINWEISE:
        if muster in kennung:
            return schluessel
    return ""


class VbsImporterWidget(QWidget):
    """Siehe Modulkopf."""

    #: Meldet eine gespeicherte Zuordnung (Feld-ID -> Bedeutung)
    mappingSaved = Signal(dict)

    def __init__(self, settings=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.fields: list[VbsField] = []
        self.transaction = ""
        #: Hinweis aus der Kodierungserkennung, falls sie unsicher war
        self._kodierungshinweis = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Die Feld-IDs des SAP GUI unterscheiden sich je nach Anlage und "
            "lassen sich nicht mitliefern -- sie muessen aus Ihrem System "
            "kommen.\n\n"
            "Zeichnen Sie im SAP GUI eine Transaktion auf "
            "(Optionen → Scripting → Skript-Aufzeichnung), und fuegen "
            "Sie den Inhalt der .vbs unten ein oder oeffnen Sie die Datei. "
            "Welche Transaktion es war, erkennt diese Seite selbst.\n\n"
            "Sie muessen die Feldnamen NICHT kennen: unten steht, was in "
            "jedem Feld stand -- waehlen Sie einfach aus, was es war.")
        info.setWordWrap(True)
        layout.addWidget(info)

        # -- Eingabe ------------------------------------------------------
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(
            'Inhalt der .vbs hier einfuegen, z. B.:\n'
            'session.findById("wnd[0]/usr/ctxtEINA-LIFNR").text = "100234"')
        self.input.setMaximumHeight(110)
        # Wird von Hand etwas geaendert, gilt der Hinweis aus dem
        # Dateieinlesen nicht mehr fuer das, was jetzt dasteht.
        self.input.textChanged.connect(self._vergiss_kodierungshinweis)
        layout.addWidget(self.input)

        knopfleiste = QHBoxLayout()
        self.read_button = QPushButton("Eingefuegten Text auswerten")
        self.read_button.setToolTip(
            "Wertet aus, was oben eingefuegt wurde -- egal aus welcher "
            "Transaktion die Aufzeichnung stammt")
        self.read_button.clicked.connect(self.parse_input)
        knopfleiste.addWidget(self.read_button)

        self.open_button = QPushButton("Datei oeffnen ...")
        self.open_button.setToolTip("Eine .vbs-Datei vom Rechner einlesen")
        self.open_button.clicked.connect(self._open_file)
        knopfleiste.addWidget(self.open_button)
        knopfleiste.addStretch(1)
        layout.addLayout(knopfleiste)

        # -- Ergebnis -----------------------------------------------------
        self.transaction_label = QLabel("")
        self.transaction_label.setObjectName("SubHeading")
        layout.addWidget(self.transaction_label)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(
            ["SAP-Feld", "Das stand darin", "Das ist die/der ..."])
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(1, 190)
        self.table.setColumnWidth(2, 300)
        layout.addWidget(self.table, 1)

        fuss = QHBoxLayout()
        self.save_button = QPushButton("Zuordnung speichern")
        self.save_button.clicked.connect(self.save_mapping)
        fuss.addWidget(self.save_button)
        fuss.addStretch(1)
        layout.addLayout(fuss)

        self.status_label = QLabel("Bereit.")
        self.status_label.setObjectName("Muted")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------------
    def _vergiss_kodierungshinweis(self) -> None:
        self._kodierungshinweis = ""

    def _open_file(self) -> None:
        pfad, _filter = QFileDialog.getOpenFileName(
            self, "Aufzeichnung oeffnen", "",
            "Aufzeichnungen (*.vbs *.txt);;Alle Dateien (*)")
        if not pfad:
            return
        try:
            rohdaten = Path(pfad).read_bytes()
        except OSError as fehler:
            self.status_label.setText(f"Datei nicht lesbar: {fehler}")
            return
        # Die Aufzeichnung des SAP GUI ist UTF-16LE mit Byte-Order-Mark.
        # Sie als utf-8 oder cp1252 zu lesen ergibt Zeichensalat mit
        # Nullzeichen -- auf dem Bildschirm leere Rechtecke -- und der
        # Parser findet darin keine einzige Zeile wieder.
        inhalt, kodierung, warnung = decode_bytes(rohdaten)
        logger.info("Aufzeichnung %s gelesen (Kodierung %s)",
                    Path(pfad).name, kodierung)
        # Erst der Text -- das setzen loescht ueber textChanged einen
        # Hinweis aus einem frueheren Einlesen -- dann der neue Hinweis.
        self.input.setPlainText(inhalt)
        self._kodierungshinweis = warnung
        self.parse_input()

    def parse_input(self) -> None:
        """Den eingefuegten Text auswerten."""
        text = self.input.toPlainText()
        if not text.strip():
            self.status_label.setText("Es wurde noch nichts eingefuegt.")
            return

        self.fields = parse_vbs_recording(text)
        self.transaction = detect_transaction(text)

        if self.transaction:
            self.transaction_label.setText(
                f"Aufzeichnung aus {self.transaction} "
                f"({TRANSACTION_NAMES[self.transaction]})")
        else:
            # Kein Grund zur Sorge: die Zuordnung funktioniert auch ohne.
            self.transaction_label.setText(
                "Transaktion nicht erkennbar -- die Zuordnung geht trotzdem.")

        if not self.fields:
            self.table.setRowCount(0)
            self.status_label.setText(self._grund_fuer_leeres_ergebnis(text))
            return

        self._fill_table()
        vorbelegt = sum(1 for f in self.fields if vorschlag_fuer(f))
        meldung = (f"{len(self.fields)} Feld(er) gefunden, davon {vorbelegt} "
                   "mit Vorschlag. Bitte pruefen und ergaenzen, dann "
                   "speichern.")
        if self._kodierungshinweis:
            meldung = f"{meldung} -- {self._kodierungshinweis}"
        self.status_label.setText(meldung)

    def _grund_fuer_leeres_ergebnis(self, text: str) -> str:
        """Warum kam nichts heraus -- und was hilft?

        "Keine Felder gefunden" allein laesst den Anwender ratlos vor
        einer Datei stehen, die im SAP GUI eben noch entstanden ist.  Der
        haeufigste Grund ist die Kodierung: die Aufzeichnung ist UTF-16,
        und wer sie ueber die Zwischenablage aus einem Editor holt, der
        sie falsch geoeffnet hat, fuegt hier Zeichensalat ein.  Das ist an
        den Nullzeichen erkennbar und wird beim Namen genannt.
        """
        if self._kodierungshinweis:
            return self._kodierungshinweis
        if "\x00" in text or text.lstrip().startswith(("ÿþ", "þÿ")):
            return ("Der eingefuegte Text ist Zeichensalat -- die "
                    "Aufzeichnung wurde beim Kopieren falsch gelesen. "
                    "Bitte die .vbs ueber \"Datei oeffnen ...\" einlesen "
                    "statt den Inhalt einzufuegen.")
        if "findById" not in text:
            return ("Keine Eingabefelder gefunden: im Text steht keine "
                    "einzige Zeile mit session.findById(...). Stammt die "
                    "Datei aus der Skript-Aufzeichnung des SAP GUI?")
        return ("Keine Eingabefelder gefunden. Es gibt zwar Zeilen mit "
                "session.findById(...), aber keine davon schreibt einen "
                "Wert (.text = \"...\") -- die Aufzeichnung enthaelt "
                "offenbar nur Klicks und Tastendruecke.")

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.fields))
        bekannt = dict(getattr(self.settings, "sap_field_ids", {}) or {})

        for zeile, feld in enumerate(self.fields):
            kennung = QTableWidgetItem(feld.short_id())
            kennung.setToolTip(feld.field_id)
            kennung.setFlags(kennung.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(zeile, 0, kennung)

            wert = QTableWidgetItem(feld.value)
            wert.setFlags(wert.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(zeile, 1, wert)

            auswahl = QComboBox()
            for schluessel, beschriftung in FIELD_MAPPINGS:
                auswahl.addItem(beschriftung, schluessel)
            # Eine frueher gespeicherte Zuordnung hat Vorrang vor dem
            # Vorschlag -- der Anwender hat sie ja schon bestaetigt.
            gewuenscht = bekannt.get(feld.field_id) or vorschlag_fuer(feld)
            if gewuenscht:
                index = auswahl.findData(gewuenscht)
                if index >= 0:
                    auswahl.setCurrentIndex(index)
            self.table.setCellWidget(zeile, 2, auswahl)

    # ------------------------------------------------------------------
    def current_mapping(self) -> dict[str, str]:
        """Was in der Tabelle gerade eingestellt ist."""
        zuordnung: dict[str, str] = {}
        for zeile, feld in enumerate(self.fields):
            auswahl = self.table.cellWidget(zeile, 2)
            if not isinstance(auswahl, QComboBox):
                continue
            schluessel = auswahl.currentData()
            if schluessel and schluessel != "_ignore":
                zuordnung[feld.field_id] = schluessel
        return zuordnung

    def save_mapping(self) -> None:
        """Zuordnung uebernehmen und in den Einstellungen sichern."""
        if not self.fields:
            self.status_label.setText("Es wurde noch nichts ausgewertet.")
            return

        zuordnung = self.current_mapping()
        if not zuordnung:
            self.status_label.setText(
                "Kein Feld zugeordnet -- es wurde nichts gespeichert.")
            return

        # Zwei Felder auf dieselbe Bedeutung ist fast immer ein Versehen und
        # wuerde beim Schreiben in SAP im falschen Feld landen.
        umgekehrt: dict[str, list[str]] = {}
        for kennung, bedeutung in zuordnung.items():
            umgekehrt.setdefault(bedeutung, []).append(kennung)
        doppelt = {b: k for b, k in umgekehrt.items() if len(k) > 1}
        if doppelt:
            klartext = dict(FIELD_MAPPINGS)
            zeilen = "\n".join(
                f"- {klartext.get(b, b)}: {len(k)} Felder"
                for b, k in doppelt.items())
            antwort = QMessageBox.question(
                self, "Mehrfach vergeben",
                "Folgende Bedeutungen sind mehr als einmal vergeben:\n\n"
                f"{zeilen}\n\nDas ist meist ein Versehen. Trotzdem speichern?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if antwort != QMessageBox.StandardButton.Yes:
                self.status_label.setText("Nicht gespeichert.")
                return

        if self.settings is not None:
            bestand = dict(getattr(self.settings, "sap_field_ids", {}) or {})
            # Ergaenzen, nicht ersetzen: die vier Vorgaenge werden nach und
            # nach aufgezeichnet, und eine neue Aufzeichnung darf die
            # bereits gepflegten Felder nicht loeschen.
            bestand.update(zuordnung)
            self.settings.sap_field_ids = bestand
            try:
                self.settings.save()
            except OSError as fehler:
                self.status_label.setText(f"Speichern fehlgeschlagen: {fehler}")
                return
            gesamt = len(bestand)
        else:
            gesamt = len(zuordnung)

        self.mappingSaved.emit(zuordnung)
        self.status_label.setText(
            f"{len(zuordnung)} Feld(er) uebernommen -- insgesamt sind jetzt "
            f"{gesamt} Feld-IDs gepflegt.")
        logger.info("SAP-Feld-IDs gespeichert: %d aus %s",
                    len(zuordnung), self.transaction or "unbekannter Transaktion")
