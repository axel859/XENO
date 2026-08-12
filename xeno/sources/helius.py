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

#: So viele Transaktionen einer Wallet werden angesehen. Der Wert ist kein
#: Kompromiss, sondern eine Entscheidung: Wallets mit einer vollen Seite sind
#: aktiv genug, um keine frisch angelegte Bundle-Wallet zu sein. Fuer alle
#: anderen liegt damit die **gesamte** Historie vor - die aelteste
#: Transaktion steht am Ende der Seite, ganz ohne Blaettern.
WALLET_PAGE = 100

#: Unterhalb dieses Betrags ist eine Einzahlung keine Finanzierung, sondern
#: ein Staub-Transfer - unter anderem die beliebte Masche, fremde Wallets mit
#: winzigen Betraegen anzuschreiben.
MIN_FUNDING_LAMPORTS = 1_000_000  # 0.001 SOL


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


@dataclass
class Origin:
    """Woher eine Wallet ihr erstes Geld bekommen hat."""

    wallet: str
    #: Adresse, die zuerst SOL geschickt hat. Leer, wenn nicht auffindbar.
    funder: str = ""
    #: Zeitpunkt dieser ersten Einzahlung.
    funded_at: int = 0
    #: Zahl der gesehenen Transaktionen. Eine volle Seite heisst "mindestens".
    tx_count: int = 0
    #: Ob die Wallet zu aktiv ist, um ihre Historie in einer Seite zu fassen.
    established: bool = False

    @property
    def known(self) -> bool:
        return bool(self.funder)


def first_funder(transactions: list[dict[str, Any]], wallet: str) -> tuple[str, int]:
    """Sucht die erste nennenswerte Einzahlung auf eine Wallet.

    Durchlaufen wird von der aeltesten Transaktion vorwaerts. Die erste, bei
    der jemand anderes SOL an diese Wallet schickt, ist ihre Geburtsstunde -
    davor konnte sie nichts tun, weil ohne SOL auf Solana keine Gebuehr
    bezahlbar ist.
    """
    ordered = sorted(transactions, key=lambda t: int(t.get("timestamp") or 0))
    for tx in ordered:
        for transfer in tx.get("nativeTransfers") or []:
            if transfer.get("toUserAccount") != wallet:
                continue
            sender = transfer.get("fromUserAccount") or ""
            amount = transfer.get("amount") or 0
            if sender and sender != wallet and amount >= MIN_FUNDING_LAMPORTS:
                return sender, int(tx.get("timestamp") or 0)
    return "", 0


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

    def origin(self, wallet: str) -> Origin | None:
        """Herkunft einer einzelnen Wallet - eine Anfrage.

        ``None`` heisst "nicht abrufbar" und ist ausdruecklich keine
        Entwarnung; die auswertende Pruefung fuehrt das als Luecke.
        """
        if not self.available or not wallet:
            return None
        try:
            payload = self.http.get(
                f"{BASE_URL}/addresses/{wallet}/transactions",
                params={"api-key": self.api_key, "limit": WALLET_PAGE},
            )
        except HttpError:
            return None
        if not isinstance(payload, list):
            return None

        result = Origin(wallet=wallet, tx_count=len(payload))
        if len(payload) >= WALLET_PAGE:
            # Volle Seite: die Wallet handelt viel zu viel, um frisch fuer
            # diesen einen Token angelegt worden zu sein. Weiterzublaettern
            # kostete Anfragen fuer eine Antwort, die wir schon haben.
            result.established = True
            return result

        result.funder, result.funded_at = first_funder(payload, wallet)
        return result

    def origins(self, wallets: list[str], budget: int = 10) -> list[Origin]:
        """Herkunft mehrerer Wallets, mit harter Obergrenze an Anfragen."""
        found: list[Origin] = []
        for wallet in wallets[:budget]:
            result = self.origin(wallet)
            if result is not None:
                found.append(result)
        return found
