"""Registry aller Einzelpruefungen.

Reihenfolge bestimmt die Reihenfolge im Report. Eine neue Pruefung wird
angelegt, indem eine Funktion mit der Signatur ``(TokenData, RiskThresholds)
-> list[Finding]`` hier eingetragen wird.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding
from .authority import check_authorities
from .base import Check, TokenData, finding
from .botting import check_botting
from .bundling import check_bundling
from .creator import check_creator
from .extensions import check_extensions
from .holders import check_holders
from .liquidity import check_liquidity
from .tradability import check_tradability
from .tradepattern import check_trade_pattern

ALL_CHECKS: list[Check] = [
    check_authorities,
    check_extensions,
    check_liquidity,
    check_holders,
    check_bundling,
    check_botting,
    check_trade_pattern,
    check_creator,
    check_tradability,
]


def run_checks(
    data: TokenData,
    thresholds: RiskThresholds,
    checks: list[Check] | None = None,
) -> list[Finding]:
    """Fuehrt alle Pruefungen aus.

    Ein Fehler in einer Pruefung darf die uebrigen nicht mitreissen - er wird
    als eigener Befund vermerkt.
    """
    results: list[Finding] = []
    for check in checks if checks is not None else ALL_CHECKS:
        try:
            results.extend(check(data, thresholds))
        except Exception as exc:  # noqa: BLE001
            from ..models import Severity

            results.append(
                finding(
                    getattr(check, "__name__", "check"),
                    "check_failed",
                    Severity.LOW,
                    f"Pruefung fehlgeschlagen: {exc}",
                )
            )
    return results


__all__ = ["ALL_CHECKS", "Check", "TokenData", "run_checks", "finding"]
