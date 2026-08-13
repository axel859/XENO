"""Verbrauchszaehler mit hartem Deckel.

Der Anlass war ein Totalausfall: XENO gab pro geprueftem Token rund 600
Credits aus, ohne dass das irgendwo sichtbar war. Bei acht Token je Minute
war ein Monatskontingent nach drei Stunden leer - bemerkt wurde es am
naechsten Morgen auf der Webseite des Anbieters.

Der Fehler war nicht die Hoehe des Verbrauchs. Der Fehler war, dass ein Bot
ueberhaupt in der Lage war, ein Monatsbudget in einer Nacht auszugeben, ohne
dass ihn etwas daran gehindert oder auch nur davon erzaehlt haette.

**Anfragen sind nicht gleich Anfragen.** Genau das hatte ich uebersehen:

    Helius     normaler RPC-Aufruf        1 Credit
               geparste Transaktionen   100 Credits
    QuickNode  alle Methoden             30 Credits
               grosse Abfragen          120 Credits

Ein Aufruf der geparsten Transaktionen kostet also so viel wie hundert
gewoehnliche. Wer Anfragen zaehlt statt Credits, rechnet um den Faktor
hundert falsch.

**Wie der Deckel arbeitet.** Das Monatsbudget wird auf die verbleibenden
Tage verteilt. Wer heute wenig verbraucht, hat morgen mehr - wer viel
verbraucht, weniger. Das ist selbstkorrigierend und braucht keine
Feineinstellung.

Wird eine Abfrage verweigert, ist das ausdruecklich **keine Entwarnung**:
die betroffene Pruefung meldet eine Wissensluecke, und ein Token mit
Wissensluecken kommt nie ueber CAUTION hinaus.
"""

from __future__ import annotations

import calendar
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

METER_FILE_NAME = "credits.json"

#: Aufrufarten, nach denen abgerechnet wird.
RPC = "rpc"
RPC_LARGE = "rpc_large"
ENHANCED = "enhanced"

#: Preise je Anbieter. Quelle sind die Preislisten der Anbieter; der
#: oeffentliche RPC kostet nichts, ist dafuer stark gedrosselt und
#: beantwortet getTokenLargestAccounts gar nicht.
PRICES: dict[str, dict[str, int]] = {
    "helius": {RPC: 1, RPC_LARGE: 1, ENHANCED: 100},
    "quicknode": {RPC: 30, RPC_LARGE: 120, ENHANCED: 100},
    "public": {RPC: 0, RPC_LARGE: 0, ENHANCED: 0},
    # Unbekannter Anbieter: vorsichtig rechnen. Lieber zu frueh bremsen als
    # noch einmal in eine Sperre laufen.
    "unknown": {RPC: 30, RPC_LARGE: 120, ENHANCED: 100},
}

#: Monatskontingente der kostenlosen Tarife.
FREE_TIER: dict[str, int] = {
    "helius": 1_000_000,
    "quicknode": 10_000_000,
    "public": 0,          # kein Kontingent, also auch kein Deckel
    "unknown": 1_000_000,
}

#: Anteil des Kontingents, den XENO hoechstens ausgibt. Der Rest ist
#: Reserve - fuer Handabfragen und dafuer, dass die Preisliste eines
#: Anbieters nicht in jedem Detail bekannt ist.
SAFETY_MARGIN = 0.9


def provider_of(rpc_url: str) -> str:
    """Erkennt den Anbieter an der Adresse."""
    url = (rpc_url or "").lower()
    if "helius" in url:
        return "helius"
    if "quiknode" in url or "quicknode" in url:
        return "quicknode"
    if not url or "api.mainnet-beta.solana.com" in url:
        return "public"
    return "unknown"


def _month_key(when: float | None = None) -> str:
    moment = datetime.fromtimestamp(when or time.time(), tz=timezone.utc)
    return moment.strftime("%Y-%m")


def _day_key(when: float | None = None) -> str:
    moment = datetime.fromtimestamp(when or time.time(), tz=timezone.utc)
    return moment.strftime("%Y-%m-%d")


def days_left_in_month(when: float | None = None) -> int:
    """Verbleibende Tage einschliesslich heute - mindestens einer."""
    moment = datetime.fromtimestamp(when or time.time(), tz=timezone.utc)
    _, last = calendar.monthrange(moment.year, moment.month)
    return max(1, last - moment.day + 1)


class CreditMeter:
    """Zaehlt den Verbrauch und verweigert, was das Budget sprengt."""

    def __init__(
        self,
        rpc_url: str = "",
        path: str | Path | None = None,
        monthly_cap: int | None = None,
    ) -> None:
        self.provider = provider_of(rpc_url)
        self.prices = PRICES.get(self.provider, PRICES["unknown"])

        if monthly_cap is None:
            override = os.environ.get("XENO_CREDIT_CAP", "").strip()
            if override:
                try:
                    monthly_cap = int(float(override))
                except ValueError:
                    monthly_cap = None
        if monthly_cap is None:
            allowance = FREE_TIER.get(self.provider, 0)
            monthly_cap = int(allowance * SAFETY_MARGIN)
        self.monthly_cap = monthly_cap

        if path is None:
            from .paths import data_dir

            path = data_dir() / METER_FILE_NAME
        self.path = Path(path)

        self.month = _month_key()
        self.day = _day_key()
        self.month_spent = 0
        self.day_spent = 0
        #: Verweigerte Aufrufe - macht sichtbar, dass gebremst wird.
        self.denied = 0
        self._dirty = False
        self._saved_at = 0.0
        self._lock = threading.RLock()
        self.load()

    # -- Grenzen ----------------------------------------------------------

    @property
    def capped(self) -> bool:
        """Ob ueberhaupt ein Deckel gilt. Der oeffentliche RPC kostet nichts."""
        return self.monthly_cap > 0

    @property
    def daily_allowance(self) -> int:
        """Was heute insgesamt ausgegeben werden darf.

        Das Restbudget wird gleichmaessig auf die verbleibenden Tage verteilt.
        Ein sparsamer Tag vergroessert damit den naechsten von selbst.

        Gerechnet wird mit dem Verbrauch der **Vortage**. Zaehlte der heutige
        mit, wuechse das Tagesbudget mit jeder Ausgabe ein Stueck weit mit -
        der Deckel wuerde nie greifen.
        """
        if not self.capped:
            return 0
        earlier = max(0, self.month_spent - self.day_spent)
        remaining = max(0, self.monthly_cap - earlier)
        return remaining // days_left_in_month()

    @property
    def remaining_today(self) -> int:
        if not self.capped:
            return 0
        return max(0, self.daily_allowance - self.day_spent)

    @property
    def remaining_month(self) -> int:
        if not self.capped:
            return 0
        return max(0, self.monthly_cap - self.month_spent)

    def cost(self, kind: str, count: int = 1) -> int:
        return self.prices.get(kind, self.prices[RPC]) * count

    def can_afford(self, kind: str, count: int = 1) -> bool:
        if not self.capped:
            return True
        with self._lock:
            self._roll_over()
            return self.cost(kind, count) <= self.remaining_today

    def affordable(self, kind: str, wanted: int) -> int:
        """Wie viele Aufrufe dieser Art heute noch drin sind, hoechstens ``wanted``.

        Gedacht fuer Schleifen, die sich selbst begrenzen sollen: statt sie
        mitten im Durchlauf abzubrechen, bekommen sie von vornherein die
        Zahl, die sie sich leisten koennen.
        """
        if not self.capped:
            return wanted
        price = self.cost(kind)
        if price <= 0:
            return wanted
        with self._lock:
            self._roll_over()
            return max(0, min(wanted, self.remaining_today // price))

    def spend(self, kind: str, count: int = 1) -> int:
        """Bucht den Verbrauch und gibt die gebuchten Credits zurueck."""
        amount = self.cost(kind, count)
        if not amount:
            return 0
        with self._lock:
            self._roll_over()
            self.month_spent += amount
            self.day_spent += amount
            self._dirty = True
        return amount

    def deny(self) -> None:
        with self._lock:
            self.denied += 1
            self._dirty = True

    def _roll_over(self) -> None:
        """Setzt die Zaehler zurueck, wenn ein neuer Tag oder Monat begonnen hat."""
        day, month = _day_key(), _month_key()
        if month != self.month:
            self.month, self.month_spent = month, 0
        if day != self.day:
            self.day, self.day_spent, self.denied = day, 0, 0
        self._dirty = True

    # -- Persistenz -------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        with self._lock:
            # Zaehler eines vergangenen Zeitraums sind wertlos - sie wuerden
            # das heutige Budget faelschlich als verbraucht ausweisen.
            if payload.get("month") == self.month:
                self.month_spent = int(payload.get("month_spent") or 0)
            if payload.get("day") == self.day:
                self.day_spent = int(payload.get("day_spent") or 0)
                self.denied = int(payload.get("denied") or 0)

    def save(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not self._dirty:
                return
            if not force and now - self._saved_at < 30.0:
                return
            payload = {
                "version": 1,
                "provider": self.provider,
                "monthly_cap": self.monthly_cap,
                "month": self.month,
                "month_spent": self.month_spent,
                "day": self.day,
                "day_spent": self.day_spent,
                "denied": self.denied,
                "saved_at": now,
            }
            self._dirty = False
            self._saved_at = now

        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".credits-", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            os.replace(tmp, self.path)
        except OSError:
            # Ein Schreibfehler darf den Watcher nicht abbrechen. Der Zaehler
            # laeuft dann nur im Arbeitsspeicher weiter.
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    # -- Anzeige ----------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_over()
            return {
                "provider": self.provider,
                "monthly_cap": self.monthly_cap,
                "month_spent": self.month_spent,
                "day_spent": self.day_spent,
                "daily_allowance": self.daily_allowance,
                "remaining_today": self.remaining_today,
                "remaining_month": self.remaining_month,
                "denied_today": self.denied,
                "days_left": days_left_in_month(),
            }

    def summary(self) -> str:
        if not self.capped:
            return f"{self.provider}: kein Kontingent noetig"
        return (
            f"{self.provider}: heute {self.day_spent:,} von {self.daily_allowance:,} "
            f"Credits, Monat {self.month_spent:,}/{self.monthly_cap:,}"
        ).replace(",", ".")
