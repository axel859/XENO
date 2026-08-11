"""Jupiter - Handelbarkeit und Honeypot-Erkennung.

Der praktischste Test ueberhaupt: nicht fragen, ob ein Verkauf theoretisch
erlaubt ist, sondern eine echte Route dafuer anfragen. Jupiter simuliert
gegen den tatsaechlichen Pool-Zustand.

Ablauf: fuer einen festen SOL-Betrag eine Kauf-Route holen, und fuer die
Menge, die dabei herauskaeme, sofort wieder eine Verkaufs-Route. Was von den
eingesetzten SOL uebrig bleibt, ist die *Round-Trip-Rate*. Bei einem
gesunden Token liegen dort nach Gebuehren und Slippage rund 95%. Schlaegt
die Verkaufsanfrage fehl oder bleibt viel zu wenig uebrig, laesst sich die
Position nicht sinnvoll aufloesen - unabhaengig davon, was der Chart zeigt.

Es werden ausschliesslich Quotes abgefragt. Es wird nichts signiert und
nichts gesendet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..known import WSOL_MINT
from ..net import HttpClient, HttpError

QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"

#: Testgroesse fuer den Round-Trip: 0.1 SOL in Lamports.
DEFAULT_PROBE_LAMPORTS = 100_000_000


@dataclass
class RoundTrip:
    """Ergebnis des Kauf-Verkauf-Tests."""

    buy_ok: bool = False
    sell_ok: bool = False
    lamports_in: int = 0
    lamports_out: int = 0
    tokens_received: int = 0
    buy_price_impact: float | None = None
    sell_price_impact: float | None = None
    error: str = ""

    @property
    def retention(self) -> float | None:
        """Anteil des Einsatzes, der nach Hin- und Rueckweg uebrig bleibt (0-1)."""
        if not self.sell_ok or self.lamports_in <= 0:
            return None
        return self.lamports_out / self.lamports_in

    @property
    def unreliable(self) -> bool:
        """True, wenn der Round-Trip mehr zurueckgibt als eingesetzt wurde.

        Das kann real nicht vorkommen - waere es echt, waere es risikolose
        Arbitrage. Bei frisch gestarteten Bonding-Curve-Token liefert Jupiter
        aber genau solche Werte, weil Kauf- und Verkaufsseite gegen
        unterschiedlich aktuelle Pool-Staende gerechnet werden. Ein Ergebnis
        mit diesem Flag darf nicht als Risikosignal gewertet werden.
        """
        retention = self.retention
        return retention is not None and retention > 1.05

    @property
    def loss_pct(self) -> float | None:
        retention = self.retention
        if retention is None or self.unreliable:
            return None
        return (1.0 - retention) * 100.0


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Jupiter:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(rate_limit=2.0)

    def quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 500
    ) -> dict[str, Any] | None:
        try:
            return self.http.get(
                QUOTE_URL,
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount,
                    "slippageBps": slippage_bps,
                },
            )
        except HttpError:
            # Keine Route ist eine gueltige Antwort, kein Programmfehler.
            return None

    def round_trip(
        self, mint: str, lamports: int = DEFAULT_PROBE_LAMPORTS
    ) -> RoundTrip:
        """Kauft und verkauft simuliert und meldet, was uebrig bleibt."""
        result = RoundTrip(lamports_in=lamports)

        buy = self.quote(WSOL_MINT, mint, lamports)
        if not buy or not buy.get("outAmount"):
            result.error = "keine Kauf-Route"
            return result

        try:
            tokens = int(buy["outAmount"])
        except (TypeError, ValueError):
            result.error = "unlesbare Kauf-Route"
            return result

        result.buy_ok = True
        result.tokens_received = tokens
        result.buy_price_impact = _to_float(buy.get("priceImpactPct"))

        if tokens <= 0:
            result.error = "Kauf-Route liefert 0 Token"
            return result

        # Ein zweiter Versuch, bevor "kein Verkauf moeglich" behauptet wird:
        # bei gerade erst erstellten Pools liegt das haeufig an der Indexierung.
        sell = self.quote(mint, WSOL_MINT, tokens)
        if not sell or not sell.get("outAmount"):
            time.sleep(2.0)
            sell = self.quote(mint, WSOL_MINT, tokens)
        if not sell or not sell.get("outAmount"):
            result.error = "keine Verkaufs-Route"
            return result

        try:
            result.lamports_out = int(sell["outAmount"])
        except (TypeError, ValueError):
            result.error = "unlesbare Verkaufs-Route"
            return result

        result.sell_ok = True
        result.sell_price_impact = _to_float(sell.get("priceImpactPct"))
        return result
