"""Stage 2: Deep-Check eines einzelnen Tokens.

Sammelt alle Rohdaten ein und laesst die Pruefungen darauf laufen. Die
Trennung ist bewusst: hier passiert der gesamte Netzwerkzugriff, in
``checks/`` keiner. Dadurch sind die Bewertungsregeln ohne Netz testbar.

Zur Datenlage: der oeffentliche Solana-RPC beantwortet
``getTokenLargestAccounts`` grundsaetzlich nicht, die Holder-Analyse laeuft
darueber also ins Leere. Deshalb zwei Wege - RPC, wenn er liefert, sonst
RugCheck. Liefert keiner von beiden, wird das als Luecke gemeldet und der
Token landet in ``UNKNOWN`` statt faelschlich in ``OK``.
"""

from __future__ import annotations

from .chain import (
    distribution_from_rugcheck,
    fetch_distribution_via_rpc,
    parse_mint_account,
)
from .checks import TokenData, run_checks
from .config import Settings
from .models import RiskReport, TokenCandidate
from .net import HttpClient, SolanaRpc
from .sources import DexScreener, RugCheck
from .sources.helius import Helius
from .sources.jupiter import Jupiter


def _key_from(rpc_url: str) -> str:
    from .sources.helius import api_key_from

    return api_key_from(rpc_url)


class TokenAnalyzer:
    def __init__(
        self,
        settings: Settings | None = None,
        rpc: SolanaRpc | None = None,
        rugcheck: RugCheck | None = None,
        jupiter: Jupiter | None = None,
        dexscreener: DexScreener | None = None,
        helius: Helius | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        http = HttpClient(
            rate_limit=self.settings.http_rate_limit,
            timeout=self.settings.request_timeout,
            max_retries=self.settings.max_retries,
        )
        self.rpc = rpc or SolanaRpc(
            self.settings.rpc_url,
            HttpClient(
                rate_limit=self.settings.rpc_rate_limit,
                timeout=self.settings.request_timeout,
                max_retries=self.settings.max_retries,
            ),
        )
        self.rugcheck = rugcheck or RugCheck(http)
        self.jupiter = jupiter or Jupiter(http)
        self.dexscreener = dexscreener or DexScreener(http)
        # Vorgeparste Transaktionen - nur mit Helius-Schluessel verfuegbar.
        self.helius = helius or Helius(
            api_key=Helius().api_key or _key_from(self.settings.rpc_url), http=http
        )

    def collect(
        self,
        mint: str,
        candidate: TokenCandidate | None = None,
        test_trade: bool = True,
        trade_pattern: bool = True,
    ) -> TokenData:
        """Holt alle Rohdaten. Einzelne Ausfaelle werden vermerkt, nicht geworfen."""
        data = TokenData(mint=mint, candidate=candidate)

        # 0. Ohne Kandidat aus der Discovery (z.B. bei "xeno check <mint>")
        #    die Marktdaten nachladen. Nicht nur fuer die Anzeige: die
        #    Bewertung einer fehlenden Verkaufsroute haengt am Pool-Alter.
        if data.candidate is None:
            try:
                pair = self.dexscreener.best_pair(mint)
                if pair:
                    data.candidate = self.dexscreener.as_candidate(pair)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"DexScreener fehlgeschlagen: {exc}")

        # 1. Mint-Account - funktioniert auch auf dem oeffentlichen RPC.
        try:
            account = self.rpc.get_account_info(mint)
            data.mint_info = parse_mint_account(mint, account)
            if data.mint_info is None:
                data.errors.append("Adresse ist kein gueltiger Token-Mint")
        except Exception as exc:  # noqa: BLE001
            data.errors.append(f"RPC getAccountInfo fehlgeschlagen: {exc}")

        # 2. RugCheck - Holder, LP, Creator, Insider in einem Request.
        try:
            data.rugcheck = self.rugcheck.report(mint)
        except Exception as exc:  # noqa: BLE001
            data.errors.append(f"RugCheck fehlgeschlagen: {exc}")

        # 3. Holder-Verteilung: RPC bevorzugt, sonst RugCheck.
        pool_addresses = data.rc.pool_addresses
        supply = data.mint_info.supply if data.mint_info else 0.0

        if supply > 0:
            distribution = fetch_distribution_via_rpc(
                self.rpc, mint, supply, extra_pool_addresses=pool_addresses
            )
            if distribution is not None:
                distribution.holder_count = data.rc.total_holders
                data.distribution = distribution
                data.holder_source = "rpc"

        if data.distribution is None and data.rc.top_holders:
            data.distribution = distribution_from_rugcheck(
                data.rc.top_holders,
                supply,
                pool_addresses=pool_addresses,
                holder_count=data.rc.total_holders,
            )
            data.holder_source = "rugcheck"

        if data.distribution is None:
            data.errors.append(
                "Keine Holder-Daten (oeffentlicher RPC sperrt getTokenLargestAccounts; "
                "HELIUS_API_KEY setzen)"
            )

        # 3b. Einzelne Handelsvorgaenge fuer die Musteranalyse. Eine
        #     Anfrage fuer bis zu hundert Trades - ohne die liesse sich
        #     maschineller Handel nur an zusammengefassten Zahlen ablesen.
        if trade_pattern and self.helius.available:
            try:
                data.trades = self.helius.recent_trades(mint)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"Transaktionen nicht abrufbar: {exc}")

        # 4. Simulierter Kauf-Verkauf-Test.
        if test_trade:
            try:
                data.round_trip = self.jupiter.round_trip(mint)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"Jupiter-Test fehlgeschlagen: {exc}")

        return data

    def analyze(
        self,
        mint: str,
        candidate: TokenCandidate | None = None,
        test_trade: bool = True,
        trade_pattern: bool = True,
    ) -> RiskReport:
        data = self.collect(
            mint,
            candidate=candidate,
            test_trade=test_trade,
            trade_pattern=trade_pattern,
        )
        return build_report(data, self.settings)


def build_report(data: TokenData, settings: Settings) -> RiskReport:
    """Wendet die Pruefungen auf bereits eingesammelte Daten an.

    Getrennt vom Einsammeln, damit Tests einen fertigen ``TokenData``
    hineingeben koennen, ohne ans Netz zu gehen.
    """
    rc = data.rc
    symbol = (
        (data.candidate.symbol if data.candidate else "")
        or rc.symbol
        or (data.mint_info.symbol if data.mint_info else "")
    )
    name = (
        (data.candidate.name if data.candidate else "")
        or rc.name
        or (data.mint_info.name if data.mint_info else "")
    )

    report = RiskReport(
        mint=data.mint,
        symbol=symbol,
        name=name,
        candidate=data.candidate,
        mint_info=data.mint_info,
        distribution=data.distribution,
        errors=list(data.errors),
    )
    report.findings = run_checks(data, settings.risk)
    return report
