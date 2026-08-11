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


def rank_key(candidate: TokenCandidate, profile=None) -> float:
    """Bestimmt, welche Kandidaten das knappe Pruefbudget bekommen.

    Bei frischen Token ist die Liquiditaet als Rangfolge irrefuehrend - sie
    sagt nur, wie viel jemand hineingelegt hat, nicht ob sich jemand dafuer
    interessiert. Dort zaehlt die Beteiligung.
    """
    if profile is not None and getattr(profile, "rank_by", None) == "traction":
        return candidate.traction
    return candidate.liquidity_usd or 0.0


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
        include_new: bool | None = None,
        include_trending: bool | None = None,
        pages: int | None = None,
        limit: int = 10,
        test_trade: bool = True,
        on_progress: Callable[[str], None] | None = None,
    ) -> ScanResult:
        """Fuehrt einen kompletten Durchlauf aus.

        Ohne Angabe bestimmt das aktive Profil Suchbreite, Quellen und die
        Reihenfolge der Tiefpruefungen.
        """
        profile = self.settings.profile
        if include_new is None:
            include_new = profile.include_new if profile else True
        if include_trending is None:
            include_trending = profile.include_trending if profile else True
        if pages is None:
            pages = profile.pages if profile else 1

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

        passed.sort(key=lambda r: rank_key(r.candidate, profile), reverse=True)

        for index, screen_result in enumerate(passed[:limit], start=1):
            candidate = screen_result.candidate
            report_progress(f"[{index}/{min(len(passed), limit)}] pruefe {candidate.label} ...")
            report = self.analyzer.analyze(
                candidate.mint, candidate=candidate, test_trade=test_trade
            )
            result.reports.append(report)

        result.reports.sort(key=lambda r: r.score, reverse=True)
        return result
