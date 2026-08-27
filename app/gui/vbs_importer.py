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
import re
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

from ..sap.feldnamen import beschreibe_feld
from ..sap.selectors import SelectorRegistry
from ..services.vbs_parser import (
    TRANSACTION_NAMES,
    VbsField,
    detect_transaction,
    parse_vbs_recording,
)
from ..utils.textkodierung import decode_bytes, entferne_nullzeichen

logger = logging.getLogger(__name__)

__all__ = ["VbsImporterWidget"]

#: Steuerungspraefixe eines Bedienelements -- sie sagen nur, ob es ein
#: Textfeld, ein Ankreuzfeld oder eine Schaltflaeche ist, und nichts ueber
#: die Bedeutung.
_PRAEFIXE = ("ctxt", "txt", "cmbx", "cmb", "chk", "rad", "lbl", "btn",
             "tbl", "tabs", "tabp", "ssub", "sub")


def _technischer_name(element_id: str) -> str:
    """Der Feldname am Ende einer ID -- ohne Pfad, Praefix und Index.

    ``wnd[0]/usr/ctxtEINA-LIFNR`` und
    ``wnd[0]/usr/subSUB:SAPL:0030/txtEINA-LIFNR[1,0]`` ergeben beide
    ``EINA-LIFNR``.  Der Pfad unterscheidet sich je nach Bildaufbau und
    Release, der Feldname nicht -- deshalb wird ueber ihn verglichen.
    """
    rest = (element_id or "").split("/")[-1]
    for praefix in _PRAEFIXE:
        if rest.lower().startswith(praefix):
            rest = rest[len(praefix):]
            break
    return re.sub(r"\[[^\]]*\]$", "", rest).upper()


def _feldteil(technischer_name: str) -> str:
    """Nur der Feldname hinter dem Bindestrich: ``EKKO-LIFNR`` -> ``LIFNR``."""
    return technischer_name.rpartition("-")[2] or technischer_name


class _AufzeichnungsFeld(QPlainTextEdit):
    """Ein Eingabefeld, das eingefuegten Zeichensalat sofort geradezieht.

    Die Aufzeichnung ist UTF-16.  Wer sie ueber einen Editor kopiert, der
    sie falsch geoeffnet hat, fuegt hier Text mit einem Nullzeichen
    zwischen je zwei Buchstaben ein -- auf dem Bildschirm ein Rechteck,
    dann ein Buchstabe, dann wieder ein Rechteck.

    Ausgewertet wurde so ein Text auch bisher schon richtig, die
    *Anzeige* blieb aber Zeichensalat.  Wer das sieht, haelt es fuer
    kaputt und liest gar nicht erst weiter -- deshalb wird schon beim
    Einfuegen bereinigt, nicht erst beim Auswerten.
    """

    #: Meldet, dass eingefuegter Text bereinigt werden musste.
    salatBereinigt = Signal()

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt-Vorgabe
        text = source.text() if source else ""
        if "\x00" in text:
            self.insertPlainText(entferne_nullzeichen(text))
            self.salatBereinigt.emit()
            return
        super().insertFromMimeData(source)


class VbsImporterWidget(QWidget):
    """Siehe Modulkopf."""

    #: Meldet die gespeicherte Zuordnung: (Bildschirm, Element) -> Feld-ID.
    #: ``object`` statt ``dict``, weil Qt ein Dict mit Tupelschluesseln
    #: nicht nach C++ uebersetzen kann.
    mappingSaved = Signal(object)

    def __init__(self, settings=None, registry=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        #: Dorthin wird gespeichert -- dieselbe Ablage, aus der die
        #: Schreibschicht ihre IDs holt.  Ohne sie waere diese Seite eine
        #: Sackgasse: der Anwender ordnet zu, und es wirkt nirgends.
        self.registry = registry if registry is not None else SelectorRegistry()
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
        self.input = _AufzeichnungsFeld()
        self.input.salatBereinigt.connect(self._melde_bereinigung)
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
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            ["SAP-Feld", "Das stand darin", "So heisst das Feld in SAP",
             "Das ist die/der ..."])
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 300)
        self.table.setColumnWidth(3, 260)
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
    def _melde_bereinigung(self) -> None:
        """Nach dem Einfuegen von Zeichensalat: sagen, was geschehen ist.

        Der Hinweis wird gemerkt statt nur angezeigt: gleich darauf setzt
        das Auswerten seine eigene Meldung, und ein Hinweis, der davon
        ueberschrieben wird, ist keiner.
        """
        self._kodierungshinweis = (
            "Der eingefuegte Text war Zeichensalat und wurde geradegezogen "
            "-- zuverlaessiger ist \"Datei oeffnen ...\", dann bleiben auch "
            "Umlaute erhalten.")
        self.status_label.setText(self._kodierungshinweis)

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
        if "\x00" in text:
            # Kommt der Text nicht ueber die Zwischenablage herein --
            # etwa per Ziehen und Ablegen --, greift die Bereinigung des
            # Eingabefelds nicht.  Dann eben hier, und sichtbar: der
            # Anwender soll nicht auf Zeichensalat blicken, waehrend
            # darunter die richtigen Werte stehen.
            text = entferne_nullzeichen(text)
            self.input.setPlainText(text)
            self._kodierungshinweis = (
                "Der Text war Zeichensalat und wurde geradegezogen -- "
                "zuverlaessiger ist \"Datei oeffnen ...\".")
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
        vorbelegt = sum(1 for f in self.fields
                        if self._vorschlag_aus_registry(f))
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

    def _auswahlmoeglichkeiten(self) -> list[tuple[str, str]]:
        """Was zur Auswahl steht -- die Felder der erkannten Transaktion.

        Frueher stand hier eine feste Liste allgemeiner Bedeutungen
        ("Lieferantennummer", "Preis").  Die passte zu keiner Transaktion
        richtig: eine Lieferantennummer gibt es im Infosatz, im Kontrakt
        und in der Bestellung, und es sind drei verschiedene Felder mit
        drei verschiedenen IDs.  Gespeichert wurde trotzdem nur eine --
        die naechste Aufzeichnung ueberschrieb sie oder galt als Dublette.

        Angeboten werden deshalb die Felder der Bildschirme, die zu dieser
        Transaktion gehoeren, jeweils mit ihrem Bildschirm davor.  Wird
        die Transaktion nicht erkannt, steht alles zur Auswahl.
        """
        eintraege: list[tuple[str, str]] = [("", "-- bitte auswaehlen --")]
        for screen_key in self.registry.screens_fuer_transaktion(self.transaction):
            screen = self.registry.screens[screen_key]
            for element_key, selector in screen.elements.items():
                eintraege.append(
                    (f"{screen_key}.{element_key}",
                     f"{screen.title or screen_key}: {selector.description}"))
        eintraege.append(("_ignore", "Nicht benoetigt (ueberspringen)"))
        return eintraege

    def _vorschlag_aus_registry(self, feld: VbsField) -> str:
        """Welches Feld der Registry meint diese aufgezeichnete ID?

        Verglichen wird der technische Feldname am Ende (``EINA-LIFNR``),
        nicht der ganze Pfad -- der unterscheidet sich je nach Bildaufbau
        und Release, der Feldname nicht.
        """
        gesucht = _technischer_name(feld.field_id)
        if not gesucht:
            return ""

        screens = self.registry.screens_fuer_transaktion(self.transaction)
        kandidaten: list[tuple[str, str]] = []
        for screen_key in screens:
            screen = self.registry.screens[screen_key]
            for element_key, selector in screen.elements.items():
                if not selector.id:
                    continue
                hinterlegt = _technischer_name(selector.id)
                if hinterlegt == gesucht:
                    # Voller Treffer: Tabelle und Feld stimmen ueberein.
                    return f"{screen_key}.{element_key}"
                if _feldteil(hinterlegt) == _feldteil(gesucht):
                    kandidaten.append((screen_key, element_key))

        # Zweite Stufe: nur der Feldname stimmt, die Bildstruktur davor
        # nicht (``EKKO-LIFNR`` gegen ``RM06E-LIFNR``).  Das kommt bei
        # abweichendem Bildaufbau staendig vor und ist ein guter Hinweis
        # -- aber nur, solange er eindeutig ist.  Gibt es mehrere Felder
        # desselben Namens, wird nichts vorgeschlagen: ein falscher
        # Vorschlag, den jemand ungeprueft bestaetigt, schreibt spaeter in
        # das falsche Feld.
        if len(kandidaten) == 1:
            screen_key, element_key = kandidaten[0]
            return f"{screen_key}.{element_key}"
        return ""

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.fields))
        moeglichkeiten = self._auswahlmoeglichkeiten()

        for zeile, feld in enumerate(self.fields):
            kennung = QTableWidgetItem(feld.short_id())
            erklaerung = beschreibe_feld(feld.field_id)
            kennung.setToolTip(f"{feld.field_id}\n\n{erklaerung}"
                               if erklaerung else feld.field_id)
            kennung.setFlags(kennung.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(zeile, 0, kennung)

            wert = QTableWidgetItem(feld.value)
            wert.setFlags(wert.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(zeile, 1, wert)

            # Die Lesehilfe: SAP-Feldnamen sind nicht kryptisch, nur
            # abgekuerzt -- und ueber alle Anlagen hinweg dieselben.  Ohne
            # sie muesste der Anwender raten oder mit auffaelligen
            # Testwerten herumprobieren, bis er weiss, welches Feld das ist.
            klartext = beschreibe_feld(feld.field_id)
            bedeutung = QTableWidgetItem(klartext)
            bedeutung.setFlags(bedeutung.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if not klartext:
                bedeutung.setText("– dieser Feldname ist hier nicht hinterlegt –")
                bedeutung.setToolTip(
                    "Kein Grund zur Sorge: der Wert daneben verraet meist, "
                    "worum es geht. Waehlen Sie rechts aus, was es ist.")
            self.table.setItem(zeile, 2, bedeutung)

            auswahl = QComboBox()
            for schluessel, beschriftung in moeglichkeiten:
                auswahl.addItem(beschriftung, schluessel)
            gewuenscht = self._vorschlag_aus_registry(feld)
            if gewuenscht:
                index = auswahl.findData(gewuenscht)
                if index >= 0:
                    auswahl.setCurrentIndex(index)
            self.table.setCellWidget(zeile, 3, auswahl)

    # ------------------------------------------------------------------
    def current_mapping(self) -> dict[tuple[str, str], str]:
        """Was in der Tabelle eingestellt ist: Feld der Maske -> Feld-ID.

        Der Schluessel ist das Paar aus Bildschirm und Element, so wie die
        Schreibschicht seine IDs anfordert (``id_for("info_record_initial",
        "vendor")``).  Damit wirkt die Zuordnung dort, wo sie gebraucht
        wird, statt in einer eigenen flachen Liste zu enden.
        """
        zuordnung: dict[tuple[str, str], str] = {}
        for zeile, feld in enumerate(self.fields):
            auswahl = self.table.cellWidget(zeile, 3)
            if not isinstance(auswahl, QComboBox):
                continue
            schluessel = auswahl.currentData()
            if not schluessel or schluessel == "_ignore":
                continue
            screen_key, _, element_key = str(schluessel).partition(".")
            if screen_key and element_key:
                zuordnung[(screen_key, element_key)] = feld.field_id
        return zuordnung

    def save_mapping(self) -> None:
        """Zuordnung in die Selektorenablage uebernehmen und sichern.

        Gespeichert wird dorthin, wo die Schreibschicht ihre IDs holt --
        je Bildschirm und Element.  Frueher landete alles in einer flachen
        Liste neben den Einstellungen, die niemand las: der Anwender
        ordnete zu, bestaetigte, bekam eine Erfolgsmeldung, und beim
        Schreiben nach SAP fehlten die IDs trotzdem.
        """
        if not self.fields:
            self.status_label.setText("Es wurde noch nichts ausgewertet.")
            return

        zuordnung = self.current_mapping()
        if not zuordnung:
            self.status_label.setText(
                "Kein Feld zugeordnet -- es wurde nichts gespeichert.")
            return

        # Zwei aufgezeichnete Felder auf dasselbe Feld der Maske ist fast
        # immer ein Versehen und wuerde beim Schreiben im falschen Feld
        # landen.  Geprueft wird je Bildschirm: dass es den Lieferanten im
        # Infosatz UND im Kontrakt gibt, ist dagegen voellig richtig -- und
        # galt frueher faelschlich als Dublette.
        umgekehrt: dict[str, list[str]] = {}
        for (screen_key, element_key), feld_id in zuordnung.items():
            umgekehrt.setdefault(f"{screen_key}.{element_key}", []).append(feld_id)
        doppelt = {b: k for b, k in umgekehrt.items() if len(k) > 1}
        if doppelt:
            zeilen = "\n".join(f"- {b}: {len(k)} Felder"
                                for b, k in doppelt.items())
            antwort = QMessageBox.question(
                self, "Mehrfach vergeben",
                "Folgende Felder sind mehr als einmal vergeben:\n\n"
                f"{zeilen}\n\nDas ist meist ein Versehen. Trotzdem speichern?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if antwort != QMessageBox.StandardButton.Yes:
                self.status_label.setText("Nicht gespeichert.")
                return

        for (screen_key, element_key), feld_id in zuordnung.items():
            self.registry.set_id(screen_key, element_key, feld_id)

        if self.settings is not None:
            try:
                self.registry.save(self.settings.selectors_file)
            except OSError as fehler:
                self.status_label.setText(f"Speichern fehlgeschlagen: {fehler}")
                return

        betroffene = sorted({screen for screen, _ in zuordnung})
        self.mappingSaved.emit(dict(zuordnung))
        self.status_label.setText(
            f"{len(zuordnung)} Feld-ID(s) uebernommen "
            f"({', '.join(betroffene)}). Die Felder gelten als ungeprueft, "
            "bis Sie sie unter 'SAP-Feld-IDs' bestaetigen -- vorher schreibt "
            "die Anwendung damit nicht in SAP.")
        logger.info("SAP-Feld-IDs gespeichert: %d aus %s",
                    len(zuordnung), self.transaction or "unbekannter Transaktion")
