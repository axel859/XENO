"""DexScreener - Marktdaten und zusaetzliche Discovery.

Ergaenzt GeckoTerminal: liefert alle Pools zu einem Mint (wichtig, wenn
Liquiditaet ueber mehrere Paare verteilt ist) sowie Social-Links aus den
Token-Profilen. Zaehlt allerdings keine eindeutigen Kaeufer, nur Trades.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import TokenCandidate
from ..net import HttpClient

BASE_URL = "https://api.dexscreener.com"


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class DexScreener:
    def __init__(self, http: HttpClient | None = None, chain: str = "solana") -> None:
        self.http = http or HttpClient(rate_limit=4.0)
        self.chain = chain

    def latest_profiles(self) -> list[TokenCandidate]:
        """Frisch beworbene Token. Guter Fruehindikator, aber ungefiltert."""
        payload = self.http.get(f"{BASE_URL}/token-profiles/latest/v1") or []
        candidates = []
        for entry in payload:
            if entry.get("chainId") != self.chain:
                continue
            mint = entry.get("tokenAddress")
            if mint:
                candidates.append(
                    TokenCandidate(mint=mint, source="dexscreener:profiles")
                )
        return candidates

    def pairs_for_token(self, mint: str) -> list[dict[str, Any]]:
        payload = self.http.get(f"{BASE_URL}/latest/dex/tokens/{mint}")
        return (payload or {}).get("pairs") or []

    def prices(self, mints: list[str], batch_size: int = 30) -> dict[str, float]:
        """Aktuelle Preise fuer viele Token auf einmal.

        Die API nimmt mehrere Adressen kommagetrennt entgegen - damit kostet
        die Nachverfolgung von 100 Token vier Requests statt hundert.

        Liegen zu einem Mint mehrere Paare vor, gewinnt das mit der hoechsten
        Liquiditaet; dort entsteht der belastbare Kurs.
        """
        result: dict[str, float] = {}
        best_liquidity: dict[str, float] = {}

        for start in range(0, len(mints), batch_size):
            chunk = mints[start : start + batch_size]
            if not chunk:
                continue
            payload = self.http.get(f"{BASE_URL}/latest/dex/tokens/{','.join(chunk)}")
            for pair in (payload or {}).get("pairs") or []:
                if pair.get("chainId") != self.chain:
                    continue
                mint = (pair.get("baseToken") or {}).get("address")
                price = _to_float(pair.get("priceUsd"))
                if not mint or price is None:
                    continue
                liquidity = _to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
                if mint not in result or liquidity > best_liquidity.get(mint, -1.0):
                    result[mint] = price
                    best_liquidity[mint] = liquidity
        return result

    def best_pair(self, mint: str) -> dict[str, Any] | None:
        """Das Paar mit der hoechsten Liquiditaet - dort wird real gehandelt."""
        pairs = [p for p in self.pairs_for_token(mint) if p.get("chainId") == self.chain]
        if not pairs:
            return None
        return max(pairs, key=lambda p: _to_float((p.get("liquidity") or {}).get("usd")) or 0.0)

    def total_liquidity_usd(self, mint: str) -> float:
        """Summe ueber alle Paare - relevant bei aufgeteilter Liquiditaet."""
        return sum(
            _to_float((p.get("liquidity") or {}).get("usd")) or 0.0
            for p in self.pairs_for_token(mint)
            if p.get("chainId") == self.chain
        )

    def enrich_many(
        self, candidates: list[TokenCandidate], batch_size: int = 30
    ) -> list[TokenCandidate]:
        """Fuellt Marktdaten fuer viele Kandidaten in wenigen Requests nach.

        Gedacht fuer den Live-Strom: dort kommen die Mints ohne Marktdaten
        an, und einzeln nachzufragen waere bei rund vierzig Neuzugaengen pro
        Minute nicht tragbar.

        Achtung bei der Deutung: DexScreener liefert Kaeufe und Verkaeufe,
        aber **keine eindeutigen Wallets**. Kandidaten aus dieser Quelle
        haben deshalb kein ``buyers_h1`` - die betreffenden Pruefungen
        melden dann "keine Aussage moeglich" statt Entwarnung.
        """
        if not candidates:
            return []

        by_mint = {c.mint: c for c in candidates}
        mints = list(by_mint)
        best: dict[str, tuple[float, dict[str, Any]]] = {}

        for start in range(0, len(mints), batch_size):
            chunk = mints[start : start + batch_size]
            try:
                payload = self.http.get(
                    f"{BASE_URL}/latest/dex/tokens/{','.join(chunk)}"
                )
            except Exception:  # noqa: BLE001 - eine Luecke ist besser als ein Abbruch
                continue
            for pair in (payload or {}).get("pairs") or []:
                if pair.get("chainId") != self.chain:
                    continue
                mint = (pair.get("baseToken") or {}).get("address")
                if mint not in by_mint:
                    continue
                liquidity = _to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
                if mint not in best or liquidity > best[mint][0]:
                    best[mint] = (liquidity, pair)

        enriched = []
        for mint, (_liquidity, pair) in best.items():
            enriched.append(self._merge(by_mint[mint], pair))
        return enriched

    def enrich(self, candidate: TokenCandidate) -> TokenCandidate:
        """Fuellt fehlende Marktdaten aus DexScreener nach.

        Vorhandene Werte werden nicht ueberschrieben - GeckoTerminal ist bei
        Pool-Daten die genauere Quelle.
        """
        pair = self.best_pair(candidate.mint)
        if not pair:
            return candidate
        return self._merge(candidate, pair)

    def as_candidate(self, pair: dict[str, Any], source: str = "dexscreener") -> TokenCandidate | None:
        base = pair.get("baseToken") or {}
        mint = base.get("address")
        if not mint:
            return None
        return self._merge(
            TokenCandidate(
                mint=mint,
                symbol=base.get("symbol", "") or "",
                name=base.get("name", "") or "",
                source=source,
            ),
            pair,
        )

    def _merge(self, candidate: TokenCandidate, pair: dict[str, Any]) -> TokenCandidate:
        volume = pair.get("volume") or {}
        txns = pair.get("txns") or {}
        h1 = txns.get("h1") or {}
        price_change = pair.get("priceChange") or {}
        base = pair.get("baseToken") or {}

        created_at = candidate.created_at
        if created_at is None and pair.get("pairCreatedAt"):
            created_at = datetime.fromtimestamp(
                pair["pairCreatedAt"] / 1000, tz=timezone.utc
            )

        def keep(current: Any, incoming: Any) -> Any:
            return current if current not in (None, "", 0) else incoming

        candidate.symbol = keep(candidate.symbol, base.get("symbol", "") or "")
        candidate.name = keep(candidate.name, base.get("name", "") or "")
        candidate.pool_address = keep(candidate.pool_address, pair.get("pairAddress", "") or "")
        candidate.dex = keep(candidate.dex, pair.get("dexId", "") or "")
        candidate.created_at = created_at
        candidate.price_usd = keep(candidate.price_usd, _to_float(pair.get("priceUsd")))
        # liquidity ist bei Bonding-Curve-Paaren null - dann bleibt der alte Wert
        candidate.liquidity_usd = keep(
            candidate.liquidity_usd, _to_float((pair.get("liquidity") or {}).get("usd"))
        )
        candidate.volume_h1_usd = keep(candidate.volume_h1_usd, _to_float(volume.get("h1")))
        candidate.volume_h24_usd = keep(candidate.volume_h24_usd, _to_float(volume.get("h24")))
        candidate.fdv_usd = keep(candidate.fdv_usd, _to_float(pair.get("fdv")))
        candidate.buys_h1 = keep(candidate.buys_h1, h1.get("buys"))
        candidate.sells_h1 = keep(candidate.sells_h1, h1.get("sells"))
        candidate.price_change_h1_pct = keep(
            candidate.price_change_h1_pct, _to_float(price_change.get("h1"))
        )
        return candidate
