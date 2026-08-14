"""Tests der gestuften Tiefpruefung.

Mehr als die Haelfte der Pruefungen kostet nichts: RugCheck, Jupiter,
GeckoTerminal und DexScreener sind fremde Dienste ohne Kontingent. Nur die
Chain-Abfragen werden abgerechnet - und die teuerste kostet das Hundertfache
einer gewoehnlichen.

Bisher hing alles zusammen: ein Token bekam die komplette Tiefpruefung oder
gar keine. Ein Token mit lebender Mint-Authority - also erledigt, egal was
sonst noch kommt - kostete deshalb genauso viel wie ein aussichtsreicher.

Geprueft wird hier, dass die Reihenfolge haelt: erst das Kostenlose, dann
ein billiger Aufruf, und die teuren nur fuer die, die noch im Rennen sind.
"""

from __future__ import annotations

from conftest import MINT, make_candidate, make_mint_info

from xeno.analyzer import TokenAnalyzer
from xeno.checks.base import TokenData
from xeno.config import Settings
from xeno.credits import RPC, CreditMeter
from xeno.known import TOKEN_PROGRAM
from xeno.sources.helius import Origin, Trade
from xeno.sources.rugcheck import RugCheckReport


class FakeRpc:
    """Zaehlt, welche Methoden aufgerufen wurden."""

    LARGE_METHODS = frozenset({"getTokenLargestAccounts"})

    def __init__(self, mint_authority: str | None = None) -> None:
        self.mint_authority = mint_authority
        self.requests = 0
        self.large_requests = 0
        self.calls: list[str] = []

    def get_account_info(self, mint):
        self.calls.append("getAccountInfo")
        self.requests += 1
        return {"value": {"data": ["", "base64"], "owner": TOKEN_PROGRAM}}

    def get_token_largest_accounts(self, mint):
        self.calls.append("getTokenLargestAccounts")
        self.large_requests += 1
        return []

    def get_multiple_accounts(self, addresses):
        self.calls.append("getMultipleAccounts")
        self.requests += 1
        return []


class FakeHelius:
    def __init__(self) -> None:
        self.available = True
        self.requests = 0
        self.calls: list[str] = []

    def recent_trades(self, mint, limit=100):
        self.calls.append("recent_trades")
        self.requests += 1
        return [Trade(wallet="w", lamports=100_000_000)]

    def origins(self, wallets, budget=10, cache=None):
        self.calls.append(f"origins:{budget}")
        self.requests += len(wallets[:budget])
        return [Origin(wallet=w, funder="q", tx_count=5) for w in wallets[:budget]]


class FakeSource:
    """Kostenlose Quellen - liefern etwas Brauchbares und zaehlen mit.

    Bewusst ohne Attribute, die so heissen wie die Methoden: ein
    ``self.report = ...`` im Konstruktor wuerde die Methode ``report``
    ueberschreiben, und der Test pruefte anschliessend etwas anderes als er
    behauptet.
    """

    def __init__(self) -> None:
        self.calls = 0

    # RugCheck
    def report(self, mint):
        self.calls += 1
        return RugCheckReport(mint=mint, raw={})

    # Jupiter
    def round_trip(self, mint):
        self.calls += 1
        return None

    # GeckoTerminal
    def candles(self, pool, **kwargs):
        self.calls += 1
        return []

    # DexScreener
    def best_pair(self, mint):
        self.calls += 1
        return None


def build(monkeypatch, tmp_path, mint_info, *, cap=1_000_000, reaches_ok=True):
    """Baut einen Analyzer, dessen Quellen alle Doppel sind.

    ``reaches_ok`` setzt die Schwellenpruefung vor Stufe 4 ausser Kraft.
    Die Tests hier pruefen Stufung und Budget; ob ein Token die OK-Schwelle
    erreicht, haengt an einem Dutzend Einzelpruefungen und gehoert in
    eigene Tests (siehe ``TestSchwellenfilter``). Ohne diesen Schalter
    haetten sie sich gegenseitig im Weg gestanden: der Doppel-RPC liefert
    keine Holder-Daten, damit kommt jeder Testtoken auf 35 Punkte, und
    Stufe 4 liefe nie.
    """
    analyzer = TokenAnalyzer.__new__(TokenAnalyzer)
    analyzer.settings = Settings()
    analyzer.rpc = FakeRpc()
    analyzer.rugcheck = FakeSource()
    analyzer.jupiter = FakeSource()
    analyzer.dexscreener = FakeSource()
    analyzer.gecko = FakeSource()
    analyzer.helius = FakeHelius()
    analyzer.origin_cache = None
    analyzer.meter = CreditMeter(
        "https://mainnet.helius-rpc.com/?api-key=x",
        path=tmp_path / "credits.json",
        monthly_cap=cap,
    )
    monkeypatch.setattr(
        "xeno.analyzer.parse_mint_account", lambda mint, account: mint_info
    )
    if reaches_ok:
        analyzer._can_still_reach_ok = lambda data: True
    return analyzer


class TestEarlyExit:
    def test_a_critical_finding_stops_the_expensive_part(self, monkeypatch, tmp_path):
        """Lebende Mint-Authority: der Token ist erledigt. Ihm noch Holder-
        und Transaktionsabfragen hinterherzuwerfen waere Verschwendung."""
        deadly = make_mint_info(mint_authority="Der-Ersteller-Kann-Nachdrucken")
        analyzer = build(monkeypatch, tmp_path, deadly)

        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))

        assert analyzer.rpc.calls == ["getAccountInfo"]
        assert analyzer.helius.calls == []

    def test_a_clean_token_gets_the_full_treatment(self, monkeypatch, tmp_path):
        analyzer = build(monkeypatch, tmp_path, make_mint_info())

        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))

        assert "getTokenLargestAccounts" in analyzer.rpc.calls
        assert "recent_trades" in analyzer.helius.calls

    def test_the_free_sources_run_either_way(self, monkeypatch, tmp_path):
        """Sie kosten nichts, also gibt es keinen Grund, sie auszulassen -
        und sie liefern gerade die Befunde, die frueh aussortieren."""
        deadly = make_mint_info(mint_authority="lebt")
        analyzer = build(monkeypatch, tmp_path, deadly)

        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))

        assert analyzer.rugcheck.calls == 1
        assert analyzer.jupiter.calls == 1
        assert analyzer.gecko.calls == 1


class TestBudgetEnforcement:
    def test_an_exhausted_budget_skips_the_expensive_calls(self, monkeypatch, tmp_path):
        analyzer = build(monkeypatch, tmp_path, make_mint_info(), cap=1_000_000)
        # Tagesbudget bis auf einen Rest aufbrauchen, der gerade noch fuer
        # den billigen Mint-Abruf reicht.
        analyzer.meter.spend(RPC, analyzer.meter.daily_allowance - 5)

        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))

        assert "getAccountInfo" in analyzer.rpc.calls
        assert "recent_trades" not in analyzer.helius.calls
        assert analyzer.meter.denied > 0

    def test_a_denial_is_a_gap_not_an_all_clear(self, monkeypatch, tmp_path):
        """Der entscheidende Punkt: ausgelassen heisst unbekannt, und
        unbekannt darf nie wie geprueft aussehen."""
        analyzer = build(monkeypatch, tmp_path, make_mint_info())
        analyzer.meter.spend(RPC, analyzer.meter.daily_allowance - 5)

        data = analyzer.collect(MINT, candidate=make_candidate())

        assert data.trades is None
        assert data.origins is None

    def test_the_funding_budget_shrinks_with_the_purse(self, monkeypatch, tmp_path):
        analyzer = build(monkeypatch, tmp_path, make_mint_info())
        # Genug fuer den Mint-Abruf, die Verteilung und zwei teure Aufrufe.
        analyzer.meter.spend(RPC, analyzer.meter.daily_allowance - 250)

        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))

        gestellt = [c for c in analyzer.helius.calls if c.startswith("origins:")]
        if gestellt:
            budget = int(gestellt[0].split(":")[1])
            assert budget <= 2

    def test_nothing_is_capped_without_a_quota(self, monkeypatch, tmp_path):
        analyzer = build(monkeypatch, tmp_path, make_mint_info())
        analyzer.meter = CreditMeter(
            "https://api.mainnet-beta.solana.com", path=tmp_path / "c.json"
        )
        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))
        assert "recent_trades" in analyzer.helius.calls


class TestAccounting:
    def test_spending_is_booked(self, monkeypatch, tmp_path):
        analyzer = build(monkeypatch, tmp_path, make_mint_info())
        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))
        assert analyzer.meter.day_spent > 0

    def test_a_disqualified_token_costs_almost_nothing(self, monkeypatch, tmp_path):
        """Der eigentliche Gewinn der Stufung, in einer Zahl."""
        clean = build(monkeypatch, tmp_path / "a", make_mint_info())
        (tmp_path / "a").mkdir()
        clean.collect(MINT, candidate=make_candidate())

        (tmp_path / "b").mkdir()
        deadly = build(monkeypatch, tmp_path / "b", make_mint_info(mint_authority="lebt"))
        deadly.collect(MINT, candidate=make_candidate())

        assert deadly.meter.day_spent < clean.meter.day_spent / 10


def test_disqualified_needs_a_critical_finding():
    """Ein schlechter Punktestand allein reicht nicht - der kann auch daher
    ruehren, dass noch Daten fehlen."""
    analyzer = TokenAnalyzer.__new__(TokenAnalyzer)
    analyzer.settings = Settings()

    leer = TokenData(mint=MINT, candidate=make_candidate())
    assert analyzer._already_disqualified(leer) is False


class TestFehlschlagMarkieren:
    """Der Unterschied zwischen "nicht nachgesehen" und "nachgesehen, nichts".

    Beides endet im Urteil UNKNOWN, aber nur das eine ist ein Ergebnis.
    """

    def test_an_empty_budget_marks_the_lookup_as_failed(self):
        from xeno.credits import CreditMeter

        meter = CreditMeter(rpc_url="https://x.quiknode.pro/k/", monthly_cap=1)
        analyzer = TokenAnalyzer(Settings(), rpc=_NoRpc(), meter=meter)
        data = analyzer.collect(MINT, candidate=make_candidate())
        assert data.lookup_failed
        assert any("Tagesbudget" in e for e in data.errors)

    def test_a_broken_rpc_marks_it_too(self):
        analyzer = TokenAnalyzer(Settings(), rpc=_BrokenRpc())
        data = analyzer.collect(MINT, candidate=make_candidate())
        assert data.lookup_failed

    def test_a_non_mint_address_is_a_result(self):
        """Hier wurde nachgesehen - das Ergebnis ist nur unerfreulich."""
        analyzer = TokenAnalyzer(Settings(), rpc=_EmptyRpc())
        data = analyzer.collect(MINT, candidate=make_candidate())
        assert not data.lookup_failed
        assert any("kein gueltiger Token-Mint" in e for e in data.errors)


class _NoRpc:
    requests = 0
    large_requests = 0

    def get_account_info(self, mint):
        raise AssertionError("darf bei leerem Budget nicht gefragt werden")


class _BrokenRpc(_NoRpc):
    def get_account_info(self, mint):
        raise RuntimeError("Verbindung abgebrochen")


class _EmptyRpc(_NoRpc):
    def get_account_info(self, mint):
        return None


class TestSchwellenfilter:
    """Die teuerste Abfrage nur dort, wo sie das Urteil noch drehen kann.

    Die Idee steht und faellt mit einer Eigenschaft: die Zwischenpunktzahl
    vor Stufe 4 ist eine **Obergrenze**. Beide Pruefungen dieser Stufe
    melden bei fehlenden Daten ausdruecklich nichts, und was sie bei
    vorhandenen Daten melden, zieht nur ab. Wer jetzt unter der Schwelle
    steht, steht es auch danach.

    Der erste Test sichert genau diese Annahme. Kippt sie irgendwann - etwa
    weil eine Pruefung anfaengt, Punkte zu vergeben - wuerde die Filterung
    still Token aussortieren, die OK geworden waeren.
    """

    def analyzer(self):
        a = TokenAnalyzer.__new__(TokenAnalyzer)
        a.settings = Settings()
        return a

    def test_the_expensive_checks_only_ever_subtract(self):
        """Die Annahme, auf der alles steht."""
        from xeno.checks.funding import check_funding
        from xeno.checks.tradepattern import check_trade_pattern

        leer = TokenData(mint=MINT, candidate=make_candidate(), mint_info=make_mint_info())
        assert check_trade_pattern(leer, Settings().risk) == []
        assert check_funding(leer, Settings().risk) == []

        mit_daten = TokenData(
            mint=MINT,
            candidate=make_candidate(),
            mint_info=make_mint_info(),
            trades=[],
            origins=[],
        )
        for pruefung in (check_trade_pattern, check_funding):
            assert all(f.penalty >= 0 for f in pruefung(mit_daten, Settings().risk))

    def test_a_token_below_the_threshold_is_skipped(self):
        """35 Punkte - kein Musterbefund der Welt macht daraus OK."""
        data = TokenData(mint=MINT, candidate=make_candidate(), mint_info=make_mint_info())
        assert self.analyzer()._can_still_reach_ok(data) is False

    def test_a_clean_token_passes(self):
        from conftest import make_distribution, rugcheck

        data = TokenData(
            mint=MINT,
            candidate=make_candidate(socials={"twitter": "https://x.com/x"}),
            mint_info=make_mint_info(),
            distribution=make_distribution([4.0, 3.0, 2.0]),
            holder_source="rpc",
            rugcheck=rugcheck(markets=[{"lp": {"lpLockedPct": 100.0}}]),
        )
        assert self.analyzer()._can_still_reach_ok(data) is True

    def test_the_threshold_is_the_same_one_the_verdict_uses(self):
        """Zwei getrennte 80er waeren ein Fehler, der erst auffiele, wenn
        jemand einen davon verschiebt."""
        from xeno.models import OK_SCORE, Finding, RiskReport, Severity

        report = RiskReport(mint=MINT, mint_info=make_mint_info())
        report.findings = [
            Finding(check="t", code="x", severity=Severity.MEDIUM, message="m")
        ]
        assert report.score == 100 - 15
        assert (report.score >= OK_SCORE) == (report.verdict.value == "OK")

    def test_it_saves_the_expensive_calls(self, monkeypatch, tmp_path):
        """Der Gewinn, in einer Zahl: ein Token unter der Schwelle kostet
        keinen einzigen Enhanced-Aufruf mehr."""
        analyzer = build(monkeypatch, tmp_path, make_mint_info(), reaches_ok=False)
        analyzer.collect(MINT, candidate=make_candidate(pool_address="pool1"))
        assert analyzer.helius.calls == []
