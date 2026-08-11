"""Mint- und Freeze-Authority.

Die beiden wichtigsten Einzelpruefungen ueberhaupt, weil sie eindeutig sind:

* Aktive **Mint-Authority** heisst, der Ersteller kann jederzeit beliebig
  viele Token nachdrucken und damit deinen Anteil auf null verwaessern.
* Aktive **Freeze-Authority** heisst, er kann einzelne Token-Accounts
  einfrieren. Du kannst dann kaufen, aber nie wieder verkaufen.

Beides ist kein Verdacht, sondern ein direkt ablesbarer Zustand.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "authority"


def check_authorities(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    findings: list[Finding] = []

    mint_info = data.mint_info
    rc = data.rc

    # RPC ist die verlaessliche Quelle; RugCheck nur, wenn kein RPC-Ergebnis vorliegt.
    if mint_info is not None:
        mint_authority = mint_info.mint_authority
        freeze_authority = mint_info.freeze_authority
        source = "rpc"
    elif rc.available:
        mint_authority = rc.mint_authority
        freeze_authority = rc.freeze_authority
        source = "rugcheck"
    else:
        return [
            finding(
                CHECK,
                "authority_unknown",
                Severity.HIGH,
                "Mint-/Freeze-Authority konnten nicht geprueft werden",
            )
        ]

    if mint_authority:
        findings.append(
            finding(
                CHECK,
                "mint_authority_active",
                Severity.CRITICAL,
                "Mint-Authority ist aktiv - der Ersteller kann unbegrenzt nachdrucken",
                authority=mint_authority,
                source=source,
            )
        )
    if freeze_authority:
        findings.append(
            finding(
                CHECK,
                "freeze_authority_active",
                Severity.CRITICAL,
                "Freeze-Authority ist aktiv - Wallets koennen am Verkauf gehindert werden",
                authority=freeze_authority,
                source=source,
            )
        )

    if not findings:
        findings.append(
            finding(
                CHECK,
                "authorities_revoked",
                Severity.INFO,
                "Mint- und Freeze-Authority sind abgegeben",
                source=source,
            )
        )
    return findings
