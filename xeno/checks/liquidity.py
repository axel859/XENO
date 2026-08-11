"""Liquiditaet und LP-Sicherung.

Der klassische Rug: der Ersteller haelt die LP-Token, zieht die Liquiditaet
aus dem Pool und laesst wertlose Token zurueck. Dagegen hilft nur, dass die
LP-Token verbrannt oder in einem Locker festgesetzt sind.

Zweiter Punkt ist die absolute Groesse: bei duenner Liquiditaet bewegt schon
ein mittlerer Verkauf den Kurs so stark, dass ein Ausstieg zum gezeigten
Preis nicht moeglich ist.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "liquidity"


def check_liquidity(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    findings: list[Finding] = []
    rc = data.rc

    if rc.rugged:
        findings.append(
            finding(
                CHECK,
                "already_rugged",
                Severity.CRITICAL,
                "Token ist bereits als gerugged markiert",
            )
        )

    locked = rc.lp_locked_pct
    if locked is None:
        if rc.available and not rc.markets:
            findings.append(
                finding(
                    CHECK,
                    "no_market",
                    Severity.HIGH,
                    "Kein handelbarer Markt gefunden",
                )
            )
        else:
            findings.append(
                finding(
                    CHECK,
                    "lp_status_unknown",
                    Severity.MEDIUM,
                    "LP-Lock-Status unbekannt",
                )
            )
    elif locked < thresholds.min_lp_locked_pct:
        severity = Severity.CRITICAL if locked < 50 else Severity.HIGH
        findings.append(
            finding(
                CHECK,
                "lp_unlocked",
                severity,
                f"Nur {locked:.1f}% der LP-Token sind gesichert - die Liquiditaet "
                f"kann abgezogen werden",
                lp_locked_pct=round(locked, 2),
            )
        )
    else:
        findings.append(
            finding(
                CHECK,
                "lp_locked",
                Severity.INFO,
                f"LP zu {locked:.1f}% verbrannt oder gelockt",
                lp_locked_pct=round(locked, 2),
            )
        )

    # Absolute Liquiditaet: bevorzugt aus den Marktdaten, sonst aus RugCheck.
    liquidity = None
    if data.candidate and data.candidate.liquidity_usd:
        liquidity = data.candidate.liquidity_usd
    elif rc.total_liquidity_usd:
        liquidity = rc.total_liquidity_usd

    if liquidity is not None:
        if liquidity < 1_000:
            findings.append(
                finding(
                    CHECK,
                    "liquidity_dust",
                    Severity.HIGH,
                    f"Liquiditaet nur ${liquidity:,.0f} - kein realer Ausstieg moeglich",
                    liquidity_usd=round(liquidity, 2),
                )
            )
        elif liquidity < thresholds.healthy_liquidity_usd:
            findings.append(
                finding(
                    CHECK,
                    "liquidity_thin",
                    Severity.MEDIUM,
                    f"Duenne Liquiditaet (${liquidity:,.0f}) - hoher Slippage beim Verkauf",
                    liquidity_usd=round(liquidity, 2),
                )
            )

    return findings
