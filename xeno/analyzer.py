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
from .sources import DexScreener, GeckoTerminal, RugCheck
from .sources.helius import Helius
from .sources.jupiter import Jupiter
from .structure import analyse as analyse_structure

#: So viele Wallets werden auf ihre Geldherkunft geprueft. Jede kostet eine
#: Anfrage, deshalb die Grenze - und deshalb nur die groessten: bei einem
#: Buendel sitzt die Supply oben, nicht im langen Schwanz.
FUNDING_BUDGET = 10

#: Kerzenaufloesung fuer die Strukturanalyse. Fuenf Minuten ist der
#: Kompromiss, der bei einem zwei Stunden alten Token noch zwei Dutzend
#: Kerzen ergibt und bei einem zwei Tage alten nicht im Rauschen untergeht.
CANDLE_TIMEFRAME = "minute"
CANDLE_AGGREGATE = 5
CANDLE_LIMIT = 120


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
        gecko: GeckoTerminal | None = None,
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
        self.gecko = gecko or GeckoTerminal()
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
        read_structure: bool = True,
        check_funding: bool = True,
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

        # 3c. Herkunft der Gelder der groessten Halter. Der einzige Weg, ein
        #     Buendel nachzuweisen statt zu vermuten - kostet aber eine
        #     Anfrage je Wallet, deshalb nur die groessten und gedeckelt.
        if check_funding and self.helius.available:
            wallets = self._top_wallets(data)
            if wallets:
                try:
                    data.origins = self.helius.origins(wallets, budget=FUNDING_BUDGET)
                except Exception as exc:  # noqa: BLE001
                    data.errors.append(f"Wallet-Herkunft nicht abrufbar: {exc}")

        # 3d. Kursverlauf fuer die Richtungsanalyse. Eine Anfrage, und die
        #     einzige Datenquelle, die etwas ueber die Richtung sagt - alle
        #     anderen Pruefungen bewerten nur Sicherheit.
        pool = data.candidate.pool_address if data.candidate else ""
        if read_structure and pool:
            try:
                candles = self.gecko.candles(
                    pool,
                    timeframe=CANDLE_TIMEFRAME,
                    aggregate=CANDLE_AGGREGATE,
                    limit=CANDLE_LIMIT,
                )
                data.structure = analyse_structure(candles)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"Kursverlauf nicht abrufbar: {exc}")

        # 4. Simulierter Kauf-Verkauf-Test.
        if test_trade:
            try:
                data.round_trip = self.jupiter.round_trip(mint)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"Jupiter-Test fehlgeschlagen: {exc}")

        return data

    @staticmethod
    def _top_wallets(data: TokenData) -> list[str]:
        """Die groessten echten Halter - ohne Pools, Locker und Boersen.

        Die Auswahl ist entscheidend: waeren Pool-Adressen dabei, teilten
        sich mehrere "Halter" trivialerweise dieselbe Herkunft, und jeder
        Token saehe gebuendelt aus.
        """
        if data.distribution is None:
            return []
        wallets: list[str] = []
        for holder in data.distribution.holders:
            if holder.is_excluded or not holder.owner:
                continue
            if holder.owner not in wallets:
                wallets.append(holder.owner)
        return wallets

    def analyze(
        self,
        mint: str,
        candidate: TokenCandidate | None = None,
        test_trade: bool = True,
        trade_pattern: bool = True,
        read_structure: bool = True,
        check_funding: bool = True,
    ) -> RiskReport:
        data = self.collect(
            mint,
            candidate=candidate,
            test_trade=test_trade,
            trade_pattern=trade_pattern,
            read_structure=read_structure,
            check_funding=check_funding,
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
        structure=data.structure,
        errors=list(data.errors),
    )
    report.findings = run_checks(data, settings.risk)
    return report
