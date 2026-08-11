"""GeckoTerminal - Discovery neuer und laufender Pools.

Die beste kostenlose Quelle fuer Stage 1: ein Request liefert 20 Pools
inklusive Alter, Liquiditaet, Volumen und - anders als DexScreener - der
Zahl *eindeutiger* Kaeufer und Verkaeufer. Das unterscheidet echtes
Interesse von ein paar Bots, die sich gegenseitig zuspielen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import TokenCandidate
from ..net import HttpClient

BASE_URL = "https://api.geckoterminal.com/api/v2"


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        # API liefert ISO-8601 mit "Z"
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _mint_from_id(token_id: str | None) -> str:
    """"solana_<mint>" -> "<mint>"."""
    if not token_id:
        return ""
    _, _, mint = token_id.partition("_")
    return mint or token_id


class GeckoTerminal:
    def __init__(self, http: HttpClient | None = None, network: str = "solana") -> None:
        self.http = http or HttpClient(rate_limit=0.5)  # API erlaubt ~30/min
        self.network = network

    def _pools(self, endpoint: str, pages: int, source: str) -> list[TokenCandidate]:
        candidates: list[TokenCandidate] = []
        for page in range(1, pages + 1):
            payload = self.http.get(
                f"{BASE_URL}/networks/{self.network}/{endpoint}", params={"page": page}
            )
            entries = (payload or {}).get("data") or []
            if not entries:
                break
            for entry in entries:
                candidate = self._to_candidate(entry, source)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def new_pools(self, pages: int = 1) -> list[TokenCandidate]:
        """Frisch erstellte Pools, neueste zuerst."""
        return self._pools("new_pools", pages, "geckoterminal:new")

    def trending_pools(self, pages: int = 1) -> list[TokenCandidate]:
        """Pools mit aktuell auffaelliger Aktivitaet."""
        return self._pools("trending_pools", pages, "geckoterminal:trending")

    def _to_candidate(self, entry: dict[str, Any], source: str) -> TokenCandidate | None:
        attributes = entry.get("attributes") or {}
        relationships = entry.get("relationships") or {}

        base = ((relationships.get("base_token") or {}).get("data") or {}).get("id")
        mint = _mint_from_id(base)
        if not mint:
            return None

        dex = ((relationships.get("dex") or {}).get("data") or {}).get("id", "")

        volume = attributes.get("volume_usd") or {}
        txns = attributes.get("transactions") or {}
        h1 = txns.get("h1") or {}
        price_change = attributes.get("price_change_percentage") or {}

        # "MOMOTA / SOL" -> "MOMOTA"
        name = str(attributes.get("name") or "")
        symbol = name.split("/")[0].strip() if "/" in name else name.strip()

        return TokenCandidate(
            mint=mint,
            symbol=symbol,
            name=name,
            pool_address=attributes.get("address", "") or "",
            dex=dex,
            source=source,
            created_at=_parse_time(attributes.get("pool_created_at")),
            price_usd=_to_float(attributes.get("base_token_price_usd")),
            liquidity_usd=_to_float(attributes.get("reserve_in_usd")),
            volume_h1_usd=_to_float(volume.get("h1")),
            volume_h24_usd=_to_float(volume.get("h24")),
            fdv_usd=_to_float(attributes.get("fdv_usd")),
            buys_h1=h1.get("buys"),
            sells_h1=h1.get("sells"),
            buyers_h1=h1.get("buyers"),
            sellers_h1=h1.get("sellers"),
            price_change_h1_pct=_to_float(price_change.get("h1")),
        )

    @staticmethod
    def quote_mint(entry: dict[str, Any]) -> str:
        relationships = entry.get("relationships") or {}
        quote = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
        return _mint_from_id(quote)
