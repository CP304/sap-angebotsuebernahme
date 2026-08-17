"""SAP-GUI-Scripting-Verbindung.

Diese Klasse ist die *einzige* Stelle im Projekt, die ``win32com`` anfasst.
Alles darueber (Services, GUI) arbeitet nur noch mit fachlichen Methoden.

Wartekonzept
------------
Es wird bewusst **nicht** mit ``sleep(2)``-Kaskaden gearbeitet.  Stattdessen:

* ``_wait_while_busy()`` pollt ``session.Busy`` -- SAP meldet selbst, wann es
  fertig ist.
* ``wait_for_element()`` pollt gezielt auf das Element, das als naechstes
  gebraucht wird.
* Nach jedem Schritt wird die Statusleiste ausgewertet (E/A/W-Meldungen).
* Unerwartete Popups fuehren zum kontrollierten Abbruch -- es wird **nie**
  blind Enter gedrueckt.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config.settings import SapRuntime
from .selectors import SelectorRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fehlertypen
# ---------------------------------------------------------------------------

class SapError(RuntimeError):
    """Basisklasse aller SAP-Fehler dieser Anwendung."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class SapNotAvailableError(SapError):
    """SAP GUI laeuft nicht bzw. es besteht keine Anmeldung."""


class SapScriptingDisabledError(SapError):
    """Scripting ist am Frontend oder am Applikationsserver deaktiviert."""


class SapElementNotFoundError(SapError):
    """Ein erwartetes Bildelement war nicht (rechtzeitig) da."""


class SapPopupError(SapError):
    """Ein unerwartetes modales Fenster ist aufgetaucht."""

    def __init__(self, message: str, popup_text: str = "", title: str = "") -> None:
        super().__init__(message, detail=popup_text)
        self.popup_text = popup_text
        self.title = title


class SapBusinessError(SapError):
    """SAP hat eine fachliche Fehlermeldung (Typ E/A) zurueckgegeben."""

    def __init__(self, message: str, message_id: str = "", number: str = "") -> None:
        super().__init__(message)
        self.message_id = message_id
        self.number = number


class SapWriteBlockedError(SapError):
    """Schreiben ist gesperrt (Dry Run oder ungepruefte Selektoren)."""


# ---------------------------------------------------------------------------
# Hilfsobjekte
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """Beschreibung einer offenen SAP-Session."""

    connection_index: int
    session_index: int
    system: str = ""
    client: str = ""
    user: str = ""
    language: str = ""
    transaction: str = ""
    description: str = ""

    def label(self) -> str:
        parts = [p for p in (self.system, f"Mandant {self.client}" if self.client else "",
                             self.user) if p]
        base = " / ".join(parts) or f"Verbindung {self.connection_index}"
        if self.transaction:
            base += f"  [{self.transaction}]"
        return f"{base}  (Session {self.session_index + 1})"


@dataclass
class StatusMessage:
    """Inhalt der SAP-Statusleiste."""

    type: str = ""          # S=Erfolg, W=Warnung, E=Fehler, A=Abbruch, I=Info
    text: str = ""
    message_id: str = ""
    number: str = ""

    @property
    def is_error(self) -> bool:
        return self.type in ("E", "A")

    @property
    def is_warning(self) -> bool:
        return self.type == "W"

    @property
    def is_success(self) -> bool:
        return self.type == "S"

    def display(self) -> str:
        if not self.text:
            return ""
        prefix = {"S": "Erfolg", "W": "Warnung", "E": "Fehler", "A": "Abbruch",
                  "I": "Hinweis"}.get(self.type, self.type)
        code = f" ({self.message_id} {self.number})" if self.message_id else ""
        return f"{prefix}: {self.text}{code}"


# ---------------------------------------------------------------------------
# Verbindung
# ---------------------------------------------------------------------------

class SapGuiConnection:
    """Kapselt Zugriff auf eine bestehende SAP-GUI-Session.

    Die Anwendung startet SAP *nicht* selbst und meldet sich *nicht* an -- sie
    nutzt ausschliesslich eine bereits vom Anwender geoeffnete Session.  Das
    ist bewusst so: keine Zugangsdaten im Werkzeug.
    """

    def __init__(self, runtime: SapRuntime, selectors: SelectorRegistry) -> None:
        self.runtime = runtime
        self.selectors = selectors

        self._sap_gui: Any = None
        self._application: Any = None
        self._connection: Any = None
        self._session: Any = None
        self._info: SessionInfo | None = None

        #: Harte Schreibsperre auf unterster Ebene.  Wird von den Services
        #: bewusst freigegeben; im Dry Run bleibt sie zu.
        self.allow_write: bool = False

        #: Protokoll aller ausgefuehrten Schritte (fuer die Fehleranalyse)
        self.trace: list[str] = []

    # ------------------------------------------------------------------
    # Verbindungsaufbau
    # ------------------------------------------------------------------
    def connect(self) -> SessionInfo:
        """Bestehende SAP-GUI-Session uebernehmen."""
        try:
            import win32com.client  # noqa: PLC0415 - optionale Abhaengigkeit
        except ImportError as exc:
            raise SapNotAvailableError(
                "Das Modul 'pywin32' ist nicht installiert. Ohne pywin32 ist kein "
                "SAP GUI Scripting moeglich.",
                detail="pip install pywin32",
            ) from exc

        try:
            self._sap_gui = win32com.client.GetObject("SAPGUI")
        except Exception as exc:  # noqa: BLE001 - COM-Fehler sind unspezifisch
            raise SapNotAvailableError(
                "Es wurde keine laufende SAP-GUI gefunden. Bitte SAP Logon starten "
                "und an einem System anmelden.",
                detail=str(exc),
            ) from exc

        if self._sap_gui is None:
            raise SapNotAvailableError("SAP GUI antwortet nicht.")

        try:
            self._application = self._sap_gui.GetScriptingEngine
        except Exception as exc:  # noqa: BLE001
            raise SapScriptingDisabledError(
                "Die SAP-GUI-Scripting-Schnittstelle ist nicht verfuegbar. "
                "Bitte im SAP Logon unter Optionen -> Zugriffshilfen & Scripting -> "
                "Scripting das Scripting aktivieren (und ggf. den Systemparameter "
                "sapgui/user_scripting im System setzen lassen).",
                detail=str(exc),
            ) from exc

        if getattr(self._application, "Children", None) is None or \
                self._application.Children.Count == 0:
            raise SapNotAvailableError(
                "SAP GUI laeuft, es ist aber keine Systemverbindung offen. "
                "Bitte an einem System anmelden."
            )

        index = min(self.runtime.connection_index, self._application.Children.Count - 1)
        self._connection = self._application.Children(index)

        if self._connection.Children.Count == 0:
            raise SapNotAvailableError("Die SAP-Verbindung hat keine offene Session.")

        session_index = min(self.runtime.session_index, self._connection.Children.Count - 1)
        self._session = self._connection.Children(session_index)

        self._info = self._read_session_info(index, session_index, self._session)
        logger.info("SAP-Verbindung hergestellt: %s", self._info.label())
        self._trace(f"connect -> {self._info.label()}")
        return self._info

    def disconnect(self) -> None:
        """Referenzen loesen (die SAP-Session selbst bleibt offen)."""
        self._session = None
        self._connection = None
        self._application = None
        self._sap_gui = None
        self._info = None
        self.allow_write = False
        logger.info("SAP-Verbindung getrennt (Session bleibt geoeffnet).")

    def is_connected(self) -> bool:
        if self._session is None:
            return False
        try:
            _ = self._session.Info.SystemName   # provoziert COM-Fehler bei totem Handle
            return True
        except Exception:  # noqa: BLE001
            logger.warning("SAP-Session ist nicht mehr erreichbar.")
            return False

    @property
    def session_info(self) -> SessionInfo | None:
        return self._info

    @property
    def session(self) -> Any:
        if self._session is None:
            raise SapNotAvailableError("Keine SAP-Session verbunden.")
        return self._session

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def get_sessions(self) -> list[SessionInfo]:
        """Alle offenen Verbindungen/Sessions auflisten."""
        if self._application is None:
            try:
                self.connect()
            except SapError:
                return []
        sessions: list[SessionInfo] = []
        try:
            for c_index in range(self._application.Children.Count):
                connection = self._application.Children(c_index)
                for s_index in range(connection.Children.Count):
                    session = connection.Children(s_index)
                    sessions.append(self._read_session_info(c_index, s_index, session))
        except Exception as exc:  # noqa: BLE001
            logger.error("Sessions konnten nicht gelesen werden: %s", exc)
        return sessions

    def select_session(self, connection_index: int, session_index: int) -> SessionInfo:
        """Auf eine bestimmte Session umschalten."""
        if self._application is None:
            self.connect()
        try:
            self._connection = self._application.Children(connection_index)
            self._session = self._connection.Children(session_index)
        except Exception as exc:  # noqa: BLE001
            raise SapNotAvailableError(
                f"Session {connection_index}/{session_index} ist nicht verfuegbar.",
                detail=str(exc),
            ) from exc
        self.runtime.connection_index = connection_index
        self.runtime.session_index = session_index
        self._info = self._read_session_info(connection_index, session_index, self._session)
        logger.info("Session gewechselt: %s", self._info.label())
        return self._info

    @staticmethod
    def _read_session_info(c_index: int, s_index: int, session: Any) -> SessionInfo:
        info = SessionInfo(connection_index=c_index, session_index=s_index)
        try:
            raw = session.Info
            info.system = str(raw.SystemName or "")
            info.client = str(raw.Client or "")
            info.user = str(raw.User or "")
            info.language = str(raw.Language or "")
            info.transaction = str(raw.Transaction or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Session-Info nicht lesbar: %s", exc)
        return info

    @property
    def sap_user(self) -> str:
        return self._info.user if self._info else ""

    @property
    def sap_system(self) -> str:
        return self._info.system if self._info else ""

    # ------------------------------------------------------------------
    # Warten
    # ------------------------------------------------------------------
    def _wait_while_busy(self, timeout: float | None = None) -> None:
        """Warten, bis SAP die Verarbeitung abgeschlossen hat."""
        limit = time.monotonic() + (timeout or self.runtime.element_timeout_s)
        while time.monotonic() < limit:
            try:
                if not bool(self._session.Busy):
                    return
            except Exception:  # noqa: BLE001 - Busy ist nicht ueberall verfuegbar
                return
            time.sleep(self.runtime.poll_interval_s)
        logger.warning("SAP war laenger als %.1fs beschaeftigt.", timeout or
                       self.runtime.element_timeout_s)

    def wait_for_element(self, element_id: str, timeout: float | None = None) -> Any:
        """Auf ein Element warten und es zurueckgeben."""
        limit = time.monotonic() + (timeout or self.runtime.element_timeout_s)
        last_error = ""
        while time.monotonic() < limit:
            element = self.find_element(element_id, required=False)
            if element is not None:
                return element
            popup = self.detect_popup()
            if popup is not None:
                raise SapPopupError(
                    "SAP zeigt ein unerwartetes Fenster an. Die Verarbeitung wurde "
                    "angehalten.",
                    popup_text=popup.get("text", ""),
                    title=popup.get("title", ""),
                )
            time.sleep(self.runtime.poll_interval_s)
        raise SapElementNotFoundError(
            "Ein erwartetes SAP-Feld war nicht auffindbar. Vermutlich weicht die "
            "Maske ab oder die hinterlegte Feld-ID stimmt nicht.",
            detail=f"Element-ID: {element_id}{(' | ' + last_error) if last_error else ''}",
        )

    def wait_for_transaction(self, transaction: str, timeout: float | None = None) -> bool:
        """Warten, bis die angegebene Transaktion aktiv ist."""
        limit = time.monotonic() + (timeout or self.runtime.element_timeout_s)
        wanted = transaction.upper().lstrip("/N").lstrip("/")
        while time.monotonic() < limit:
            current = self.current_transaction().upper()
            if current == wanted:
                return True
            time.sleep(self.runtime.poll_interval_s)
        return False

    # ------------------------------------------------------------------
    # Elementzugriff
    # ------------------------------------------------------------------
    def find_element(self, element_id: str, required: bool = True) -> Any:
        """Element suchen.  ``required=False`` liefert ``None`` statt Fehler."""
        try:
            element = self._session.findById(element_id, False)
        except Exception as exc:  # noqa: BLE001
            element = None
            logger.debug("findById('%s') fehlgeschlagen: %s", element_id, exc)
        if element is None and required:
            raise SapElementNotFoundError(
                "Ein erwartetes SAP-Feld wurde nicht gefunden.",
                detail=f"Element-ID: {element_id}",
            )
        return element

    def exists(self, element_id: str) -> bool:
        return self.find_element(element_id, required=False) is not None

    def _retry(self, action, description: str):
        """COM-Aufruf mit begrenzter Wiederholung (sporadische RPC-Fehler)."""
        attempts = max(1, self.runtime.retry_count + 1)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return action()
            except (SapError, SapPopupError):
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("%s fehlgeschlagen (Versuch %d/%d): %s",
                               description, attempt + 1, attempts, exc)
                if attempt + 1 < attempts:
                    time.sleep(self.runtime.retry_delay_s)
        raise SapError(
            f"SAP hat auf '{description}' nicht reagiert.",
            detail=str(last_exc) if last_exc else "",
        )

    def set_text(self, element_id: str, value: str, wait: bool = False) -> None:
        """Textfeld setzen."""
        text = "" if value is None else str(value)
        element = self.wait_for_element(element_id) if wait else self.find_element(element_id)

        def _do():
            element.text = text

        self._retry(_do, f"Feld setzen ({element_id})")
        self._trace(f"set_text {element_id} = {text!r}")

    def set_checkbox(self, element_id: str, checked: bool) -> None:
        element = self.find_element(element_id)

        def _do():
            element.selected = bool(checked)

        self._retry(_do, f"Ankreuzfeld setzen ({element_id})")
        self._trace(f"set_checkbox {element_id} = {checked}")

    def set_combo(self, element_id: str, key: str) -> None:
        element = self.find_element(element_id)

        def _do():
            element.key = str(key)

        self._retry(_do, f"Auswahlfeld setzen ({element_id})")
        self._trace(f"set_combo {element_id} = {key!r}")

    def read_text(self, element_id: str, default: str = "") -> str:
        """Feldinhalt lesen; nicht vorhandene Felder liefern ``default``."""
        element = self.find_element(element_id, required=False)
        if element is None:
            return default
        try:
            return str(element.text).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Text von '%s' nicht lesbar: %s", element_id, exc)
            return default

    def read_checkbox(self, element_id: str, default: bool = False) -> bool:
        element = self.find_element(element_id, required=False)
        if element is None:
            return default
        try:
            return bool(element.selected)
        except Exception:  # noqa: BLE001
            return default

    def press_button(self, element_id: str, expect_write: bool = False) -> None:
        """Schaltflaeche druecken."""
        if expect_write:
            self._guard_write(f"Schaltflaeche {element_id}")
        element = self.find_element(element_id)

        def _do():
            element.press()

        self._retry(_do, f"Schaltflaeche druecken ({element_id})")
        self._trace(f"press_button {element_id}")
        self._wait_while_busy()

    def select_element(self, element_id: str) -> None:
        """Registerkarte/Knoten auswaehlen."""
        element = self.find_element(element_id)

        def _do():
            element.select()

        self._retry(_do, f"Element auswaehlen ({element_id})")
        self._trace(f"select {element_id}")
        self._wait_while_busy()

    def set_focus(self, element_id: str) -> None:
        element = self.find_element(element_id, required=False)
        if element is None:
            return
        try:
            element.setFocus()
        except Exception as exc:  # noqa: BLE001
            logger.debug("setFocus('%s') fehlgeschlagen: %s", element_id, exc)

    def send_vkey(self, key: int, window: str = "wnd[0]", expect_write: bool = False) -> None:
        """Funktionstaste senden (0=Enter, 3=F3, 11=Sichern, 12=Abbrechen)."""
        if expect_write or key == 11:
            self._guard_write(f"Funktionstaste {key}")
        target = self.find_element(window)

        def _do():
            target.sendVKey(key)

        self._retry(_do, f"Funktionstaste {key} senden")
        self._trace(f"send_vkey {key} @ {window}")
        self._wait_while_busy()

    # ------------------------------------------------------------------
    # Tabellen (Table-Control)
    # ------------------------------------------------------------------
    def table_row_count(self, table_id: str) -> int:
        table = self.find_element(table_id, required=False)
        if table is None:
            return 0
        try:
            return int(table.rowCount)
        except Exception:  # noqa: BLE001
            return 0

    def table_visible_rows(self, table_id: str) -> int:
        table = self.find_element(table_id, required=False)
        if table is None:
            return 0
        try:
            return int(table.visibleRowCount)
        except Exception:  # noqa: BLE001
            return 0

    def scroll_table(self, table_id: str, top_row: int) -> None:
        """Table-Control scrollen -- Zellen-IDs beziehen sich auf den
        *sichtbaren* Bereich, deshalb ist Scrollen zwingend."""
        table = self.find_element(table_id, required=False)
        if table is None:
            return
        try:
            table.verticalScrollbar.position = int(top_row)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Scrollen in '%s' nicht moeglich: %s", table_id, exc)
        self._wait_while_busy()

    # ------------------------------------------------------------------
    # Transaktionen
    # ------------------------------------------------------------------
    def current_transaction(self) -> str:
        try:
            return str(self._session.Info.Transaction or "")
        except Exception:  # noqa: BLE001
            return ""

    def start_transaction(self, transaction: str, force: bool = True) -> None:
        """Transaktion aufrufen.

        ``force=True`` verwendet ``/n``, beendet also den aktuellen Vorgang.
        Das ist beabsichtigt: Wir wollen immer auf einem definierten Einstieg
        starten und nicht in einer halb ausgefuellten Maske landen.
        """
        code = transaction.strip().upper()
        if not code:
            raise SapError("Es wurde keine Transaktion angegeben.")

        self.ensure_no_popup()
        command = f"/n{code}" if force and not code.startswith("/") else code
        ok_code = self.selectors.id_for("common", "ok_code")

        self.set_text(ok_code, command)
        self.send_vkey(0)
        self._wait_while_busy()

        status = self.read_status()
        if status.is_error:
            raise SapBusinessError(
                f"Transaktion {code} konnte nicht gestartet werden: {status.text}",
                message_id=status.message_id, number=status.number,
            )

        popup = self.detect_popup()
        if popup is not None:
            raise SapPopupError(
                f"Beim Start von {code} erschien ein unerwartetes Fenster.",
                popup_text=popup.get("text", ""), title=popup.get("title", ""),
            )

        if not self.wait_for_transaction(code, timeout=self.runtime.element_timeout_s):
            actual = self.current_transaction()
            raise SapError(
                f"Transaktion {code} wurde nicht aktiv (aktuell: {actual or 'unbekannt'}). "
                f"Moeglicherweise fehlt die Berechtigung.",
            )
        self._trace(f"start_transaction {code}")
        logger.debug("Transaktion %s gestartet", code)

    def ensure_transaction(self, transaction: str) -> None:
        """Nur wechseln, wenn noetig -- spart teure Transaktionswechsel."""
        if self.current_transaction().upper() == transaction.strip().upper():
            return
        self.start_transaction(transaction)

    def leave_transaction(self) -> None:
        """Sauber zum SAP-Easy-Access zurueck (ohne zu sichern)."""
        try:
            self.set_text(self.selectors.id_for("common", "ok_code"), "/n")
            self.send_vkey(0)
        except SapError as exc:
            logger.debug("Verlassen der Transaktion fehlgeschlagen: %s", exc)

    # ------------------------------------------------------------------
    # Status und Popups
    # ------------------------------------------------------------------
    def read_status(self) -> StatusMessage:
        """Statusleiste auswerten."""
        if not self.runtime.read_status_bar:
            return StatusMessage()
        bar = self.find_element(self.selectors.id_for("common", "status_bar"), required=False)
        if bar is None:
            return StatusMessage()
        message = StatusMessage()
        try:
            message.type = str(getattr(bar, "messageType", "") or "")
            message.text = str(getattr(bar, "text", "") or "").strip()
            message.message_id = str(getattr(bar, "messageId", "") or "")
            message.number = str(getattr(bar, "messageNumber", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Statusleiste nicht lesbar: %s", exc)
        if message.text:
            self._trace(f"status[{message.type}] {message.text}")
        return message

    def raise_on_error_status(self, context: str = "") -> StatusMessage:
        """Statusleiste lesen und bei E/A-Meldung abbrechen."""
        status = self.read_status()
        if status.is_error:
            prefix = f"{context}: " if context else ""
            raise SapBusinessError(f"{prefix}{status.text}",
                                   message_id=status.message_id, number=status.number)
        return status

    def detect_popup(self) -> dict[str, str] | None:
        """Modales Fenster erkennen und dessen Text einsammeln.

        Es wird bewusst **nicht** automatisch bestaetigt.  Der Aufrufer
        entscheidet, ob das Popup erwartet war.
        """
        try:
            if self._session.Children.Count <= 1:
                return None
            window = self._session.findById("wnd[1]", False)
            if window is None:
                return None
        except Exception:  # noqa: BLE001
            return None

        title = ""
        try:
            title = str(window.text or "")
        except Exception:  # noqa: BLE001
            pass

        texts: list[str] = []
        try:
            self._collect_texts(window, texts, depth=0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Popup-Text nicht vollstaendig lesbar: %s", exc)

        body = " | ".join(dict.fromkeys(t for t in texts if t))[:1000]
        logger.warning("Popup erkannt: %s -- %s", title, body)
        self._trace(f"popup {title}: {body}")
        return {"title": title, "text": body}

    def _collect_texts(self, element: Any, sink: list[str], depth: int) -> None:
        """Rekursiv Beschriftungen eines Fensters einsammeln (max. Tiefe 6)."""
        if depth > 6 or len(sink) > 60:
            return
        try:
            text = str(getattr(element, "text", "") or "").strip()
            if text and len(text) > 1:
                sink.append(text)
        except Exception:  # noqa: BLE001
            pass
        try:
            children = element.Children
            for index in range(children.Count):
                self._collect_texts(children(index), sink, depth + 1)
        except Exception:  # noqa: BLE001
            return

    def ensure_no_popup(self) -> None:
        """Sicherstellen, dass kein Fenster den naechsten Schritt blockiert."""
        popup = self.detect_popup()
        if popup is None:
            return
        raise SapPopupError(
            "Vor dem naechsten Schritt ist ein SAP-Fenster offen. Bitte pruefen "
            "und schliessen.",
            popup_text=popup.get("text", ""), title=popup.get("title", ""),
        )

    def close_popup(self, accept: bool = False) -> bool:
        """Popup gezielt schliessen.

        ``accept=True`` bestaetigt (Enter), sonst wird abgebrochen (F12).
        Wird nur nach *bewusster* Entscheidung des Aufrufers verwendet --
        niemals automatisch in einer Schleife.
        """
        if self.detect_popup() is None:
            return False
        key = 0 if accept else 12
        try:
            self.send_vkey(key, window="wnd[1]")
        except SapError as exc:
            logger.warning("Popup konnte nicht geschlossen werden: %s", exc)
            return False
        self._trace(f"close_popup accept={accept}")
        return True

    # ------------------------------------------------------------------
    # Schreibschutz
    # ------------------------------------------------------------------
    def _guard_write(self, what: str) -> None:
        if not self.allow_write:
            raise SapWriteBlockedError(
                "Schreibender SAP-Zugriff ist derzeit gesperrt (Dry Run oder noch "
                "nicht freigegebene Feld-IDs).",
                detail=what,
            )

    def press_save(self) -> StatusMessage:
        """Sichern (F11) und Ergebnis der Statusleiste zurueckgeben."""
        self._guard_write("Sichern")
        self.send_vkey(11, expect_write=True)
        status = self.read_status()
        self._trace(f"save -> {status.display()}")
        return status

    # ------------------------------------------------------------------
    # Diagnose
    # ------------------------------------------------------------------
    def _trace(self, line: str) -> None:
        self.trace.append(line)
        if len(self.trace) > 500:
            del self.trace[:250]

    def take_trace(self) -> list[str]:
        lines = list(self.trace)
        self.trace.clear()
        return lines

    def describe(self) -> str:
        if not self.is_connected():
            return "nicht verbunden"
        info = self._info
        return info.label() if info else "verbunden"
