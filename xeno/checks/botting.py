"""Maschineller Handel: sieht der Chart echt aus oder gemacht?

Volumen und ein gruener Chart erzeugen Aufmerksamkeit. Genau deshalb werden
sie hergestellt: ein Programm schiebt Token zwischen eigenen Wallets hin und
her, der Token taucht in Listen auf, sieht belebt aus - und die echten Kaeufer
kommen von selbst. Deren Geld ist der eigentliche Zweck.

Von aussen wirkt so ein Token oft *besser* als ein ehrlicher, weil er mehr
Bewegung zeigt. Ohne diese Pruefung waere er also nicht nur unerkannt, sondern
sogar bevorzugt.

**Abstufung statt schwarz-weiss.** In fast jedem Chart steckt etwas
maschineller Handel - Arbitrage, Sniper, Handels-Bots. Gemessen an 138 Pools
mit nennenswertem Handel liegt der Median bei 2.7 Trades je Wallet, das obere
Viertel beginnt bei 6.5. Erst die Ausreisser darueber sind ein Signal, und nur
die ganz extremen fuehren zum Ausschluss.

Alle Signale hier stammen aus Daten, die ohnehin schon geholt wurden - die
Pruefung kostet keine einzige zusaetzliche Anfrage.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "botting"

#: Stufen fuer Trades je Wallet, abgeleitet aus der gemessenen Verteilung.
#: (Grenze, Schwere, Klartext) - die erste passende Zeile gewinnt.
TRADE_RATIO_LEVELS: list[tuple[float, Severity, str]] = [
    (25.0, Severity.HIGH, "nahezu ausschliesslich maschineller Handel"),
    (12.0, Severity.MEDIUM, "ueberwiegend maschineller Handel"),
    (6.0, Severity.LOW, "erhoehter Anteil maschinellen Handels"),
]

#: Ab hier gilt der Handel als so eindeutig gemacht, dass sich ein
#: Deep-Check nicht lohnt. Bewusst hoch angesetzt: das betrifft die obersten
#: paar Prozent, nicht das obere Viertel.
EXTREME_TRADE_RATIO = 25.0

#: Eine einzelne Wallet, die so oft handelt, spielt mit sich selbst.
SOLO_WALLET_TRADES = 15

#: Wash-Trading: viel Umsatz gemessen an der Liquiditaet, aber der Kurs
#: bewegt sich kaum. Dann dreht sich dasselbe Geld im Kreis.
WASH_VOLUME_RATIO = 20.0
WASH_MAX_PRICE_MOVE = 5.0


def check_botting(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    candidate = data.candidate
    if candidate is None:
        return []

    findings: list[Finding] = []

    trades = candidate.trade_count_h1
    wallets = candidate.wallet_count_h1
    ratio = candidate.trades_per_wallet

    if ratio is None:
        # Zu wenig Handel, um ein Muster zu erkennen. Das ist keine
        # Entwarnung, aber auch kein Befund.
        return [
            finding(
                CHECK,
                "activity_too_low_to_judge",
                Severity.INFO,
                "Zu wenig Handel, um echte von maschineller Aktivitaet zu trennen",
                trades=trades,
            )
        ]

    # Eine einzige Wallet mit vielen Trades ist eindeutig - dafuer braucht es
    # keine Statistik.
    if wallets is not None and wallets <= 1 and (trades or 0) >= SOLO_WALLET_TRADES:
        findings.append(
            finding(
                CHECK,
                "single_wallet_trading",
                Severity.HIGH,
                f"{trades} Trades von einer einzigen Wallet - der Chart ist gemacht",
                trades=trades,
                wallets=wallets,
            )
        )
    else:
        for limit, severity, text in TRADE_RATIO_LEVELS:
            if ratio >= limit:
                findings.append(
                    finding(
                        CHECK,
                        "machine_trading",
                        severity,
                        f"{text}: {trades} Trades von nur {wallets} Wallets "
                        f"({ratio:.0f} je Wallet)",
                        trades=trades,
                        wallets=wallets,
                        trades_per_wallet=round(ratio, 1),
                    )
                )
                break

    # Umsatz ohne Kursbewegung - dasselbe Geld im Kreis.
    vol_liq = candidate.volume_to_liquidity
    move = candidate.price_change_h1_pct
    if (
        vol_liq is not None
        and move is not None
        and vol_liq >= WASH_VOLUME_RATIO
        and abs(move) <= WASH_MAX_PRICE_MOVE
    ):
        findings.append(
            finding(
                CHECK,
                "wash_trading",
                Severity.MEDIUM,
                f"Umsatz vom {vol_liq:.0f}-fachen der Liquiditaet, Kurs bewegt sich "
                f"aber nur {move:+.1f}% - Geld dreht sich im Kreis",
                volume_to_liquidity=round(vol_liq, 1),
                price_change_pct=round(move, 2),
            )
        )

    if not findings:
        findings.append(
            finding(
                CHECK,
                "trading_looks_organic",
                Severity.INFO,
                f"Handel wirkt echt: {trades} Trades von {wallets} Wallets "
                f"({ratio:.1f} je Wallet)",
                trades_per_wallet=round(ratio, 1),
            )
        )

    return findings
