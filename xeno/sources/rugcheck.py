"""RugCheck - Deep-Daten ohne eigenen RPC-Key.

Wichtig fuer die Praxis: der oeffentliche Solana-RPC sperrt
``getTokenLargestAccounts`` komplett, die Holder-Analyse ist darueber also
nicht moeglich. RugCheck liefert dieselben Daten aufbereitet und dazu noch
Creator-Historie und erkannte Insider-Netzwerke.

Achtung bei ``score_normalised``: dort bedeutet ein *hoher* Wert ein hohes
Risiko - genau umgekehrt zum XENO-Score. ``risk_score_inverted`` rechnet um.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..net import HttpClient, HttpError

BASE_URL = "https://api.rugcheck.xyz/v1"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RugCheckHolder:
    address: str
    owner: str
    pct: float
    ui_amount: float
    insider: bool = False


@dataclass
class RugCheckReport:
    """Getypte Sicht auf die RugCheck-Antwort."""

    mint: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return bool(self.raw)

    @property
    def symbol(self) -> str:
        return (self.raw.get("tokenMeta") or {}).get("symbol", "") or ""

    @property
    def name(self) -> str:
        return (self.raw.get("tokenMeta") or {}).get("name", "") or ""

    @property
    def risk_score_raw(self) -> float:
        """RugCheck-Skala: hoeher = riskanter."""
        return _to_float(self.raw.get("score_normalised"))

    @property
    def rugged(self) -> bool:
        return bool(self.raw.get("rugged"))

    @property
    def risks(self) -> list[dict[str, Any]]:
        return self.raw.get("risks") or []

    @property
    def mint_authority(self) -> str | None:
        return self.raw.get("mintAuthority") or None

    @property
    def freeze_authority(self) -> str | None:
        return self.raw.get("freezeAuthority") or None

    @property
    def token_program(self) -> str:
        return self.raw.get("tokenProgram", "") or ""

    @property
    def creator(self) -> str:
        return self.raw.get("creator", "") or ""

    @property
    def creator_balance(self) -> float:
        return _to_float(self.raw.get("creatorBalance"))

    @property
    def creator_tokens(self) -> list[dict[str, Any]]:
        """Frueher vom selben Wallet gelaunchte Token."""
        return self.raw.get("creatorTokens") or []

    @property
    def total_holders(self) -> int | None:
        """Holder-Anzahl, oder None wenn der Wert nicht belastbar ist.

        Bei frisch erstellten Token liefert die API ``totalHolders: 0``,
        obwohl gleichzeitig Top-Holder zurueckkommen - der Zaehler ist dann
        schlicht noch nicht befuellt. Dieser Widerspruch wird hier zu "nicht
        bekannt" aufgeloest, sonst meldet der Holder-Check "praktisch kein
        Streubesitz" fuer voellig normale Neustarts.
        """
        value = self.raw.get("totalHolders")
        if not isinstance(value, (int, float)):
            return None
        count = int(value)
        if count == 0 and (self.raw.get("topHolders") or []):
            return None
        return count

    @property
    def total_liquidity_usd(self) -> float:
        return _to_float(self.raw.get("totalMarketLiquidity"))

    @property
    def launchpad(self) -> str:
        return (self.raw.get("launchpad") or {}).get("name", "") or ""

    @property
    def top_holders(self) -> list[RugCheckHolder]:
        return [
            RugCheckHolder(
                address=h.get("address", "") or "",
                owner=h.get("owner", "") or "",
                pct=_to_float(h.get("pct")),
                ui_amount=_to_float(h.get("uiAmount")),
                insider=bool(h.get("insider")),
            )
            for h in (self.raw.get("topHolders") or [])
        ]

    @property
    def insider_networks(self) -> list[dict[str, Any]]:
        """Erkannte Wallet-Cluster - der Bundling-Indikator."""
        return self.raw.get("insiderNetworks") or []

    @property
    def insider_pct(self) -> float:
        """Supply-Anteil, der auf als Insider markierte Holder entfaellt."""
        return sum(h.pct for h in self.top_holders if h.insider)

    @property
    def lp_locked_pct(self) -> float | None:
        """Hoechster LP-Lock-Anteil ueber alle Maerkte.

        Mehrere Maerkte werden nicht summiert - entscheidend ist, ob der
        Hauptmarkt gesichert ist.
        """
        values = [
            _to_float((m.get("lp") or {}).get("lpLockedPct"), default=-1.0)
            for m in (self.raw.get("markets") or [])
        ]
        values = [v for v in values if v >= 0]
        return max(values) if values else None

    @property
    def markets(self) -> list[dict[str, Any]]:
        return self.raw.get("markets") or []

    @property
    def transfer_fee_pct(self) -> float:
        fee = self.raw.get("transferFee") or {}
        return _to_float(fee.get("pct"))

    @property
    def token_extensions(self) -> Any:
        return self.raw.get("token_extensions")

    @property
    def pool_addresses(self) -> set[str]:
        """Alle Adressen, die zu Pools gehoeren - fuer den Holder-Ausschluss."""
        addresses: set[str] = set()
        for market in self.markets:
            for key in ("pubkey", "liquidityA", "liquidityB", "mintLPAccount",
                        "liquidityAAccount", "liquidityBAccount"):
                value = market.get(key)
                if isinstance(value, str) and value:
                    addresses.add(value)
        return addresses


class RugCheck:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(rate_limit=2.0)

    def report(self, mint: str) -> RugCheckReport:
        """Vollreport. Bei Fehler ein leerer Report statt einer Exception -
        der Deep-Check soll weiterlaufen und die Luecke als solche melden."""
        try:
            payload = self.http.get(f"{BASE_URL}/tokens/{mint}/report")
        except HttpError:
            return RugCheckReport(mint=mint, raw={})
        if not isinstance(payload, dict):
            return RugCheckReport(mint=mint, raw={})
        return RugCheckReport(mint=mint, raw=payload)

    def new_tokens(self) -> list[dict[str, Any]]:
        try:
            return self.http.get(f"{BASE_URL}/stats/new_tokens") or []
        except HttpError:
            return []
