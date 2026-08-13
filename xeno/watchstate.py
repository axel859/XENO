"""Zustand des Watch-Modus.

Ohne Gedaechtnis waere ein Watcher unbrauchbar: er wuerde bei jedem Durchlauf
dieselben Token erneut melden und sein Anfrage-Budget an Kandidaten
verschwenden, die er vor einer Minute schon geprueft hat.

Der Zustand loest drei Aufgaben:

* **Doppelmeldungen vermeiden** - gemeldet wird nur, was sich geaendert hat.
* **Budget lenken** - wann ein Token erneut geprueft wird, haengt von seinem
  Alter ab. Ein funf Minuten alter Token aendert sich staendig, ein drei Tage
  alter kaum noch.
* **Verschlechterungen erkennen** - dafuer muss der vorherige Befundstand
  gespeichert sein, sonst faellt nicht auf, dass die LP-Sperre verschwunden
  ist oder eine Wallet begonnen hat einzusammeln.

Gespeichert wird als JSON, damit ein Neustart nicht alles erneut meldet.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import RiskReport, Verdict

STATE_VERSION = 1

#: Nur noch der Dateiname - wo die Datei liegt, entscheidet ``paths``.
DEFAULT_STATE_FILE = "xeno-state.json"

#: Reihenfolge der Urteile von schlecht nach gut. UNKNOWN steht bewusst
#: ausserhalb - es ist keine Bewertung, sondern das Fehlen einer.
_VERDICT_RANK = {
    Verdict.AVOID: 0,
    Verdict.RISKY: 1,
    Verdict.CAUTION: 2,
    Verdict.OK: 3,
}


def verdict_rank(verdict: Verdict) -> int | None:
    return _VERDICT_RANK.get(verdict)


def is_worse(new: Verdict, old: Verdict) -> bool:
    new_rank, old_rank = verdict_rank(new), verdict_rank(old)
    if new_rank is None or old_rank is None:
        return False
    return new_rank < old_rank


def is_better(new: Verdict, old: Verdict) -> bool:
    new_rank, old_rank = verdict_rank(new), verdict_rank(old)
    if new_rank is None or old_rank is None:
        return False
    return new_rank > old_rank


@dataclass
class TokenState:
    """Was ueber einen Token aus frueheren Durchlaeufen bekannt ist."""

    mint: str
    symbol: str = ""
    first_seen: float = 0.0
    last_checked: float = 0.0
    check_count: int = 0
    verdict: str = Verdict.UNKNOWN.value
    score: int = 0
    #: Befund-Codes des letzten Durchlaufs - Basis der Aenderungserkennung.
    finding_codes: list[str] = field(default_factory=list)
    #: Codes kritischer Befunde, ueber die bereits gemeldet wurde.
    critical_codes: list[str] = field(default_factory=list)
    #: Urteil, mit dem zuletzt eine Meldung rausging.
    alerted_verdict: str = ""
    watchlisted: bool = False
    #: Pool-Erstellung als Unix-Zeit, steuert den Wiederholungsabstand.
    created_at: float | None = None
    #: Endgueltig aussortiert - wird nicht mehr geprueft (ausser auf der Watchlist).
    #:
    #: Blockiert die *Wiederholungspruefung*, nicht die Aufwach-Erkennung:
    #: dass ein Token damals einen kritischen Befund hatte, heisst nicht,
    #: dass er nie wieder gehandelt wird - und genau der Fall soll auffallen.
    dead: bool = False
    #: Wann zuletzt ein Aufwachen gemeldet wurde. Sperrfrist gegen
    #: Dauermeldungen, solange eine Bewegung anhaelt.
    woke_at: float = 0.0
    #: Marktstand zum Zeitpunkt der letzten Pruefung, klein gehalten.
    #:
    #: Die vollstaendigen Berichte liegen nur im Speicher und fallen nach
    #: 300 Eintraegen hinten heraus - erst recht bei einem Neustart. Ohne
    #: diese Kopie standen auf jeder aelteren Karte im Dashboard nur noch
    #: Striche, obwohl der Token laengst geprueft war.
    market: dict[str, Any] = field(default_factory=dict)
    #: Verteilung der groessten Halter, ebenfalls nur die Anzeigewerte.
    holders: dict[str, Any] = field(default_factory=dict)

    # -- Nachverfolgung des Kursverlaufs ---------------------------------
    #: Zeitpunkt und Kurs beim ersten Deep-Check. Alles Weitere wird daran
    #: gemessen.
    baseline_at: float = 0.0
    baseline_price_usd: float | None = None
    baseline_fdv_usd: float | None = None
    #: Urteil beim ersten Check. Bewusst getrennt vom aktuellen Urteil:
    #: die Frage lautet "wie entwickelt sich, was XENO damals so eingestuft
    #: hat" - ein spaeter geaendertes Urteil wuerde das Ergebnis verfaelschen.
    first_verdict: str = ""
    #: Vielfaches des Ausgangskurses je Zeitpunkt, z.B. {"1h": 2.4}.
    outcomes: dict[str, float] = field(default_factory=dict)
    #: Schwung beim ersten Check - fuer die spaetere Auswertung.
    first_momentum: int = 0

    #: Stichprobe aus den im Vorfilter abgelehnten Token. Sie werden nie
    #: tief geprueft, sondern nur mitgemessen.
    #:
    #: Ohne sie kann XENO nur beantworten, wie sich die Token entwickelt
    #: haben, die er *durchgelassen* hat - seine Fehlalarme bleiben
    #: unsichtbar. Ein Filter, der seine eigenen Fehler nicht kennt, kann
    #: sich nicht verbessern.
    control: bool = False
    #: Warum der Vorfilter abgelehnt hat.
    control_reason: str = ""

    @property
    def verdict_enum(self) -> Verdict:
        try:
            return Verdict(self.verdict)
        except ValueError:
            return Verdict.UNKNOWN

    def age_minutes(self, now: float | None = None) -> float | None:
        if self.created_at is None:
            return None
        return ((now or time.time()) - self.created_at) / 60.0


#: Wiederholungsabstand nach Alter des Pools: (Alter in Minuten, Abstand in Sekunden).
#: Je juenger ein Token, desto schneller aendert sich sein Zustand.
#: Anzeigewerte, die je Token dauerhaft aufgehoben werden. Bewusst eine
#: kurze Liste und nicht der ganze Kandidat: das hier steht fuer jeden je
#: gesehenen Token in der Zustandsdatei, und die soll ueber Wochen
#: handlich bleiben.
MARKET_KEEP = (
    "price_usd",
    "liquidity_usd",
    "volume_h1_usd",
    "volume_h24_usd",
    "price_change_h1_pct",
    "buyers_h1",
    "buys_h1",
    "sells_h1",
    "age_minutes",
    "mcap_usd",
    "socials",
    "websites",
)


def _market_snapshot(candidate) -> dict[str, Any]:
    """Die Anzeigewerte eines Kandidaten, klein gehalten.

    ``age_minutes`` wird bewusst als Wert der Pruefung festgehalten und
    nicht spaeter neu gerechnet: die Karte schreibt ohnehin darunter, von
    wann die Zahlen stammen. Ein Alter, das mitlaeuft, waehrend alles
    daneben stehenbleibt, waere die unehrlichere Variante.
    """
    data = candidate.to_dict()
    return {key: data[key] for key in MARKET_KEEP if data.get(key) not in (None, "")}


RECHECK_TIERS: list[tuple[float, float]] = [
    (60, 5 * 60),          # erste Stunde: alle 5 Minuten
    (6 * 60, 15 * 60),     # bis 6h: viertelstuendlich
    (24 * 60, 60 * 60),    # bis 24h: stuendlich
    (float("inf"), 4 * 3600),  # danach: alle 4 Stunden
]

#: Token auf der Watchlist werden mindestens so oft geprueft - unabhaengig
#: vom Alter, weil dort eine Verschlechterung unmittelbar relevant ist.
WATCHLIST_MAX_INTERVAL = 10 * 60


def recheck_interval(age_minutes: float | None, watchlisted: bool = False) -> float:
    """Abstand in Sekunden bis zur naechsten Pruefung."""
    if age_minutes is None:
        interval = 15 * 60.0
    else:
        interval = next(
            seconds for limit, seconds in RECHECK_TIERS if age_minutes < limit
        )
    if watchlisted:
        return min(interval, WATCHLIST_MAX_INTERVAL)
    return interval


class WatchState:
    """Persistenter Zustand aller je gesehenen Token.

    Threadsicher: im Dashboard-Betrieb schreibt der Watcher-Thread, waehrend
    die HTTP-Threads gleichzeitig lesen. Ohne Sperre koennte ein Lesevorgang
    ein halb aktualisiertes Bild erwischen oder ueber ein Dictionary
    iterieren, das sich gerade aendert.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        from .paths import state_path

        #: Uebernommener Altbestand, falls beim Start einer gefunden wurde.
        #: Die Oberflaeche meldet das einmal - stillschweigend Dateien zu
        #: kopieren waere unhoeflich.
        self.path, self.adopted_from = state_path(path)
        self.tokens: dict[str, TokenState] = {}
        self._lock = threading.RLock()
        self.load()

    # -- Persistenz -------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Kaputter Zustand darf den Watcher nicht am Start hindern -
            # schlimmstenfalls wird einmal mehr gemeldet als noetig.
            return
        known = {f for f in TokenState.__dataclass_fields__}
        with self._lock:
            for mint, raw in (payload.get("tokens") or {}).items():
                self.tokens[mint] = TokenState(
                    **{k: v for k, v in raw.items() if k in known}
                )

    def save(self) -> None:
        """Schreibt atomar, damit ein Abbruch die Datei nicht zerstoert."""
        with self._lock:
            payload = {
                "version": STATE_VERSION,
                "saved_at": time.time(),
                "tokens": {mint: asdict(state) for mint, state in self.tokens.items()},
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".xeno-state-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=1)
            os.replace(tmp_path, self.path)
        except OSError:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    # -- Abfragen ---------------------------------------------------------

    def get(self, mint: str) -> TokenState | None:
        return self.tokens.get(mint)

    def known(self, mint: str) -> bool:
        return mint in self.tokens

    @property
    def watchlist(self) -> list[TokenState]:
        with self._lock:
            return [s for s in self.tokens.values() if s.watchlisted]

    def snapshot(self) -> list[dict[str, Any]]:
        """Kopie aller Eintraege als einfache Dicts.

        Fuer die Weboberflaeche: die Kopie entsteht unter der Sperre, danach
        kann der HTTP-Thread damit arbeiten, ohne den Watcher zu blockieren.
        """
        with self._lock:
            return [asdict(state) for state in self.tokens.values()]

    def is_due(self, mint: str, now: float | None = None) -> bool:
        """Ob ein bekannter Token erneut geprueft werden soll."""
        state = self.tokens.get(mint)
        if state is None:
            return True
        # Vergleichsstichproben werden nie tief geprueft - sonst waeren sie
        # keine Vergleichsgruppe mehr, sondern Teil der geprueften.
        if state.control:
            return False
        if state.dead and not state.watchlisted:
            return False
        now = now or time.time()
        interval = recheck_interval(state.age_minutes(now), state.watchlisted)
        return (now - state.last_checked) >= interval

    def due_for_recheck(self, now: float | None = None) -> list[TokenState]:
        now = now or time.time()
        with self._lock:
            return [s for s in self.tokens.values() if self.is_due(s.mint, now)]

    # -- Aktualisieren ----------------------------------------------------

    def record(self, report: RiskReport, now: float | None = None) -> TokenState:
        """Uebernimmt ein Pruefergebnis in den Zustand."""
        now = now or time.time()
        with self._lock:
            return self._record_locked(report, now)

    def _record_locked(self, report: RiskReport, now: float) -> TokenState:
        state = self.tokens.get(report.mint)
        if state is None:
            state = TokenState(mint=report.mint, first_seen=now)
            self.tokens[report.mint] = state

        state.symbol = report.symbol or state.symbol
        state.last_checked = now
        state.check_count += 1
        state.verdict = report.verdict.value
        state.score = report.score
        state.finding_codes = [f.code for f in report.findings]
        state.critical_codes = [
            f.code for f in report.findings if f.severity.value == "critical"
        ]

        candidate = report.candidate
        if candidate and candidate.created_at:
            state.created_at = candidate.created_at.timestamp()
        if candidate is not None:
            state.market = _market_snapshot(candidate)
        if report.distribution is not None:
            state.holders = {
                "top10_pct": report.distribution.top10_pct,
                "largest_pct": report.distribution.largest_pct,
                "holder_count": report.distribution.holder_count,
            }

        # Ausgangswerte nur einmal festhalten - beim ersten Urteil. Spaetere
        # Pruefungen duerfen den Bezugspunkt nicht verschieben, sonst misst
        # man am Ende gegen einen mitgewanderten Kurs.
        if not state.baseline_at and candidate and candidate.price_usd:
            state.baseline_at = now
            state.baseline_price_usd = candidate.price_usd
            state.baseline_fdv_usd = candidate.fdv_usd
            state.first_verdict = report.verdict.value
            state.first_momentum = candidate.momentum

        # Ein Token mit kritischem Befund aendert sich praktisch nie zum
        # Guten. Weitere Pruefungen waeren verschwendetes Budget - es sei
        # denn, er steht auf der Watchlist.
        if report.verdict is Verdict.AVOID and state.critical_codes:
            state.dead = True

        return state

    def record_control(
        self, candidate, reason: str = "", now: float | None = None
    ) -> TokenState | None:
        """Nimmt einen abgelehnten Token als Vergleichsstichprobe auf.

        Ohne Kurs geht das nicht - ohne Ausgangswert waere spaeter nichts zu
        messen. Bereits bekannte Token werden nicht angefasst: ein Token, der
        schon geprueft wurde, gehoert in die andere Gruppe.
        """
        if not candidate or not candidate.price_usd:
            return None
        now = now or time.time()
        with self._lock:
            if candidate.mint in self.tokens:
                return None
            state = TokenState(
                mint=candidate.mint,
                symbol=candidate.symbol,
                first_seen=now,
                control=True,
                control_reason=reason,
                baseline_at=now,
                baseline_price_usd=candidate.price_usd,
                baseline_fdv_usd=candidate.fdv_usd,
                # Eigene Gruppe in der Auswertung. Ein echtes Urteil hat der
                # Token nie bekommen - er wurde ja gerade nicht geprueft.
                first_verdict="CONTROL",
                first_momentum=candidate.momentum,
            )
            if candidate.created_at:
                state.created_at = candidate.created_at.timestamp()
            self.tokens[candidate.mint] = state
            return state

    def mark_alerted(self, mint: str, verdict: Verdict) -> None:
        with self._lock:
            state = self.tokens.get(mint)
            if state is not None:
                state.alerted_verdict = verdict.value

    def mark_woken(self, mint: str, now: float | None = None) -> None:
        """Startet die Sperrfrist der Aufwach-Erkennung fuer diesen Token."""
        with self._lock:
            state = self.tokens.get(mint)
            if state is not None:
                state.woke_at = now or time.time()

    def add_to_watchlist(self, mint: str, symbol: str = "") -> TokenState:
        with self._lock:
            state = self.tokens.get(mint)
            if state is None:
                state = TokenState(mint=mint, symbol=symbol, first_seen=time.time())
                self.tokens[mint] = state
            state.watchlisted = True
            state.dead = False  # bewusst beobachtet, also weiter pruefen
            return state

    def remove_from_watchlist(self, mint: str) -> bool:
        with self._lock:
            state = self.tokens.get(mint)
            if state is None or not state.watchlisted:
                return False
            state.watchlisted = False
            return True

    def prune(self, max_age_days: float = 7.0, now: float | None = None) -> int:
        """Entfernt alte, nicht beobachtete Eintraege, damit die Datei nicht waechst."""
        now = now or time.time()
        cutoff = now - max_age_days * 86400
        with self._lock:
            stale = [
                mint
                for mint, state in self.tokens.items()
                if not state.watchlisted
                and state.last_checked
                and state.last_checked < cutoff
            ]
            for mint in stale:
                del self.tokens[mint]
            return len(stale)
