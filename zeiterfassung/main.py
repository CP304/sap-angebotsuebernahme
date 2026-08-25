"""Einstiegspunkt der Zeiterfassung.

Das Programm lebt im Infobereich der Taskleiste:

* jede Minute ein Herzschlag in die Datenbank (Nachweis der Laufzeit),
* alle 60 ms die Abfrage der Tastenkombination fuer das Mini-Fenster,
* beim Start und nach dem Aufwachen die Rueckfrage zu Luecken im Tag,
* beim Abmelden oder Herunterfahren ein sauberes Sitzungsende.

Aufruf:
    python -m zeiterfassung.main               # mit Fenster
    python -m zeiterfassung.main --hintergrund # nur Infobereich (Autostart)
    python -m zeiterfassung.main --export ...  # ohne Oberflaeche exportieren
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__, autostart
from .config import Einstellungen
from .excel_export import exportieren
from .gui.gap_dialog import LueckenDialog
from .gui.main_window import Hauptfenster
from .gui.mini_window import MiniFenster
from .hotkey import HotkeyWaechter
from .rules import als_stunden
from .storage import Datenbank
from .tracker import OffeneLuecke, Zeiterfassung

EINZELSTUECK = "Zeiterfassung-Instanz"
HOTKEY_TAKT_MS = 60

# Haelt die laufende Anwendung am Leben.
_LAUFEND: list["Anwendung"] = []


def _symbol(farbe: str = "#2E5AAC") -> QIcon:
    """Kleines Ziffernblatt -- damit kein Bildmaterial mitgeliefert werden muss."""
    bild = QPixmap(64, 64)
    bild.fill(QColor(0, 0, 0, 0))
    maler = QPainter(bild)
    maler.setRenderHint(QPainter.Antialiasing)
    maler.setBrush(QColor(farbe))
    maler.setPen(QPen(QColor("#FFFFFF"), 4))
    maler.drawEllipse(4, 4, 56, 56)
    maler.drawLine(32, 32, 32, 16)
    maler.drawLine(32, 32, 45, 38)
    maler.end()
    return QIcon(bild)


class Anwendung:
    """Haelt Aufzeichnung, Hotkey und Oberflaeche zusammen."""

    def __init__(self, qt_anwendung: QApplication, im_hintergrund: bool) -> None:
        self.qt = qt_anwendung
        self.einstellungen = Einstellungen.laden()
        self.zeiterfassung = Zeiterfassung(Datenbank(), self.einstellungen)

        self.fenster = Hauptfenster(self.zeiterfassung)
        self.mini = MiniFenster(self.zeiterfassung)
        self.mini.uebersicht_gewuenscht.connect(self._fenster_zeigen)
        self._offene_luecken: list[OffeneLuecke] = []
        self._dialog_offen = False

        self._tray = self._infobereich_einrichten()
        self._letzter_herzschlag = datetime.now()

        # Beim allerersten Start den Autostart selbst einrichten.
        if self.einstellungen.autostart and not autostart.ist_eingerichtet():
            erfolg, meldung = autostart.einrichten()
            if not erfolg:
                self._tray.showMessage(APP_NAME, meldung, QSystemTrayIcon.Warning, 8000)

        self._offene_luecken = self.zeiterfassung.starten()
        self._herzschlag_timer = QTimer(self.qt)
        self._herzschlag_timer.setInterval(self.einstellungen.herzschlag_sekunden * 1000)
        self._herzschlag_timer.timeout.connect(self._herzschlag)
        self._herzschlag_timer.start()

        self._hotkey = HotkeyWaechter(self.einstellungen.hotkey)
        if self._hotkey.verfuegbar:
            self._hotkey_timer = QTimer(self.qt)
            self._hotkey_timer.setInterval(HOTKEY_TAKT_MS)
            self._hotkey_timer.timeout.connect(self._hotkey_pruefen)
            self._hotkey_timer.start()

        self.qt.aboutToQuit.connect(self._beenden)
        # Windows meldet das Abmelden oder Herunterfahren ueber die
        # Sitzungsverwaltung -- damit endet die Zeiterfassung punktgenau und
        # nicht erst mit dem letzten Herzschlag.
        self.qt.commitDataRequest.connect(self._abmelden)
        if not im_hintergrund:
            self.fenster.show()
        else:
            self._tray.showMessage(
                APP_NAME,
                f"Zeiterfassung laeuft. {self.einstellungen.hotkey} zeigt die Tageszahlen.",
                QSystemTrayIcon.Information,
                5000,
            )
        # Rueckfragen erst stellen, wenn die Oberflaeche steht.
        QTimer.singleShot(1200, self._luecken_abfragen)

    # -- Infobereich --------------------------------------------------------
    def _infobereich_einrichten(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(_symbol(), self.qt)
        tray.setToolTip(APP_NAME)
        menue = QMenu()

        oeffnen = QAction("Uebersicht oeffnen", menue)
        oeffnen.triggered.connect(self._fenster_zeigen)
        menue.addAction(oeffnen)

        kennzahlen = QAction(f"Tageszahlen ({self.einstellungen.hotkey})", menue)
        kennzahlen.triggered.connect(self._kurz_zeigen)
        menue.addAction(kennzahlen)

        eintragen = QAction("Urlaub / Krank eintragen...", menue)
        eintragen.triggered.connect(self._abwesenheit_eintragen)
        menue.addAction(eintragen)

        export = QAction("Diese Woche als Excel...", menue)
        export.triggered.connect(self._woche_exportieren)
        menue.addAction(export)

        menue.addSeparator()
        beenden = QAction("Zeiterfassung beenden", menue)
        beenden.triggered.connect(self.qt.quit)
        menue.addAction(beenden)

        tray.setContextMenu(menue)
        tray.activated.connect(
            lambda grund: self._fenster_zeigen() if grund == QSystemTrayIcon.DoubleClick else None
        )
        tray.show()
        return tray

    def _fenster_zeigen(self) -> None:
        self.fenster.aktualisieren()
        self.fenster.show()
        self.fenster.raise_()
        self.fenster.activateWindow()

    def _kurz_zeigen(self) -> None:
        """Mini-Fenster kurz einblenden (fuer den Weg ueber das Menue)."""
        self.mini.einblenden()
        QTimer.singleShot(4000, self.mini.loslassen)

    def _abwesenheit_eintragen(self) -> None:
        self._fenster_zeigen()
        self.fenster.abwesenheit_eintragen()

    def _woche_exportieren(self) -> None:
        self.fenster.zeitraum_diese_woche()
        self._fenster_zeigen()
        self.fenster.export_starten()

    # -- Takte --------------------------------------------------------------
    def _herzschlag(self) -> None:
        jetzt = datetime.now()
        abstand = jetzt - self._letzter_herzschlag
        grenze = timedelta(minutes=self.einstellungen.luecke_ab_minuten)
        if abstand > grenze + timedelta(seconds=self.einstellungen.herzschlag_sekunden):
            # Der Rechner hat geschlafen oder war ausgelastet -- die Zeit
            # dazwischen ist nicht belegt und wird nachgefragt.
            self.zeiterfassung.beenden(self._letzter_herzschlag)
            neue = self.zeiterfassung.starten(jetzt)
            self._offene_luecken.extend(neue)
            self._luecken_abfragen()
        else:
            self.zeiterfassung.herzschlag(jetzt)
        self._letzter_herzschlag = jetzt
        if self.fenster.isVisible():
            self.fenster.aktualisieren()

    def _hotkey_pruefen(self) -> None:
        self._hotkey.pruefen(self.mini.einblenden, self.mini.loslassen)

    # -- Rueckfragen --------------------------------------------------------
    def _luecken_abfragen(self) -> None:
        if self._dialog_offen:
            return
        self._dialog_offen = True
        try:
            while self._offene_luecken:
                luecke = self._offene_luecken.pop(0)
                dialog = LueckenDialog(luecke)
                if dialog.exec() == LueckenDialog.Accepted:
                    art, notiz = dialog.ergebnis()
                    self.zeiterfassung.luecke_einordnen(luecke, art, notiz)
        finally:
            self._dialog_offen = False
        if self.fenster.isVisible():
            self.fenster.aktualisieren()

    # -- Ende ---------------------------------------------------------------
    def _abmelden(self, verwaltung) -> None:
        """Windows faehrt herunter: Sitzung sofort sauber abschliessen."""
        verwaltung.setRestartHint(verwaltung.RestartNever)
        self.zeiterfassung.beenden()

    def _beenden(self) -> None:
        self.zeiterfassung.beenden()
        self.zeiterfassung.db.schliessen()


def _bereits_gestartet() -> bool:
    """Verhindert, dass Autostart und Handstart doppelt aufzeichnen."""
    probe = QLocalSocket()
    probe.connectToServer(EINZELSTUECK)
    if probe.waitForConnected(300):
        probe.close()
        return True
    QLocalServer.removeServer(EINZELSTUECK)
    server = QLocalServer()
    server.listen(EINZELSTUECK)
    _bereits_gestartet.server = server  # Referenz halten, sonst schliesst Qt sie
    return False


def _export_ohne_oberflaeche(argumente) -> int:
    einstellungen = Einstellungen.laden()
    zeiterfassung = Zeiterfassung(Datenbank(), einstellungen)
    heute = date.today()
    von = date.fromisoformat(argumente.von) if argumente.von else heute - timedelta(days=heute.weekday())
    bis = date.fromisoformat(argumente.bis) if argumente.bis else heute
    ziel = exportieren(zeiterfassung, von, bis, Path(argumente.export))
    tag = zeiterfassung.tag()
    print(f"Export geschrieben: {ziel}")
    print(f"Heute {als_stunden(tag.arbeitszeit)} von {als_stunden(tag.soll)}")
    zeiterfassung.db.schliessen()
    return 0


def main(argumente: list[str] | None = None) -> int:
    zerleger = argparse.ArgumentParser(prog="zeiterfassung", description="Zeiterfassung")
    zerleger.add_argument("--hintergrund", action="store_true", help="ohne Fenster starten")
    zerleger.add_argument("--export", metavar="DATEI", help="Zeitraum als .xlsx schreiben und beenden")
    zerleger.add_argument("--von", metavar="JJJJ-MM-TT")
    zerleger.add_argument("--bis", metavar="JJJJ-MM-TT")
    zerleger.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    gewaehlt = zerleger.parse_args(argumente)

    if gewaehlt.export:
        return _export_ohne_oberflaeche(gewaehlt)

    qt = QApplication(sys.argv[:1])
    qt.setApplicationName(APP_NAME)
    qt.setQuitOnLastWindowClosed(False)  # Fenster schliessen beendet nicht

    if _bereits_gestartet():
        QMessageBox.information(
            None, APP_NAME, "Die Zeiterfassung laeuft bereits (Symbol im Infobereich)."
        )
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(
            None,
            APP_NAME,
            "Der Infobereich der Taskleiste ist nicht verfuegbar.\n"
            "Die Zeiterfassung laeuft mit Fenster weiter.",
        )

    # Referenz festhalten -- sonst raeumt Python Fenster und Timer weg.
    _LAUFEND.append(Anwendung(qt, im_hintergrund=gewaehlt.hintergrund))
    return qt.exec()


if __name__ == "__main__":
    sys.exit(main())
