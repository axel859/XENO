"""Handelbarkeit - kommt man wieder raus?

Alle anderen Pruefungen sagen etwas ueber Absichten und Strukturen. Diese
sagt etwas ueber die Realitaet: sie fragt eine echte Verkaufs-Route an.

Ein Token kann saubere Authorities und gelockte LP haben und trotzdem
unverkaeuflich sein - etwa durch einen Transfer-Hook, eine kaputte
Pool-Konfiguration oder schlicht weil auf der Verkaufsseite nichts liegt.
Umgekehrt ist ein bestandener Round-Trip der belastbarste Einzelbeleg dafuer,
dass die Position aufloesbar ist.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "tradability"

#: Bis zu diesem Alter gilt ein Pool als "noch nicht zuverlaessig indexiert".
YOUNG_POOL_MINUTES = 30.0


def check_tradability(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    trip = data.round_trip
    if trip is None:
        return []

    if not trip.buy_ok:
        return [
            finding(
                CHECK,
                "no_buy_route",
                Severity.HIGH,
                f"Keine Kauf-Route ueber Jupiter ({trip.error or 'unbekannt'})",
            )
        ]

    if not trip.sell_ok:
        # Bei sehr jungen Pools ist eine fehlende Verkaufsroute meist
        # Indexierungsverzug, kein Honeypot. Erst wenn der Pool alt genug ist,
        # dass die Aggregatoren ihn kennen muessten, ist der Befund eindeutig.
        age = data.candidate.age_minutes if data.candidate else None
        if age is not None and age < YOUNG_POOL_MINUTES:
            return [
                finding(
                    CHECK,
                    "sell_route_missing_young",
                    Severity.MEDIUM,
                    f"Keine Verkaufs-Route abrufbar - Pool ist erst {age:.0f}min alt, "
                    f"das kann an der Indexierung liegen",
                    error=trip.error,
                    age_minutes=round(age, 1),
                )
            ]
        return [
            finding(
                CHECK,
                "no_sell_route",
                Severity.CRITICAL,
                "Kauf ist moeglich, Verkauf nicht - klassisches Honeypot-Muster",
                error=trip.error,
            )
        ]

    if trip.unreliable:
        return [
            finding(
                CHECK,
                "round_trip_unreliable",
                Severity.INFO,
                "Round-Trip nicht auswertbar - Kauf- und Verkaufsquote sind "
                "inkonsistent (typisch bei frischen Bonding-Curve-Pools)",
            )
        ]

    loss = trip.loss_pct
    if loss is None:
        return []

    if loss >= 50:
        severity = Severity.CRITICAL
        text = "Verkauf nur mit massivem Verlust moeglich"
    elif loss >= 20:
        severity = Severity.HIGH
        text = "Verkauf nur mit hohem Verlust moeglich"
    elif loss >= 10:
        severity = Severity.MEDIUM
        text = "Deutliche Verluste beim Round-Trip"
    else:
        return [
            finding(
                CHECK,
                "round_trip_ok",
                Severity.INFO,
                f"Kauf und Verkauf moeglich, Round-Trip-Verlust {loss:.1f}%",
                loss_pct=round(loss, 2),
            )
        ]

    return [
        finding(
            CHECK,
            "round_trip_lossy",
            severity,
            f"{text}: {loss:.1f}% Verlust bei sofortigem Hin- und Rueckhandel",
            loss_pct=round(loss, 2),
            buy_price_impact=trip.buy_price_impact,
            sell_price_impact=trip.sell_price_impact,
        )
    ]
