"""Creator-Wallet.

Das in der Praxis staerkste Einzelsignal. Serien-Rugger arbeiten mit
demselben Wallet weiter: Token launchen, Kurs laufen lassen, verkaufen,
naechster Token. Wer vorher schon Token hat sterben lassen, macht es beim
naechsten hoechstwahrscheinlich genauso.

Zweiter Punkt ist der eigene Bestand des Erstellers. Ein grosser Anteil in
der Deployer-Wallet ist ein permanenter Verkaufsdruck ueber dem Kurs.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "creator"

#: RugCheck-Risiken, die eindeutig auf die Creator-Historie zeigen.
_RUG_HISTORY_MARKERS = ("rugged", "creator history")


def check_creator(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    rc = data.rc
    if not rc.available:
        return []

    findings: list[Finding] = []

    # RugCheck fuehrt die Historie bereits als eigenes Risiko - uebernehmen,
    # statt sie nachzubauen, weil dafuer die Kurshistorie aller frueheren
    # Token des Wallets noetig waere.
    for risk in rc.risks:
        name = str(risk.get("name", ""))
        if any(marker in name.lower() for marker in _RUG_HISTORY_MARKERS):
            level = str(risk.get("level", "")).lower()
            findings.append(
                finding(
                    CHECK,
                    "creator_rug_history",
                    Severity.CRITICAL if level == "danger" else Severity.HIGH,
                    "Ersteller hat nachweislich schon Token gerugged",
                    creator=rc.creator,
                    detail_text=str(risk.get("description", "")),
                )
            )
            break

    previous = rc.creator_tokens
    if len(previous) >= 10:
        findings.append(
            finding(
                CHECK,
                "serial_deployer",
                Severity.HIGH,
                f"Ersteller hat bereits {len(previous)} Token gelauncht",
                count=len(previous),
                creator=rc.creator,
            )
        )
    elif len(previous) >= 3:
        findings.append(
            finding(
                CHECK,
                "repeat_deployer",
                Severity.MEDIUM,
                f"Ersteller hat bereits {len(previous)} Token gelauncht",
                count=len(previous),
                creator=rc.creator,
            )
        )

    # creatorBalance kommt in Roh-Einheiten; ohne Supply nicht in Prozent
    # umrechenbar, deshalb nur bei bekanntem Mint bewerten.
    balance = rc.creator_balance
    if balance > 0 and data.mint_info and data.mint_info.supply > 0:
        pct = balance / (10**data.mint_info.decimals) / data.mint_info.supply * 100.0
        if pct > thresholds.max_creator_pct:
            findings.append(
                finding(
                    CHECK,
                    "creator_holds_supply",
                    Severity.HIGH if pct >= 10 else Severity.MEDIUM,
                    f"Ersteller haelt noch {pct:.1f}% der Supply",
                    pct=round(pct, 2),
                    creator=rc.creator,
                )
            )

    if not findings and rc.creator:
        findings.append(
            finding(
                CHECK,
                "creator_unremarkable",
                Severity.INFO,
                "Keine Auffaelligkeiten beim Ersteller",
                creator=rc.creator,
            )
        )
    return findings
