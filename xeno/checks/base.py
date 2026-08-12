"""Gemeinsame Basis der Einzelpruefungen.

``TokenData`` buendelt alles, was fuer einen Token eingesammelt wurde. Jede
Pruefung ist danach eine reine Funktion ``(TokenData, RiskThresholds) ->
list[Finding]``. Kein Check macht selbst Netzwerkzugriffe - dadurch sind sie
vollstaendig ohne Netz testbar, und das Nachladen der Daten passiert genau
einmal an einer Stelle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import RiskThresholds
from ..models import Finding, HolderDistribution, MintInfo, Severity, TokenCandidate
from ..sources.helius import Origin, Trade
from ..sources.jupiter import RoundTrip
from ..sources.rugcheck import RugCheckReport
from ..structure import Structure


@dataclass
class TokenData:
    """Alle eingesammelten Rohdaten zu einem Token."""

    mint: str
    candidate: TokenCandidate | None = None
    mint_info: MintInfo | None = None
    rugcheck: RugCheckReport | None = None
    distribution: HolderDistribution | None = None
    #: Woher die Holder-Daten stammen: "rpc", "rugcheck" oder "" (keine).
    holder_source: str = ""
    #: Ergebnis des simulierten Kauf-Verkauf-Tests, None wenn uebersprungen.
    round_trip: RoundTrip | None = None
    #: Einzelne Handelsvorgaenge. None heisst "nicht abgerufen" - das ist
    #: etwas anderes als eine leere Liste ("abgerufen, nichts gefunden").
    trades: list[Trade] | None = None
    #: Ausgewerteter Kursverlauf. None heisst "keine Kerzen abgerufen".
    structure: Structure | None = None
    #: Herkunft der groessten Halter. None heisst "nicht abgerufen".
    origins: list[Origin] | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def rc(self) -> RugCheckReport:
        """RugCheck-Report, notfalls leer - spart None-Pruefungen in den Checks."""
        return self.rugcheck or RugCheckReport(mint=self.mint, raw={})


#: Signatur jeder Pruefung.
Check = Callable[[TokenData, RiskThresholds], list[Finding]]


def finding(
    check: str,
    code: str,
    severity: Severity,
    message: str,
    **detail: object,
) -> Finding:
    return Finding(check=check, code=code, severity=severity, message=message, detail=detail)
