"""Tests fuer die Musteranalyse auf Transaktionsebene.

Der Wert dieser Ebene zeigte sich beim Ausprobieren an einem Token, den die
Marktdaten fuer unauffaellig hielten: 164 Trades von 90 verschiedenen
Wallets - aber ein Viertel davon mit exakt demselben Betrag. Ein Buendel
Wallets, das dasselbe Skript fuhr. Auf zusammengefassten Zahlen ist das
unsichtbar, weil jede Wallet nur einmal handelt.
"""

from __future__ import annotations

from conftest import MINT, make_candidate

from xeno.checks.base import TokenData
from xeno.checks.tradepattern import MIN_TRADES, check_trade_pattern, uniformity
from xeno.config import RiskThresholds
from xeno.models import Severity
from xeno.sources.helius import Trade, api_key_from, extract_trades

RISK = RiskThresholds()


def trades(sizes: list[float], wallets: list[str] | None = None) -> list[Trade]:
    wallets = wallets or [f"wallet{i}" for i in range(len(sizes))]
    return [
        Trade(wallet=w, lamports=int(s * 1e9)) for s, w in zip(sizes, wallets)
    ]


def run(trade_list) -> list:
    data = TokenData(mint=MINT, candidate=make_candidate(), trades=trade_list)
    return check_trade_pattern(data, RISK)


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def worst(findings) -> Severity:
    order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    return max((f.severity for f in findings), key=order.index, default=Severity.INFO)


class TestUniformity:
    def test_all_identical(self):
        top, top3, amount = uniformity(trades([0.5] * 10))
        assert top == 100.0
        assert amount == 0.5

    def test_all_different(self):
        top, _, _ = uniformity(trades([0.1, 0.2, 0.3, 0.4, 0.5]))
        assert top == 20.0

    def test_empty_input(self):
        assert uniformity([]) == (0.0, 0.0, 0.0)

    def test_top_three_covers_more(self):
        sizes = [0.1] * 4 + [0.2] * 3 + [0.3] * 2 + [0.9]
        top, top3, _ = uniformity(trades(sizes))
        assert top == 40.0
        assert top3 == 90.0

    def test_tiny_differences_are_treated_as_equal(self):
        """Gebuehren verwackeln die letzten Stellen - sonst zaehlte jeder
        Bot-Trade als eigener Betrag."""
        top, _, _ = uniformity(trades([0.50001, 0.500012, 0.500009]))
        assert top == 100.0


class TestPatternCheck:
    def _sizes(self, identical: int, varied: int) -> list[float]:
        return [0.05] * identical + [0.01 * (i + 3) for i in range(varied)]

    def test_human_looking_trades_pass(self):
        result = run(trades([0.01 * (i + 1) for i in range(40)]))
        assert "trade_sizes_look_human" in codes(result)
        assert worst(result) is Severity.INFO

    def test_mostly_identical_is_flagged_high(self):
        result = run(trades(self._sizes(identical=25, varied=15)))
        assert "uniform_trade_sizes" in codes(result)
        assert worst(result) is Severity.HIGH

    def test_the_case_that_market_data_misses(self):
        """164 Trades, 90 Wallets - aber ein Viertel mit gleichem Betrag.
        Jede Wallet handelt nur einmal, deshalb faellt es oben nicht auf."""
        sizes = [0.0021] * 26 + [0.001 * (i + 1) for i in range(74)]
        wallets = [f"w{i}" for i in range(100)]
        result = run(trades(sizes, wallets))
        assert "uniform_trade_sizes" in codes(result)
        assert worst(result) is Severity.MEDIUM
        # Der Wallet-Zaehler allein wuerde hier nichts melden.
        assert "few_wallets_many_trades" not in codes(result)

    def test_slightly_elevated_is_only_a_hint(self):
        result = run(trades(self._sizes(identical=7, varied=33)))
        assert worst(result) is Severity.LOW

    def test_normal_range_is_not_flagged(self):
        """Beobachtet wurden 2 bis 10 Prozent bei unauffaelligen Token."""
        result = run(trades(self._sizes(identical=4, varied=46)))
        assert "uniform_trade_sizes" not in codes(result)

    def test_few_wallets_many_trades_is_reported(self):
        result = run(trades([0.01 * (i + 1) for i in range(40)], ["a", "b", "c", "d"] * 10))
        assert "few_wallets_many_trades" in codes(result)


class TestDataAvailability:
    def test_not_fetched_yields_nothing(self):
        """None heisst 'nicht abgerufen' - das ist kein Befund."""
        data = TokenData(mint=MINT, trades=None)
        assert check_trade_pattern(data, RISK) == []

    def test_too_few_trades_is_not_an_all_clear(self):
        result = run(trades([0.5] * (MIN_TRADES - 1)))
        assert "too_few_trades_for_pattern" in codes(result)
        assert "trade_sizes_look_human" not in codes(result)

    def test_empty_list_is_handled(self):
        result = run([])
        assert "too_few_trades_for_pattern" in codes(result)


class TestTradeExtraction:
    def _tx(self, payer: str, transfers: list[tuple[str, str, int]]) -> dict:
        return {
            "feePayer": payer,
            "timestamp": 1700000000,
            "nativeTransfers": [
                {"fromUserAccount": f, "toUserAccount": t, "amount": a}
                for f, t, a in transfers
            ],
        }

    def test_reads_the_payers_outflow(self):
        tx = self._tx("kaeufer", [("kaeufer", "pool", 500_000_000)])
        result = extract_trades([tx])
        assert len(result) == 1
        assert result[0].wallet == "kaeufer"
        assert result[0].sol == 0.5

    def test_reads_the_payers_inflow_as_size(self):
        """Verkaeufe zaehlen genauso - fuer das Muster zaehlt die Groesse,
        nicht die Richtung."""
        tx = self._tx("verkaeufer", [("pool", "verkaeufer", 300_000_000)])
        assert extract_trades([tx])[0].sol == 0.3

    def test_dust_is_ignored(self):
        tx = self._tx("jemand", [("jemand", "pool", 5_000)])
        assert extract_trades([tx]) == []

    def test_transaction_without_payer_is_skipped(self):
        assert extract_trades([{"nativeTransfers": []}]) == []

    def test_several_transfers_are_summed(self):
        tx = self._tx(
            "kaeufer",
            [("kaeufer", "pool", 400_000_000), ("kaeufer", "gebuehr", 100_000_000)],
        )
        assert extract_trades([tx])[0].sol == 0.5


class TestApiKey:
    def test_environment_wins(self, monkeypatch):
        monkeypatch.setenv("HELIUS_API_KEY", "aus-umgebung")
        assert api_key_from("https://x/?api-key=aus-url") == "aus-umgebung"

    def test_falls_back_to_the_rpc_url(self, monkeypatch):
        monkeypatch.delenv("HELIUS_API_KEY", raising=False)
        assert (
            api_key_from("https://mainnet.helius-rpc.com/?api-key=abc123") == "abc123"
        )

    def test_other_providers_yield_nothing(self, monkeypatch):
        monkeypatch.delenv("HELIUS_API_KEY", raising=False)
        assert api_key_from("https://api.mainnet-beta.solana.com") == ""
