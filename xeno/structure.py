"""Marktstruktur aus Kerzendaten.

Bisher hat XENO nur gemessen, ob ein Token eine Falle ist. Nicht, ob er
laeuft. Das sind zwei verschiedene Fragen, und die zweite blieb offen - ein
sauberer Token ohne Bewegung bekam dasselbe gute Urteil wie einer, der
gerade ausbricht.

Diese Schicht liest die Richtung. Die Regel dahinter ist alt und einfach:

    hoehere Hochs **und** hoehere Tiefs   -> Aufwaertstrend
    tiefere Hochs **und** tiefere Tiefs   -> Abwaertstrend
    alles andere                          -> seitwaerts

Und wenn ein Tief bricht, das halten sollte, hat sich etwas geaendert.

Gemessen wird an den **Kerzenkoerpern**, nicht an den Dochten. Bei duenner
Liquiditaet reicht ein einzelner Kauf, um den Kurs fuer einen Moment weit
nach oben zu reissen; auf dem Chart bleibt ein langer Docht stehen. Das ist
die Handlung einer einzelnen Wallet, nicht die Aussage des Marktes. Der
Koerper - Eroeffnung bis Schluss - ueberlebt dagegen nur, wenn der Kurs auch
dort geblieben ist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Wie viele Kerzen links und rechts einen Wendepunkt bestaetigen muessen.
#: Zwei ist bewusst knapp gehalten: bei einem zwei Stunden alten Token gibt
#: es nur zwei Dutzend Kerzen, ein groesseres Fenster fraesse den halben
#: Verlauf weg und faende nie einen Wendepunkt.
SWING_WINDOW = 2

#: Mindestabstand zweier Wendepunkte in Prozent, damit sie als verschieden
#: gelten. Ohne diese Schwelle wuerde ein flacher Verlauf staendig zwischen
#: "hoeheres Hoch" und "tieferes Hoch" springen, obwohl sich nichts bewegt.
MIN_SWING_MOVE_PCT = 2.0

#: Unter dieser Zahl Kerzen wird keine Aussage getroffen. Eine Struktur aus
#: drei Kerzen ist geraten, nicht gelesen.
MIN_CANDLES = 12


class Trend(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"
    UNKNOWN = "unknown"


class Break(str, Enum):
    """Bruch der Struktur - der Moment, in dem sich etwas aendert."""

    NONE = "none"
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)


@dataclass(frozen=True)
class Swing:
    """Ein bestaetigter Wendepunkt."""

    index: int
    time: int
    price: float


def _differs(a: float, b: float) -> bool:
    """Ob zwei Kurse weit genug auseinanderliegen, um verglichen zu werden."""
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) * 100.0 >= MIN_SWING_MOVE_PCT


def swing_highs(candles: list[Candle], window: int = SWING_WINDOW) -> list[Swing]:
    """Kerzen, deren Koerper hoeher steht als der ihrer Nachbarn."""
    found: list[Swing] = []
    for i in range(window, len(candles) - window):
        span = candles[i - window : i + window + 1]
        price = candles[i].body_high
        if price < max(c.body_high for c in span):
            continue
        # Bei gleichem Kurs gewinnt der erste - sonst erzeugt eine flache
        # Strecke ein Dutzend Wendepunkte auf demselben Niveau.
        if found and not _differs(price, found[-1].price) and i - found[-1].index <= window * 2:
            continue
        found.append(Swing(index=i, time=candles[i].time, price=price))
    return found


def swing_lows(candles: list[Candle], window: int = SWING_WINDOW) -> list[Swing]:
    """Kerzen, deren Koerper tiefer steht als der ihrer Nachbarn."""
    found: list[Swing] = []
    for i in range(window, len(candles) - window):
        span = candles[i - window : i + window + 1]
        price = candles[i].body_low
        if price > min(c.body_low for c in span):
            continue
        if found and not _differs(price, found[-1].price) and i - found[-1].index <= window * 2:
            continue
        found.append(Swing(index=i, time=candles[i].time, price=price))
    return found


def classify(highs: list[Swing], lows: list[Swing]) -> Trend:
    """Trend aus der Abfolge der letzten beiden Hochs und Tiefs."""
    if len(highs) < 2 or len(lows) < 2:
        return Trend.UNKNOWN

    last_high, prev_high = highs[-1].price, highs[-2].price
    last_low, prev_low = lows[-1].price, lows[-2].price

    # Ein Vergleich zaehlt nur, wenn die Kurse ueberhaupt auseinanderliegen.
    higher_highs = _differs(last_high, prev_high) and last_high > prev_high
    higher_lows = _differs(last_low, prev_low) and last_low > prev_low
    lower_highs = _differs(last_high, prev_high) and last_high < prev_high
    lower_lows = _differs(last_low, prev_low) and last_low < prev_low

    if higher_highs and higher_lows:
        return Trend.UP
    if lower_highs and lower_lows:
        return Trend.DOWN
    return Trend.SIDEWAYS


def settled(swings: list[Swing], count: int, window: int = SWING_WINDOW) -> list[Swing]:
    """Wendepunkte, die nicht mehr zur laufenden Bewegung gehoeren.

    Ohne diese Abgrenzung misst sich ein Bruch an sich selbst: faellt der
    Kurs steil, bildet der Sturz am Ende des Verlaufs selbst einen neuen
    Tiefpunkt. Verglichen mit dem waere kein Tief je gebrochen - der Kurs
    steht ja genau darauf.
    """
    cutoff = count - window * 2 - 1
    return [s for s in swings if s.index <= cutoff]


def detect_break(
    candles: list[Candle],
    highs: list[Swing],
    lows: list[Swing],
    window: int = SWING_WINDOW,
) -> Break:
    """Ob der letzte Schlusskurs eine gesetzte Marke durchbrochen hat.

    Verglichen wird gegen den letzten Wendepunkt, der bereits stand, bevor
    die aktuelle Bewegung begann. Nur so beantwortet der Vergleich die
    eigentliche Frage: hat eine Marke gehalten, an der frueher jemand
    eingegriffen hat?
    """
    if not candles:
        return Break.NONE
    close = candles[-1].close
    count = len(candles)

    prior_lows = settled(lows, count, window)
    if prior_lows and close < prior_lows[-1].price and _differs(close, prior_lows[-1].price):
        return Break.BEARISH

    prior_highs = settled(highs, count, window)
    if prior_highs and close > prior_highs[-1].price and _differs(close, prior_highs[-1].price):
        return Break.BULLISH
    return Break.NONE


@dataclass
class Structure:
    """Was der Verlauf ueber die Richtung sagt."""

    trend: Trend = Trend.UNKNOWN
    structure_break: Break = Break.NONE
    candle_count: int = 0
    highs: list[Swing] = None  # type: ignore[assignment]
    lows: list[Swing] = None  # type: ignore[assignment]
    #: Kursaenderung ueber den gesamten betrachteten Zeitraum, in Prozent.
    change_pct: float | None = None
    #: Abstand zum hoechsten Punkt des Zeitraums, in Prozent.
    drawdown_pct: float | None = None

    def __post_init__(self) -> None:
        if self.highs is None:
            self.highs = []
        if self.lows is None:
            self.lows = []

    @property
    def readable(self) -> bool:
        """Ob genug Verlauf vorlag, um ueberhaupt etwas abzulesen."""
        return self.candle_count >= MIN_CANDLES

    def to_dict(self) -> dict:
        return {
            "trend": self.trend.value,
            "structure_break": self.structure_break.value,
            "candles": self.candle_count,
            "change_pct": self.change_pct,
            "drawdown_pct": self.drawdown_pct,
            "swing_highs": len(self.highs),
            "swing_lows": len(self.lows),
        }


def analyse(candles: list[Candle], window: int = SWING_WINDOW) -> Structure:
    """Liest Trend, Strukturbruch und Abstand zum Hoch aus dem Verlauf."""
    if not candles:
        return Structure()

    result = Structure(candle_count=len(candles))

    first, last = candles[0].close, candles[-1].close
    if first > 0:
        result.change_pct = (last - first) / first * 100.0

    # Der Hoechststand wird am Koerper gemessen, aus demselben Grund wie die
    # Wendepunkte: ein Docht ist kein Kurs, an dem gehandelt wurde.
    peak = max(c.body_high for c in candles)
    if peak > 0:
        result.drawdown_pct = (peak - last) / peak * 100.0

    if not result.readable:
        return result

    result.highs = swing_highs(candles, window)
    result.lows = swing_lows(candles, window)
    result.trend = classify(result.highs, result.lows)
    result.structure_break = detect_break(candles, result.highs, result.lows)
    return result
