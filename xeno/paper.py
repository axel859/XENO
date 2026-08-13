"""Papierhandel - der Auto-Trader ohne Geld.

Dies ist bewusst kein Simulator neben dem eigentlichen Handel, sondern
dieselbe Entscheidungskette: Signal rein, Einstieg, Ausstiegsregel,
Position schliessen, Bilanz. Wer spaeter echtes Geld einsetzen will,
schaltet an genau dieser Stelle um - es entsteht keine zweite Software,
deren Verhalten von der getesteten abweicht.

**Warum das vor dem echten Handel kommt.** XENO hat bisher nie belegt, dass
seine Urteile in die richtige Richtung zeigen; die einzige Beobachtung war,
dass die abgelehnten Token besser liefen als die empfohlenen. Wer das
automatisiert, verliert nicht langsamer, sondern schneller und rund um die
Uhr. Eine Woche Papierhandel kostet nichts und beantwortet die Frage.

**Die Ausstiegsregel ist der eigentliche Inhalt.** Ein Einstieg ohne
geplanten Ausstieg ist kein Handel, sondern eine Hoffnung. Sie steht
deshalb fest, bevor eine Position eroeffnet wird, und sie ist stumpf:
Ziel, Verlustgrenze, Zeitlimit. Ein Programm hat gegenueber einem Menschen
an dieser Stelle genau einen Vorteil - es wird nicht gierig.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median

BOOK_FILE_NAME = "paper-trades.json"

#: Einsatz je Position. Nur eine Rechengroesse - es fliesst kein Geld.
DEFAULT_SIZE_USD = 100.0

#: Ausstiegsregel. Bewusst grob: die Frage ist erst einmal, ob die Signale
#: ueberhaupt taugen, nicht ob 2.1x besser waere als 2.0x.
TAKE_PROFIT = 2.0      # verdoppelt -> raus
STOP_LOSS = 0.6        # 40 Prozent im Minus -> raus
MAX_HOLD_SECONDS = 24 * 3600


@dataclass
class Position:
    """Eine offene oder abgeschlossene Papierposition."""

    mint: str
    symbol: str = ""
    opened_at: float = 0.0
    entry_price: float = 0.0
    size_usd: float = DEFAULT_SIZE_USD
    strength: int = 0
    reasons: list[str] = field(default_factory=list)

    closed_at: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    #: Hoechster Kurs seit Einstieg - zeigt, was ein besserer Ausstieg
    #: gebracht haette.
    peak_price: float = 0.0

    @property
    def open(self) -> bool:
        return not self.closed_at

    @property
    def multiple(self) -> float | None:
        """Vielfaches des Einstiegskurses.

        Bei einer geschlossenen Position ist ein Kurs von null **kein**
        fehlender Wert, sondern das Ergebnis: der Markt ist verschwunden.
        Ihn als "keine Angabe" zu behandeln wuerde Totalverluste aus jeder
        Auswertung herausfallen lassen - und die Bilanz schoenrechnen.
        """
        if not self.entry_price:
            return None
        if self.closed_at:
            return self.exit_price / self.entry_price
        return self.peak_price / self.entry_price if self.peak_price else None

    def result_usd(self, price: float | None = None) -> float | None:
        """Gewinn oder Verlust in Dollar."""
        if not self.entry_price:
            return None
        if self.closed_at:
            current = self.exit_price
        elif price:
            current = price
        else:
            # Offene Position ohne aktuellen Kurs: hier ist tatsaechlich
            # nichts bekannt.
            return None
        return self.size_usd * (current / self.entry_price - 1.0)

    def decide(self, price: float, now: float) -> str:
        """Ob und warum diese Position jetzt geschlossen wird."""
        if not self.entry_price or price <= 0:
            return ""
        ratio = price / self.entry_price
        if ratio >= TAKE_PROFIT:
            return "ziel"
        if ratio <= STOP_LOSS:
            return "verlustgrenze"
        if now - self.opened_at >= MAX_HOLD_SECONDS:
            return "zeitlimit"
        return ""


class PaperBook:
    """Alle Papierpositionen, dauerhaft gespeichert."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .paths import data_dir

            path = data_dir() / BOOK_FILE_NAME
        self.path = Path(path)
        self.positions: list[Position] = []
        self._lock = threading.RLock()
        self._dirty = False
        self.load()

    # -- Persistenz -------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        known = set(Position.__dataclass_fields__)
        with self._lock:
            for raw in payload.get("positions") or []:
                if isinstance(raw, dict):
                    self.positions.append(
                        Position(**{k: v for k, v in raw.items() if k in known})
                    )

    def save(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "positions": [asdict(p) for p in self.positions],
            }
            self._dirty = False

        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".paper-", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    # -- Handel -----------------------------------------------------------

    @property
    def open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self.positions if p.open]

    @property
    def closed_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self.positions if not p.open]

    def holds(self, mint: str) -> bool:
        return any(p.mint == mint and p.open for p in self.positions)

    def enter(self, call, now: float | None = None) -> Position | None:
        """Eroeffnet eine Position auf einen Call.

        Kein Nachkaufen: ein zweiter Call auf denselben Token waehrend die
        Position laeuft wuerde die Auswertung verfaelschen, weil derselbe
        Kursverlauf doppelt zaehlte.
        """
        if not call.price_usd or self.holds(call.mint):
            return None
        now = now or time.time()
        position = Position(
            mint=call.mint,
            symbol=call.symbol,
            opened_at=now,
            entry_price=call.price_usd,
            peak_price=call.price_usd,
            strength=call.strength,
            reasons=list(call.reasons),
        )
        with self._lock:
            self.positions.append(position)
            self._dirty = True
        return position

    def update(self, prices: dict[str, float], now: float | None = None) -> list[Position]:
        """Traegt neue Kurse ein und schliesst, was faellig ist.

        Ein Token, fuer den es keinen Kurs mehr gibt, ist nicht "unbekannt" -
        sein Markt ist verschwunden. Das ist das Ergebnis "wertlos", und es
        wird auch so verbucht.
        """
        now = now or time.time()
        closed: list[Position] = []
        with self._lock:
            for position in self.positions:
                if not position.open:
                    continue
                price = prices.get(position.mint)
                if price is None:
                    if now - position.opened_at >= MAX_HOLD_SECONDS:
                        position.closed_at = now
                        position.exit_price = 0.0
                        position.exit_reason = "markt_weg"
                        closed.append(position)
                        self._dirty = True
                    continue

                if price > position.peak_price:
                    position.peak_price = price
                reason = position.decide(price, now)
                if reason:
                    position.closed_at = now
                    position.exit_price = price
                    position.exit_reason = reason
                    closed.append(position)
                self._dirty = True
        return closed

    # -- Auswertung -------------------------------------------------------

    def summary(self, prices: dict[str, float] | None = None) -> dict:
        """Die Zahl, um die es geht: was waere herausgekommen."""
        prices = prices or {}
        with self._lock:
            closed = [p for p in self.positions if not p.open]
            still_open = [p for p in self.positions if p.open]

        realised = [p.result_usd() for p in closed]
        realised = [r for r in realised if r is not None]

        wins = [r for r in realised if r > 0]
        multiples = [m for m in (p.multiple for p in closed) if m is not None]

        unrealised = 0.0
        for position in still_open:
            value = position.result_usd(prices.get(position.mint))
            if value is not None:
                unrealised += value

        return {
            "open": len(still_open),
            "closed": len(closed),
            "result_usd": round(sum(realised), 2),
            "unrealised_usd": round(unrealised, 2),
            "wins": len(wins),
            "win_rate": round(100.0 * len(wins) / len(realised), 1) if realised else 0.0,
            "median_multiple": round(median(multiples), 2) if multiples else None,
            "best_multiple": round(max(multiples), 2) if multiples else None,
            "invested_usd": round(sum(p.size_usd for p in closed), 2),
            "reasons": _reason_counts(closed),
        }

    def hit_rate_text(self) -> str:
        """Die Bilanz in einem Satz - gehoert an jeden Call.

        Ohne diese Zahl ist ein Call eine Vermutung mit selbstbewusstem
        Etikett.
        """
        closed = self.closed_positions
        if not closed:
            return "noch keine abgeschlossenen Calls"
        wins = sum(1 for p in closed if (p.result_usd() or 0) > 0)
        return f"von {len(closed)} abgeschlossenen Calls waren {wins} im Plus"


def _reason_counts(positions: list[Position]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for position in positions:
        if position.exit_reason:
            counts[position.exit_reason] = counts.get(position.exit_reason, 0) + 1
    return counts
