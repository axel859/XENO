"""Stage 1: guenstiger Vorfilter.

Arbeitet ausschliesslich auf Marktdaten aus der Discovery - kein einziger
RPC-Call. Das ist der Punkt: der Deep-Check ist teuer, also muss der
Grossteil der Kandidaten vorher rausfliegen.

Was hier geprueft wird, sind Handelbarkeit und Aktivitaet, nicht Sicherheit.
Ein Token, der das Screening besteht, ist damit ausdruecklich noch nicht
"gut" - er ist nur einen genaueren Blick wert.
"""

from __future__ import annotations

from .checks.botting import EXTREME_TRADE_RATIO
from .config import ScreenThresholds
from .models import ScreenResult, TokenCandidate


def screen(candidate: TokenCandidate, thresholds: ScreenThresholds) -> ScreenResult:
    """Prueft einen Kandidaten. Sammelt alle Gruende, nicht nur den ersten -
    beim Justieren der Schwellwerte will man sehen, woran es tatsaechlich lag."""
    reasons: list[str] = []

    age = candidate.age_minutes
    if age is None:
        reasons.append("kein Erstellungszeitpunkt bekannt")
    elif age < thresholds.min_age_minutes:
        reasons.append(f"zu frisch ({age:.1f}min < {thresholds.min_age_minutes:.0f}min)")
    elif age > thresholds.max_age_hours * 60:
        reasons.append(f"zu alt ({age / 60:.1f}h > {thresholds.max_age_hours:.0f}h)")

    liquidity = candidate.liquidity_usd
    if liquidity is None:
        reasons.append("keine Liquiditaetsdaten")
    elif liquidity < thresholds.min_liquidity_usd:
        reasons.append(f"Liquiditaet zu niedrig (${liquidity:,.0f})")
    elif liquidity > thresholds.max_liquidity_usd:
        reasons.append(f"Liquiditaet ueber Zielbereich (${liquidity:,.0f})")

    volume = candidate.volume_h1_usd
    if volume is None:
        reasons.append("keine Volumendaten")
    elif volume < thresholds.min_volume_h1_usd:
        reasons.append(f"Volumen 1h zu niedrig (${volume:,.0f})")

    buyers = candidate.buyers_h1
    if buyers is not None and buyers < thresholds.min_unique_buyers_h1:
        reasons.append(f"zu wenige eindeutige Kaeufer ({buyers})")

    ratio = candidate.buy_sell_ratio
    if ratio is not None and ratio < thresholds.min_buy_sell_ratio:
        reasons.append(f"Verkaufsdruck (Buy/Sell {ratio:.2f})")

    # Sehr hohes Volumen bei duenner Liquiditaet ist typisch fuer Wash-Trading:
    # dieselben Coins werden im Kreis gehandelt, um Volumen vorzutaeuschen.
    vol_liq = candidate.volume_to_liquidity
    if vol_liq is not None and vol_liq > thresholds.max_volume_to_liquidity:
        reasons.append(f"Volumen/Liquiditaet auffaellig hoch ({vol_liq:.0f}x)")

    # Nur die eindeutigsten Faelle maschinellen Handels - hier geht es darum,
    # das teure Pruefbudget nicht fuer offensichtliches Wash-Trading
    # auszugeben. Der abgestufte Blick folgt im Deep-Check; etwas
    # maschineller Handel steckt in fast jedem Chart und soll hier passieren.
    ratio = candidate.trades_per_wallet
    if ratio is not None and ratio >= EXTREME_TRADE_RATIO:
        reasons.append(
            f"Handel fast nur maschinell ({ratio:.0f} Trades je Wallet)"
        )

    return ScreenResult(candidate=candidate, passed=not reasons, reasons=reasons)


def screen_all(
    candidates: list[TokenCandidate], thresholds: ScreenThresholds
) -> list[ScreenResult]:
    return [screen(candidate, thresholds) for candidate in candidates]
