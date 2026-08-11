"""Ausgabe der Ergebnisse."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .models import RiskReport, ScreenResult, Severity, Verdict

_COLORS = {
    Verdict.AVOID: "\033[91m",
    Verdict.RISKY: "\033[93m",
    Verdict.CAUTION: "\033[93m",
    Verdict.OK: "\033[92m",
    Verdict.UNKNOWN: "\033[90m",
}
_SEVERITY_COLORS = {
    Severity.CRITICAL: "\033[91m",
    Severity.HIGH: "\033[91m",
    Severity.MEDIUM: "\033[93m",
    Severity.LOW: "\033[90m",
    Severity.INFO: "\033[90m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"

_MARKS = {
    Severity.CRITICAL: "XX",
    Severity.HIGH: " X",
    Severity.MEDIUM: " !",
    Severity.LOW: " -",
    Severity.INFO: " .",
}


def use_color(stream: Any = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def _paint(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{_RESET}" if enabled else text


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:.0f}"


def _age(minutes: float | None) -> str:
    if minutes is None:
        return "n/a"
    if minutes < 60:
        return f"{minutes:.0f}min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def format_report(report: RiskReport, verbose: bool = False, color: bool | None = None) -> str:
    """Ausfuehrlicher Report zu einem Token."""
    enabled = use_color() if color is None else color
    lines: list[str] = []

    title = report.symbol or report.name or report.mint[:12]
    verdict = report.verdict
    header = f"{title}  [{verdict.value}]  Score {report.score}/100"
    lines.append(_paint(_BOLD + header + _RESET, _COLORS[verdict], enabled))
    lines.append(f"  Mint: {report.mint}")

    candidate = report.candidate
    if candidate:
        lines.append(
            f"  Markt: Liq {_money(candidate.liquidity_usd)}"
            f" | Vol 1h {_money(candidate.volume_h1_usd)}"
            f" | FDV {_money(candidate.fdv_usd)}"
            f" | Alter {_age(candidate.age_minutes)}"
        )

    distribution = report.distribution
    if distribution:
        lines.append(
            f"  Holder: Top10 {distribution.top10_pct:.1f}%"
            f" | groesste {distribution.largest_pct:.1f}%"
            f" | Pools/Burn ausgeklammert {distribution.excluded_pct:.1f}%"
            + (
                f" | gesamt {distribution.holder_count}"
                if distribution.holder_count is not None
                else ""
            )
        )

    lines.append("")
    order = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]
    shown = order if verbose else order[:4]
    for severity in shown:
        for f in report.by_severity(severity):
            mark = _MARKS[severity]
            lines.append(
                f"  {_paint(mark, _SEVERITY_COLORS[severity], enabled)} "
                f"[{f.check}] {f.message}"
            )

    if not verbose:
        info_count = len(report.by_severity(Severity.INFO))
        if info_count:
            lines.append(f"  .. {info_count} unauffaellige Pruefungen (--verbose zeigt alle)")

    gaps = report.data_gaps
    if gaps:
        lines.append("")
        lines.append(f"  Wissensluecken: {', '.join(gaps)}")

    if report.errors:
        lines.append("")
        for error in report.errors:
            lines.append(f"  ! {error}")

    return "\n".join(lines)


def format_report_line(report: RiskReport, color: bool | None = None) -> str:
    """Eine Zeile pro Token - fuer Listenausgaben."""
    enabled = use_color() if color is None else color
    verdict = report.verdict
    title = (report.symbol or report.mint[:10])[:14]
    top = report.distribution.top10_pct if report.distribution else None
    liq = report.candidate.liquidity_usd if report.candidate else None
    worst = next(
        (
            f
            for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
            for f in report.by_severity(s)
        ),
        None,
    )
    return (
        f"{_paint(f'{verdict.value:8}', _COLORS[verdict], enabled)} "
        f"{report.score:3d}  {title:14.14} "
        f"liq {_money(liq):>8}  "
        f"top10 {f'{top:.0f}%' if top is not None else '  n/a':>5}  "
        f"{worst.message[:58] if worst else 'keine Auffaelligkeiten'}"
    )


def format_screen_line(result: ScreenResult) -> str:
    candidate = result.candidate
    status = "PASS" if result.passed else "skip"
    line = (
        f"{status}  {candidate.label:14.14} "
        f"liq {_money(candidate.liquidity_usd):>8}  "
        f"vol1h {_money(candidate.volume_h1_usd):>8}  "
        f"alter {_age(candidate.age_minutes):>6}"
    )
    if not result.passed:
        line += f"   <- {'; '.join(result.reasons)}"
    return line


def to_json(reports: list[RiskReport]) -> str:
    return json.dumps([r.to_dict() for r in reports], indent=2, ensure_ascii=False)
