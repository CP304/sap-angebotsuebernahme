"""Fremdwaehrungsbehandlung.

Der entscheidende Grundsatz dieses Moduls steht bewusst ganz oben, damit ihn
niemand uebersieht:

    Es wird **niemals** ein umgerechneter Betrag nach SAP geschrieben.

SAP fuehrt eigene Umrechnungskurse.  Ein hier umgerechneter Wert waere eine
zweite, abweichende Wahrheit im System -- und niemand koennte spaeter noch
sagen, welcher der beiden Betraege der richtige war.  In den Infosatz geht
deshalb immer der Originalbetrag in der Originalwaehrung des Angebots.

Wozu dann ueberhaupt ein Kurs?  Ausschliesslich fuer den **Vergleich**: Fuehrt
der Infosatz 12,40 EUR und nennt das Angebot 14,20 USD, ist die Aussage
"Aenderung +14,5 %" ohne Umrechnung sinnlos -- und ohne Umrechnung gar nicht
berechenbar.  Genau dort greift der Kurs, nirgends sonst.

Zweiter Grundsatz, wie ueberall im Projekt: Es wird nie geraten.  Eine
unbekannte Waehrung liefert ``None`` und niemals den Kurs 1 -- ein
stillschweigend angenommener Einserkurs waere der gefaehrlichste Fehler, den
diese Anwendung machen koennte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP

from ..utils.parsing import format_decimal, parse_date

logger = logging.getLogger(__name__)

__all__ = ["ConversionResult", "CurrencyService"]

#: Nachkommastellen eines umgerechneten Betrags.  Bewusst mehr als zwei: Der
#: Wert dient dem Vergleich, nicht der Buchhaltung -- durch fruehes Runden
#: entstuenden Scheinabweichungen in der Prozentangabe.
_CONVERT_DECIMALS = Decimal("0.0001")


@dataclass
class ConversionResult:
    """Ergebnis einer Umrechnung -- samt Herkunft.

    Ein umgerechneter Betrag darf nie allein durch die Anwendung wandern.  Wer
    ihn anzeigt, muss auch sagen koennen, aus welchem Betrag und mit welchem
    Kurs er entstanden ist; sonst wird aus einer Rechenhilfe unbemerkt eine
    vermeintliche Tatsache.
    """

    #: Originalbetrag (unveraendert, so wie er im Angebot steht)
    amount: Decimal | None = None
    #: Originalwaehrung des Angebots
    currency: str = ""
    #: Verwendeter Kurs (``None``, wenn nicht umgerechnet werden konnte)
    rate: Decimal | None = None
    #: Umgerechneter Betrag (``None``, wenn kein Kurs vorliegt)
    converted: Decimal | None = None
    #: Klartext zur Herkunft, z. B. "= 13,06 EUR bei Kurs 0,92"
    note: str = ""
    #: Zielwaehrung der Umrechnung
    target_currency: str = ""

    @property
    def ok(self) -> bool:
        """Liegt ein brauchbarer umgerechneter Betrag vor?"""
        return self.converted is not None

    @property
    def is_converted(self) -> bool:
        """Wurde tatsaechlich umgerechnet (also nicht dieselbe Waehrung)?"""
        return self.converted is not None and self.rate is not None and self.rate != 1


class CurrencyService:
    """Kurse, Umrechnung und Klartextmeldungen zur Fremdwaehrung."""

    def __init__(self, settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # Grundlagen
    # ------------------------------------------------------------------
    @property
    def _currency_settings(self):
        return self.settings.currency

    @property
    def home_currency(self) -> str:
        """Hauswaehrung (Vergleichsbasis)."""
        return (self._currency_settings.company_currency or "EUR").upper()

    @property
    def conversion_enabled(self) -> bool:
        return bool(self._currency_settings.convert_for_comparison)

    def _normalize(self, currency: str) -> str:
        return (currency or "").strip().upper()

    def _rate_to_home(self, currency: str) -> Decimal | None:
        """Wie viel Hauswaehrung ist eine Einheit von ``currency`` wert?

        Kurse stehen als Zeichenketten in den Einstellungen (JSON-tauglich).
        Ein kaputter Wert wird protokolliert und ignoriert -- er darf weder zu
        einer Ausnahme fuehren noch stillschweigend als 1 durchgehen.
        """
        code = self._normalize(currency)
        if not code:
            return None
        if code == self.home_currency:
            return Decimal(1)
        rates = self._currency_settings.exchange_rates or {}
        raw = None
        for key, value in rates.items():
            if self._normalize(key) == code:
                raw = value
                break
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        try:
            rate = Decimal(str(raw).strip().replace(",", "."))
        except (InvalidOperation, ValueError, TypeError):
            logger.warning("Umrechnungskurs fuer %s ist unbrauchbar (%r) und wird "
                           "ignoriert.", code, raw)
            return None
        if not rate.is_finite() or rate <= 0:
            logger.warning("Umrechnungskurs fuer %s ist unplausibel (%s) und wird "
                           "ignoriert.", code, rate)
            return None
        return rate

    # ------------------------------------------------------------------
    # Oeffentliche Schnittstelle
    # ------------------------------------------------------------------
    def rate(self, from_currency: str, to_currency: str = "") -> Decimal | None:
        """Kurs von ``from_currency`` nach ``to_currency``.

        Leere Zielwaehrung bedeutet Hauswaehrung.  Gleiche Waehrung ergibt 1.
        Ist eine der beiden Waehrungen unbekannt, lautet die Antwort ``None``
        -- es wird nicht geraten und schon gar nicht 1 angenommen.
        """
        source = self._normalize(from_currency)
        target = self._normalize(to_currency) or self.home_currency
        if not source:
            return None
        if source == target:
            return Decimal(1)

        source_rate = self._rate_to_home(source)
        target_rate = self._rate_to_home(target)
        if source_rate is None or target_rate is None:
            return None
        try:
            return source_rate / target_rate
        except (DivisionByZero, InvalidOperation):
            logger.warning("Kurs %s -> %s konnte nicht berechnet werden.", source, target)
            return None

    def convert(self, amount: Decimal | None, from_currency: str,
                to_currency: str = "") -> Decimal | None:
        """Betrag umrechnen -- ausschliesslich fuer den Vergleich.

        ``None`` bei fehlendem Betrag oder fehlendem Kurs.  Das Ergebnis geht
        nie nach SAP zurueck.
        """
        if amount is None:
            return None
        factor = self.rate(from_currency, to_currency)
        if factor is None:
            return None
        if factor == 1:
            return Decimal(amount)
        try:
            return (Decimal(amount) * factor).quantize(_CONVERT_DECIMALS,
                                                       rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            logger.warning("Betrag %s konnte nicht umgerechnet werden.", amount)
            return None

    def conversion(self, amount: Decimal | None, from_currency: str,
                   to_currency: str = "") -> ConversionResult:
        """Umrechnung samt Herkunftsangabe (siehe ``ConversionResult``)."""
        source = self._normalize(from_currency)
        target = self._normalize(to_currency) or self.home_currency
        result = ConversionResult(amount=amount, currency=source,
                                  target_currency=target)

        if not self.conversion_enabled and source != target:
            result.note = "Umrechnung fuer den Vergleich ist abgeschaltet"
            return result

        factor = self.rate(source, target)
        result.rate = factor
        if factor is None:
            result.note = f"Nicht vergleichbar: kein Kurs fuer {source or '?'} hinterlegt"
            return result

        result.converted = self.convert(amount, source, target)
        if result.converted is None:
            result.note = "kein Betrag zum Umrechnen"
            return result
        if factor == 1:
            result.note = ""
            return result
        result.note = (f"= {format_decimal(result.converted)} {target} "
                       f"bei Kurs {format_decimal(factor, 4).rstrip('0').rstrip(',')}")
        return result

    # ------------------------------------------------------------------
    # Alter der Kurse
    # ------------------------------------------------------------------
    def rate_date(self) -> date | None:
        return parse_date(self._currency_settings.rate_date)

    def rate_age_days(self) -> int | None:
        """Alter der Kurspflege in Tagen; ``None``, wenn kein Datum gepflegt ist."""
        pflegedatum = self.rate_date()
        if pflegedatum is None:
            return None
        return (date.today() - pflegedatum).days

    def is_stale(self) -> bool:
        """Sind die Kurse aelter als zugelassen?

        Ohne gepflegtes Datum lautet die Antwort ``False`` -- das Fehlen des
        Datums wird ueber ``problems()`` gemeldet, nicht hier stillschweigend
        zu "veraltet" umgedeutet.
        """
        age = self.rate_age_days()
        if age is None:
            return False
        maximum = int(self._currency_settings.max_rate_age_days or 0)
        if maximum <= 0:
            return False
        return age > maximum

    # ------------------------------------------------------------------
    # Klartext fuer die Oberflaeche
    # ------------------------------------------------------------------
    def foreign(self, currencies) -> list[str]:
        """Aus einer Menge Waehrungen die Fremdwaehrungen -- sortiert."""
        home = self.home_currency
        return sorted({self._normalize(c) for c in currencies if self._normalize(c)} - {home})

    def problems(self, currencies: set[str]) -> list[str]:
        """Verstaendliche Meldungen zu den uebergebenen Waehrungen.

        Gemeldet werden fehlende Kurse, veraltete Kurse und eine abgeschaltete
        Umrechnung.  Sind keine Fremdwaehrungen im Spiel, ist die Liste leer.
        """
        fremde = self.foreign(currencies)
        if not fremde:
            return []

        meldungen: list[str] = []
        if not self.conversion_enabled:
            meldungen.append(
                "Die Umrechnung für den Vergleich ist abgeschaltet: "
                f"{', '.join(fremde)} wird nur angezeigt, eine Prozentangabe "
                "zur Preisänderung entfällt.")
            return meldungen

        ohne_kurs = [code for code in fremde if self.rate(code) is None]
        if ohne_kurs:
            meldungen.append(
                f"Für {', '.join(ohne_kurs)} ist kein Umrechnungskurs hinterlegt. "
                "Die Preisänderung kann für diese Positionen nicht berechnet werden "
                "(Einstellungen: Währung).")

        if self.is_stale():
            alter = self.rate_age_days()
            meldungen.append(
                f"Die Umrechnungskurse sind {alter} Tage alt (zulässig sind "
                f"{self._currency_settings.max_rate_age_days} Tage). "
                "Bitte vor dem Vergleich aktualisieren.")
        elif self.rate_date() is None:
            meldungen.append(
                "Zu den Umrechnungskursen ist kein Pflegedatum hinterlegt – "
                "ihr Alter lässt sich nicht beurteilen.")

        return meldungen

    def rate_info_text(self, currencies) -> str:
        """Kurzform der gueltigen Kurse, z. B. ``USD 0,92 / CHF 1,06``."""
        teile = []
        for code in self.foreign(currencies):
            factor = self.rate(code)
            if factor is None:
                teile.append(f"{code} kein Kurs")
            else:
                teile.append(f"{code} {format_decimal(factor, 4).rstrip('0').rstrip(',')}")
        return " / ".join(teile)
