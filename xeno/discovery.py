"""Stage 0: Kandidaten einsammeln.

Fasst mehrere Quellen zusammen und entfernt Duplikate. Derselbe Mint taucht
haeufig in mehreren Quellen auf; behalten wird der Eintrag mit den meisten
Marktdaten, damit das Screening danach nicht an fehlenden Feldern scheitert.
"""

from __future__ import annotations

from typing import Callable

from .models import TokenCandidate
from .sources import DexScreener, GeckoTerminal


def _completeness(candidate: TokenCandidate) -> int:
    """Wie viele fuer das Screening relevante Felder gefuellt sind."""
    fields = (
        candidate.created_at,
        candidate.liquidity_usd,
        candidate.volume_h1_usd,
        candidate.buyers_h1,
        candidate.sellers_h1,
        candidate.buys_h1,
        candidate.price_usd,
    )
    return sum(1 for value in fields if value is not None)


def merge_candidates(groups: list[list[TokenCandidate]]) -> list[TokenCandidate]:
    """Dedupliziert ueber alle Quellen hinweg nach Mint."""
    best: dict[str, TokenCandidate] = {}
    for group in groups:
        for candidate in group:
            existing = best.get(candidate.mint)
            if existing is None or _completeness(candidate) > _completeness(existing):
                best[candidate.mint] = candidate
    return list(best.values())


class Discovery:
    """Sammelt neue und laufende Token."""

    def __init__(
        self,
        gecko: GeckoTerminal | None = None,
        dexscreener: DexScreener | None = None,
    ) -> None:
        self.gecko = gecko or GeckoTerminal()
        self.dexscreener = dexscreener or DexScreener()

    def collect(
        self,
        include_new: bool = True,
        include_trending: bool = True,
        pages: int = 1,
        on_error: Callable[[str], None] | None = None,
    ) -> list[TokenCandidate]:
        """Sammelt Kandidaten. Eine Seite entspricht 20 Pools.

        Faellt eine Quelle aus, wird die andere trotzdem ausgewertet - ein
        Aussetzer soll nicht den ganzen Durchlauf leer ausgehen lassen.

        ``on_error`` ist dabei nicht optionales Beiwerk: ohne Rueckmeldung
        sieht ein gedrosselter Zugang genauso aus wie "es gab nichts Neues" -
        naemlich nach null gefundenen Token, ohne jeden Hinweis auf die
        Ursache.
        """
        groups: list[list[TokenCandidate]] = []
        if include_new:
            groups.append(
                self._safely(self.gecko.new_pools, pages, "GeckoTerminal (neue Pools)", on_error)
            )
        if include_trending:
            # Trending liefert deutlich weniger Nachschub als neue Pools und
            # wiederholt sich stark - mehr als drei Seiten bringen nichts.
            groups.append(
                self._safely(
                    self.gecko.trending_pools, min(pages, 3), "GeckoTerminal (Trending)", on_error
                )
            )
        return merge_candidates(groups)

    @staticmethod
    def _safely(
        fetch,
        pages: int,
        label: str,
        on_error: Callable[[str], None] | None,
    ) -> list[TokenCandidate]:
        try:
            return fetch(pages=pages)
        except Exception as exc:  # noqa: BLE001
            if on_error is not None:
                message = str(exc)
                if "429" in message:
                    message = (
                        "Anfragelimit erreicht - weniger Seiten abfragen "
                        "(--pages) oder Intervall erhoehen"
                    )
                on_error(f"{label}: {message}")
            return []
