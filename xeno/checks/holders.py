"""Holder-Konzentration.

Die Kernfrage: wie viel der Supply liegt in wenigen privaten Haenden? Wenn
zehn Wallets die Haelfte halten, entscheidet deren Ausstieg allein ueber den
Kurs - egal wie gut der Rest aussieht.

Pools, Burn-Adressen, Locker und Boersen sind in ``chain.py`` bereits als
solche markiert und zaehlen hier nicht mit. Ohne diesen Ausschluss waere die
Pruefung wertlos, weil der Pool bei frischen Token fast die gesamte Supply
haelt.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "holders"


def check_holders(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    distribution = data.distribution
    if distribution is None or not distribution.holders:
        return [
            finding(
                CHECK,
                "holders_unavailable",
                Severity.HIGH,
                "Holder-Verteilung nicht verfuegbar - Konzentration ungeprueft",
                hint="Ein RPC mit getTokenLargestAccounts (z.B. Helius) behebt das",
            )
        ]

    findings: list[Finding] = []

    largest = distribution.largest_pct
    if largest > thresholds.max_single_holder_pct:
        severity = Severity.CRITICAL if largest >= 30 else (
            Severity.HIGH if largest >= 20 else Severity.MEDIUM
        )
        top = next((h for h in distribution.holders if not h.is_excluded), None)
        findings.append(
            finding(
                CHECK,
                "single_holder_dominant",
                severity,
                f"Groesste private Wallet haelt {largest:.1f}% der Supply",
                pct=round(largest, 2),
                owner=top.owner if top else None,
                source=data.holder_source,
            )
        )

    top10 = distribution.top10_pct
    if top10 > thresholds.max_top10_pct:
        severity = Severity.CRITICAL if top10 >= 70 else (
            Severity.HIGH if top10 >= 50 else Severity.MEDIUM
        )
        findings.append(
            finding(
                CHECK,
                "top10_concentration",
                severity,
                f"Top-10-Wallets halten {top10:.1f}% der Supply "
                f"(Grenze {thresholds.max_top10_pct:.0f}%)",
                pct=round(top10, 2),
                source=data.holder_source,
            )
        )

    count = distribution.holder_count
    if count is not None:
        if count < 10:
            findings.append(
                finding(
                    CHECK,
                    "almost_no_holders",
                    Severity.HIGH,
                    f"Nur {count} Holder insgesamt - praktisch kein Streubesitz",
                    holder_count=count,
                )
            )
        elif count < thresholds.min_holder_count:
            findings.append(
                finding(
                    CHECK,
                    "few_holders",
                    Severity.LOW,
                    f"Wenige Holder ({count} < {thresholds.min_holder_count})",
                    holder_count=count,
                )
            )

    if not findings:
        findings.append(
            finding(
                CHECK,
                "distribution_ok",
                Severity.INFO,
                f"Verteilung unauffaellig (Top 10: {top10:.1f}%, "
                f"groesste Wallet: {largest:.1f}%)",
                top10_pct=round(top10, 2),
                source=data.holder_source,
            )
        )

    # Die Datenbasis ist auf die groessten ~20 Accounts begrenzt. Das reicht
    # fuer Konzentration, sagt aber nichts ueber den langen Schwanz aus.
    if distribution.truncated:
        findings.append(
            finding(
                CHECK,
                "distribution_truncated",
                Severity.INFO,
                "Bewertung basiert auf den groessten Token-Accounts, nicht auf allen",
            )
        )

    return findings
