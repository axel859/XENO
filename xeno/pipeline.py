"""Die vollstaendige Kette: finden -> vorfiltern -> tief pruefen.

Der Aufbau ist bewusst als Trichter gebaut. Der Deep-Check kostet pro Token
mehrere Requests, die Discovery liefert dagegen 20 Kandidaten pro Request.
Wuerde man alles tief pruefen, waere das Kontingent nach wenigen Minuten
aufgebraucht - deshalb muss Stage 1 den Grossteil aussortieren.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .analyzer import TokenAnalyzer
from .config import Settings
from .discovery import Discovery
from .models import RiskReport, ScreenResult, TokenCandidate
from .screen import screen_all


@dataclass
class ScanResult:
    """Ergebnis eines kompletten Durchlaufs."""

    discovered: list[TokenCandidate] = field(default_factory=list)
    screened: list[ScreenResult] = field(default_factory=list)
    reports: list[RiskReport] = field(default_factory=list)

    @property
    def passed_screen(self) -> list[ScreenResult]:
        return [r for r in self.screened if r.passed]


class Scanner:
    def __init__(
        self,
        settings: Settings | None = None,
        discovery: Discovery | None = None,
        analyzer: TokenAnalyzer | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.discovery = discovery or Discovery()
        self.analyzer = analyzer or TokenAnalyzer(self.settings)

    def run(
        self,
        include_new: bool = True,
        include_trending: bool = True,
        pages: int = 1,
        limit: int = 10,
        test_trade: bool = True,
        on_progress: Callable[[str], None] | None = None,
    ) -> ScanResult:
        def report_progress(message: str) -> None:
            if on_progress:
                on_progress(message)

        result = ScanResult()

        report_progress("Suche Kandidaten ...")
        result.discovered = self.discovery.collect(
            include_new=include_new, include_trending=include_trending, pages=pages
        )
        report_progress(f"{len(result.discovered)} Kandidaten gefunden")

        result.screened = screen_all(result.discovered, self.settings.screen)
        passed = result.passed_screen
        report_progress(f"{len(passed)} durch den Vorfilter")

        # Die liquidesten zuerst - dort ist ein Einstieg ueberhaupt umsetzbar.
        passed.sort(key=lambda r: r.candidate.liquidity_usd or 0.0, reverse=True)

        for index, screen_result in enumerate(passed[:limit], start=1):
            candidate = screen_result.candidate
            report_progress(f"[{index}/{min(len(passed), limit)}] pruefe {candidate.label} ...")
            report = self.analyzer.analyze(
                candidate.mint, candidate=candidate, test_trade=test_trade
            )
            result.reports.append(report)

        result.reports.sort(key=lambda r: r.score, reverse=True)
        return result
