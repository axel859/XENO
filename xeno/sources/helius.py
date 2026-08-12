"""Helius - vorgeparste Transaktionen eines Tokens.

Der Umweg ueber den normalen RPC waere hier untragbar: erst die Signaturen
holen, dann jede Transaktion einzeln nachladen - das waeren pro Token
hundert Anfragen. Helius liefert bis zu hundert bereits ausgewertete
Transaktionen in **einer** Anfrage.

Gebraucht wird das fuer die Frage, ob hinter dem Handel Menschen oder ein
Programm stecken. Dafuer braucht es die einzelnen Trades mit Betrag und
Wallet - aus den zusammengefassten Marktdaten laesst sich das nicht ablesen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..net import HttpClient, HttpError

BASE_URL = "https://api.helius.xyz/v0"

#: Unterhalb dieses Betrags ist es kein Handel, sondern Gebuehr oder Staub.
MIN_TRADE_LAMPORTS = 20_000


@dataclass
class Trade:
    """Ein einzelner Handel: wer, wann, wie viel."""

    wallet: str
    lamports: int
    timestamp: int = 0

    @property
    def sol(self) -> float:
        return self.lamports / 1e9


def api_key_from(rpc_url: str = "") -> str:
    """Ermittelt den Helius-Schluessel aus Umgebung oder RPC-Adresse."""
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if key:
        return key
    if "helius" in rpc_url:
        found = parse_qs(urlparse(rpc_url).query).get("api-key")
        if found:
            return found[0]
    return ""


def extract_trades(transactions: list[dict[str, Any]]) -> list[Trade]:
    """Liest aus den Transaktionen die eigentlichen Handelsvorgaenge.

    Die Handelsgroesse ist die SOL-Bewegung des Gebuehrenzahlers - er ist
    derjenige, der kauft oder verkauft. Betrag und Richtung sind dabei
    gleichgueltig; fuer die Mustererkennung zaehlt die *Groesse*.
    """
    trades: list[Trade] = []
    for tx in transactions:
        payer = tx.get("feePayer")
        if not payer:
            continue
        lamports = 0
        for transfer in tx.get("nativeTransfers") or []:
            amount = transfer.get("amount") or 0
            if transfer.get("fromUserAccount") == payer:
                lamports += amount
            elif transfer.get("toUserAccount") == payer:
                lamports -= amount
        if abs(lamports) >= MIN_TRADE_LAMPORTS:
            trades.append(
                Trade(
                    wallet=payer,
                    lamports=abs(lamports),
                    timestamp=int(tx.get("timestamp") or 0),
                )
            )
    return trades


class Helius:
    def __init__(self, api_key: str = "", http: HttpClient | None = None) -> None:
        self.api_key = api_key or api_key_from()
        self.http = http or HttpClient(rate_limit=5.0, timeout=30.0)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def recent_trades(self, mint: str, limit: int = 100) -> list[Trade]:
        """Die letzten Handelsvorgaenge eines Tokens - eine Anfrage.

        Leere Liste bei fehlendem Schluessel oder Fehler; die auswertende
        Pruefung meldet dann "keine Aussage moeglich" statt einer Entwarnung.
        """
        if not self.available:
            return []
        try:
            payload = self.http.get(
                f"{BASE_URL}/addresses/{mint}/transactions",
                params={"api-key": self.api_key, "limit": min(limit, 100)},
            )
        except HttpError:
            return []
        if not isinstance(payload, list):
            return []
        return extract_trades(payload)
