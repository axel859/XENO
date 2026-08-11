"""Stage 0: Kandidaten einsammeln.

Fasst mehrere Quellen zusammen und entfernt Duplikate. Derselbe Mint taucht
haeufig in mehreren Quellen auf; behalten wird der Eintrag mit den meisten
Marktdaten, damit das Screening danach nicht an fehlenden Feldern scheitert.
"""

from __future__ import annotations

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
    ) -> list[TokenCandidate]:
        """Sammelt Kandidaten. Eine Seite entspricht 20 Pools.

        Bei Fehlern einer Quelle wird die andere trotzdem ausgewertet - ein
        Aussetzer soll nicht den ganzen Durchlauf leer ausgehen lassen.
        """
        groups: list[list[TokenCandidate]] = []
        if include_new:
            groups.append(self._safely(self.gecko.new_pools, pages))
        if include_trending:
            # Trending liefert deutlich weniger Nachschub als neue Pools und
            # wiederholt sich stark - mehr als drei Seiten bringen nichts.
            groups.append(self._safely(self.gecko.trending_pools, min(pages, 3)))
        return merge_candidates(groups)

    @staticmethod
    def _safely(fetch, pages: int) -> list[TokenCandidate]:
        try:
            return fetch(pages=pages)
        except Exception:  # noqa: BLE001
            return []
