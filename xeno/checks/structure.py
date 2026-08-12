"""Richtung des Kursverlaufs.

Die anderen Pruefungen beantworten "ist das eine Falle". Diese beantwortet
"laeuft es ueberhaupt". Beides gehoert getrennt: ein sauberer Token ohne
Bewegung ist keine Falle, aber auch kein Handel.

Die Faustregeln stammen aus der Marktstrukturlehre und sind bewusst grob
gehalten - mehr gibt ein Chart auch nicht her:

    Aufwaertstrend  -> guenstige Ausgangslage
    seitwaerts      -> keine Aussage, meist kein Handel
    Abwaertstrend   -> ungeeignet
    Bruch nach oben -> der Moment, an dem es interessant wird

**Wichtig fuer die Einordnung:** Ein Abwaertstrend ist kein Betrugsvorwurf.
Der Befund senkt die Bewertung, weil ein fallender Kurs ein schlechter
Einstieg ist - nicht, weil an dem Token etwas faul waere. Umgekehrt macht ein
Aufwaertstrend einen gebuendelten Token nicht sicher; genau das ist ja das
Muster, mit dem gebuendelte Token nach oben laufen.

Der Abstand zum Hoechststand beantwortet die Frage, die auf zusammengefassten
Marktdaten unsichtbar bleibt: ob ein Token seinen Lauf schon hinter sich hat.
Ein Token, der 80 Prozent unter seinem Hoch steht, mag heute ruhig aussehen -
er hat den Schwung aber bereits verbraucht.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from ..structure import Break, Trend
from .base import TokenData, finding

CHECK = "structure"

#: Ab diesem Abstand zum Hoechststand gilt der Lauf als vorbei. Der Wert ist
#: bewusst hoch: Memecoins schwanken im Normalbetrieb um 30 bis 50 Prozent,
#: ein Rueckgang darunter sagt noch nichts.
FADED_DRAWDOWN_PCT = 70.0
DEEP_DRAWDOWN_PCT = 85.0


def check_structure(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    result = data.structure
    if result is None:
        # Keine Kerzen abgerufen - kein Befund, aber auch keine Entwarnung.
        return []

    if not result.readable:
        return [
            finding(
                CHECK,
                "too_little_history",
                Severity.INFO,
                f"Nur {result.candle_count} Kerzen - fuer eine Aussage zur "
                "Richtung zu wenig Verlauf",
                candles=result.candle_count,
            )
        ]

    findings: list[Finding] = []
    change = result.change_pct
    move = f" ({change:+.0f}% im betrachteten Zeitraum)" if change is not None else ""

    if result.trend is Trend.DOWN:
        findings.append(
            finding(
                CHECK,
                "downtrend",
                Severity.MEDIUM,
                f"Abwaertstrend: tiefere Hochs und tiefere Tiefs{move}",
                **result.to_dict(),
            )
        )
    elif result.trend is Trend.UP:
        findings.append(
            finding(
                CHECK,
                "uptrend",
                Severity.INFO,
                f"Aufwaertstrend: hoehere Hochs und hoehere Tiefs{move}",
                **result.to_dict(),
            )
        )
    elif result.trend is Trend.SIDEWAYS:
        findings.append(
            finding(
                CHECK,
                "sideways",
                Severity.LOW,
                f"Seitwaerts - keine Richtung erkennbar{move}",
                **result.to_dict(),
            )
        )
    else:
        findings.append(
            finding(
                CHECK,
                "no_clear_structure",
                Severity.INFO,
                "Zu wenige Wendepunkte fuer eine Trendaussage",
                **result.to_dict(),
            )
        )

    if result.structure_break is Break.BEARISH:
        findings.append(
            finding(
                CHECK,
                "structure_break_down",
                Severity.MEDIUM,
                "Struktur nach unten gebrochen: ein Tief, das halten sollte, "
                "hat nicht gehalten",
            )
        )
    elif result.structure_break is Break.BULLISH:
        findings.append(
            finding(
                CHECK,
                "structure_break_up",
                Severity.INFO,
                "Struktur nach oben gebrochen: erstes hoeheres Hoch nach einer "
                "Phase ohne Richtung",
            )
        )

    drawdown = result.drawdown_pct
    if drawdown is not None and drawdown >= FADED_DRAWDOWN_PCT:
        findings.append(
            finding(
                CHECK,
                "past_its_peak",
                Severity.MEDIUM if drawdown >= DEEP_DRAWDOWN_PCT else Severity.LOW,
                f"{drawdown:.0f}% unter dem Hoechststand - der Lauf hat bereits "
                "stattgefunden",
                drawdown_pct=round(drawdown, 1),
            )
        )

    return findings
