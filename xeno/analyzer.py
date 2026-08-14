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

#: So viele der groessten Halter kommen ueberhaupt in Frage. Bei einem
#: Buendel sitzt die Supply oben, nicht im langen Schwanz - der Rest der
#: Liste sagt ueber Kontrolle nichts.
FUNDING_EXAMINE = 12

#: So viele davon duerfen je Pruefung **neu** abgefragt werden. Bekannte
#: Wallets kosten nichts und zaehlen nicht mit.
#:
#: Der Wert ist bewusst kleiner als FUNDING_EXAMINE. Bei einem jungen Token
#: wechseln die groessten Halter im Minutentakt; wuerde jede Pruefung alle
#: unbekannten nachladen, waere das Gedaechtnis wirkungslos. So waechst die
#: Abdeckung ueber mehrere Pruefungen hinweg, und der Verbrauch bleibt
#: gedeckelt - drei von fuenf genuegen ohnehin, um ein Buendel zu erkennen.
FUNDING_BUDGET = 5

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
        origin_cache=None,
        meter=None,
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
        # Gedaechtnis fuer die Wallet-Herkunft. Ohne das wuerde bei jeder
        # Wiederholungspruefung dieselbe unveraenderliche Tatsache erneut
        # abgefragt - der teuerste Posten im ganzen Deep-Check.
        if origin_cache is None:
            from .origincache import OriginCache

            origin_cache = OriginCache()
        self.origin_cache = origin_cache

        # Verbrauchszaehler. Ohne ihn konnte XENO ein Monatskontingent in
        # einer Nacht ausgeben, ohne dass das irgendwo sichtbar war.
        if meter is None:
            from .credits import CreditMeter

            meter = CreditMeter(self.settings.rpc_url)
        self.meter = meter

    def collect(
        self,
        mint: str,
        candidate: TokenCandidate | None = None,
        test_trade: bool = True,
        trade_pattern: bool = True,
        read_structure: bool = True,
        check_funding: bool = True,
    ) -> TokenData:
        """Holt alle Rohdaten. Einzelne Ausfaelle werden vermerkt, nicht geworfen.

        Die Reihenfolge ist nach Kosten sortiert, nicht nach Wichtigkeit. Der
        Grund: mehr als die Haelfte der Pruefungen kostet nichts. RugCheck,
        Jupiter, GeckoTerminal und DexScreener sind fremde Dienste ohne
        Kontingent - nur die Chain-Abfragen werden abgerechnet.

        Fruehen ein Token bereits an einer kostenlosen Pruefung durch, waere
        es Verschwendung, ihm anschliessend noch teure hinterherzuwerfen. Ein
        Token mit lebender Mint-Authority oder ohne Verkaufsroute ist erledigt,
        egal wie seine Halter verteilt sind.
        """
        from .credits import ENHANCED, RPC, RPC_LARGE

        data = TokenData(mint=mint, candidate=candidate)

        # ---- Stufe 1: kostenlose Quellen -------------------------------

        # Ohne Kandidat aus der Discovery (z.B. bei "xeno check <mint>") die
        # Marktdaten nachladen. Nicht nur fuer die Anzeige: die Bewertung
        # einer fehlenden Verkaufsroute haengt am Pool-Alter.
        if data.candidate is None:
            try:
                pair = self.dexscreener.best_pair(mint)
                if pair:
                    data.candidate = self.dexscreener.as_candidate(pair)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"DexScreener fehlgeschlagen: {exc}")

        # RugCheck - Holder, LP, Creator, Insider in einem Request.
        try:
            data.rugcheck = self.rugcheck.report(mint)
        except Exception as exc:  # noqa: BLE001
            data.errors.append(f"RugCheck fehlgeschlagen: {exc}")

        # Kursverlauf - die einzige Quelle, die etwas ueber die Richtung sagt.
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

        # Simulierter Kauf-Verkauf-Test - der eigentliche Honeypot-Test.
        if test_trade:
            try:
                data.round_trip = self.jupiter.round_trip(mint)
            except Exception as exc:  # noqa: BLE001
                data.errors.append(f"Jupiter-Test fehlgeschlagen: {exc}")

        # ---- Stufe 2: ein billiger Chain-Aufruf ------------------------

        # Der Mint-Account traegt Mint- und Freeze-Authority. Ein einzelner
        # Aufruf, und er entscheidet ueber die schwerwiegendsten Befunde
        # ueberhaupt - deshalb laeuft er, solange das Budget irgendetwas
        # hergibt.
        if self.meter.can_afford(RPC):
            try:
                account = self.rpc.get_account_info(mint)
                self.meter.spend(RPC)
                data.mint_info = parse_mint_account(mint, account)
                if data.mint_info is None:
                    # Ein Ergebnis, kein Fehlschlag: die Adresse ist einfach
                    # kein Token-Mint. Das darf und soll gespeichert werden.
                    data.errors.append("Adresse ist kein gueltiger Token-Mint")
            except Exception as exc:  # noqa: BLE001
                data.lookup_failed = True
                data.errors.append(f"RPC getAccountInfo fehlgeschlagen: {exc}")
        else:
            self.meter.deny()
            data.lookup_failed = True
            data.errors.append("Tagesbudget aufgebraucht - Mint-Account nicht geprueft")

        # ---- Abbruchpunkt ----------------------------------------------

        # Steht bereits ein schwerwiegender Befund fest, ist der Token
        # erledigt. Alles Weitere kostet Credits und aendert am Urteil nichts.
        if self._already_disqualified(data):
            return data

        # ---- Stufe 3: teurere Chain-Abfragen ---------------------------

        pool_addresses = data.rc.pool_addresses
        supply = data.mint_info.supply if data.mint_info else 0.0

        if supply > 0 and self.meter.can_afford(RPC_LARGE) and self.meter.can_afford(RPC):
            before_large = self.rpc.large_requests
            before_plain = self.rpc.requests
            distribution = fetch_distribution_via_rpc(
                self.rpc, mint, supply, extra_pool_addresses=pool_addresses
            )
            self.meter.spend(RPC_LARGE, self.rpc.large_requests - before_large)
            self.meter.spend(RPC, self.rpc.requests - before_plain)
            if distribution is not None:
                distribution.holder_count = data.rc.total_holders
                data.distribution = distribution
                data.holder_source = "rpc"

        # RugCheck als Rueckfall - kostet nichts und traegt die Analyse
        # weiter, wenn die eigene Chain-Abfrage nicht drin war.
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
                "eigenen RPC-Zugang eintragen)"
            )

        # ---- Stufe 4: die teuersten Abfragen ---------------------------

        # Nur noch fuer Token, bei denen sie das Urteil drehen koennen.
        #
        # Die Abfragen dieser Stufe kosten das Hundertfache eines
        # gewoehnlichen Aufrufs, und ihre Pruefungen ziehen ausschliesslich
        # ab - sie geben nie Punkte. Wer aus den billigen Pruefungen schon
        # unter der OK-Schwelle liegt, kann durch sie also nicht besser
        # werden: die Antwort auf "durchlassen oder nicht" steht fest, und
        # hundert Credits aendern daran nichts mehr.
        #
        # In einer echten Nacht waren 87 von rund 2400 geprueften Token OK.
        # Die uebrigen 96% bekamen die teuerste Abfrage geschenkt, ohne dass
        # sie irgendetwas entschieden haette.
        if not self._can_still_reach_ok(data):
            self.meter.save()
            return data

        # Geparste Transaktionen kosten das Hundertfache eines gewoehnlichen
        # Aufrufs. Ein Token, der bis hierher gekommen ist, ist das wert -
        # jeder andere nicht.
        if trade_pattern and self.helius.available:
            if self.meter.can_afford(ENHANCED):
                try:
                    data.trades = self.helius.recent_trades(mint)
                    self.meter.spend(ENHANCED)
                except Exception as exc:  # noqa: BLE001
                    data.errors.append(f"Transaktionen nicht abrufbar: {exc}")
            else:
                self.meter.deny()

        if check_funding and self.helius.available:
            wallets = self._top_wallets(data)
            budget = self.meter.affordable(ENHANCED, FUNDING_BUDGET)
            if wallets and budget:
                before = self.helius.requests
                try:
                    data.origins = self.helius.origins(
                        wallets[:FUNDING_EXAMINE],
                        budget=budget,
                        cache=self.origin_cache,
                    )
                    self.origin_cache.save()
                except Exception as exc:  # noqa: BLE001
                    data.errors.append(f"Wallet-Herkunft nicht abrufbar: {exc}")
                finally:
                    self.meter.spend(ENHANCED, self.helius.requests - before)
            elif wallets:
                self.meter.deny()

        self.meter.save()
        return data

    def _already_disqualified(self, data: TokenData) -> bool:
        """Ob schon ein schwerwiegender Befund vorliegt.

        Bewusst nur ``CRITICAL``: das sind Befunde, die den Token unabhaengig
        von allem Weiteren erledigen - lebende Mint-Authority, eingefrorene
        Konten, kein Verkaufsweg. Ein schlechter Punktestand allein reicht
        nicht, denn der kann auch daher ruehren, dass noch Daten fehlen.
        """
        from .checks import run_checks
        from .models import Severity

        findings = run_checks(data, self.settings.risk)
        return any(f.severity is Severity.CRITICAL for f in findings)

    def _can_still_reach_ok(self, data: TokenData) -> bool:
        """Ob die teuren Abfragen das Urteil ueberhaupt noch aendern koennen.

        Der Trick ist, dass die Zwischenpunktzahl eine **Obergrenze** ist:
        beide Pruefungen der teuren Stufe melden bei fehlenden Daten
        ausdruecklich *nichts* ("nicht abgerufen - kein Befund, aber auch
        keine Entwarnung"), und was sie bei vorhandenen Daten melden, zieht
        nur ab. Der Punktestand kann durch Stufe 4 also nie steigen.

        Wer jetzt unter der OK-Schwelle steht, steht es auch danach.

        Der Preis dafuer, ehrlich benannt: Token unterhalb der Schwelle
        bekommen keine Musteranalyse mehr. Fuer die Frage "durchlassen oder
        nicht" aendert das nichts - fuer die Feinunterscheidung zwischen
        CAUTION und RISKY schon. Das ist es wert: die Unterscheidung
        zwischen "gut" und "nicht gut" ist die, an der Geld haengt.
        """
        from .checks import run_checks
        from .models import OK_SCORE

        findings = run_checks(data, self.settings.risk)
        score = max(0, min(100, 100 - sum(f.penalty for f in findings)))
        return score >= OK_SCORE

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
        lookup_failed=data.lookup_failed,
    )
    report.findings = run_checks(data, settings.risk)
    return report
