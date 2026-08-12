"""Tests fuer die Erkennung maschinellen Handels.

Die Schwellen stammen aus einer Messung an 138 Pools mit nennenswertem
Handel: Median 2.7 Trades je Wallet, oberes Viertel ab 6.5, oberste 5% ab 21.
Etwas maschineller Handel steckt in fast jedem Chart - die Tests halten
deshalb ausdruecklich fest, dass normale Werte **nicht** anschlagen.
"""

from __future__ import annotations

from conftest import make_candidate

from xeno.checks.base import TokenData
from xeno.checks.botting import EXTREME_TRADE_RATIO, check_botting
from xeno.config import RiskThresholds, ScreenThresholds
from xeno.models import Severity
from xeno.screen import screen

RISK = RiskThresholds()


def candidate(buys: int, sells: int, buyers: int, sellers: int = 0, **kwargs):
    return make_candidate(
        buys_h1=buys, sells_h1=sells, buyers_h1=buyers, sellers_h1=sellers, **kwargs
    )


def run(cand) -> list:
    return check_botting(TokenData(mint=cand.mint, candidate=cand), RISK)


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def worst(findings) -> Severity:
    order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    return max((f.severity for f in findings), key=order.index, default=Severity.INFO)


class TestTradesPerWallet:
    def test_typical_token_is_not_flagged(self):
        """Median der Messung war 2.7 - das muss unauffaellig bleiben."""
        cand = candidate(buys=40, sells=15, buyers=20, sellers=15)
        assert cand.trades_per_wallet < 3
        assert codes(run(cand)) == {"trading_looks_organic"}

    def test_upper_quartile_still_passes(self):
        """Ein Viertel aller Token liegt ueber 6.5 - das darf kein Alarm sein,
        sonst waere die Haelfte des Marktes verdaechtig."""
        cand = candidate(buys=40, sells=10, buyers=10, sellers=8)
        assert 4 < cand.trades_per_wallet < 6
        assert worst(run(cand)) is Severity.INFO

    def test_elevated_is_only_a_hint(self):
        cand = candidate(buys=60, sells=20, buyers=10, sellers=5)
        assert worst(run(cand)) is Severity.LOW
        assert "machine_trading" in codes(run(cand))

    def test_clearly_machine_driven(self):
        cand = candidate(buys=100, sells=21, buyers=8, sellers=4)
        assert worst(run(cand)) is Severity.MEDIUM

    def test_extreme_case(self):
        """Der Fall aus der Messung: 67 Trades, kaum Wallets."""
        cand = candidate(buys=50, sells=17, buyers=2, sellers=1)
        result = run(cand)
        assert "machine_trading" in codes(result)
        assert worst(result) is Severity.HIGH

    def test_single_wallet_is_unambiguous(self):
        cand = candidate(buys=40, sells=27, buyers=1, sellers=1)
        result = run(cand)
        assert "single_wallet_trading" in codes(result)
        assert worst(result) is Severity.HIGH

    def test_single_wallet_with_few_trades_is_not_enough(self):
        """Drei Trades von einer Wallet sind kein Bot, sondern wenig los."""
        cand = candidate(buys=3, sells=1, buyers=1, sellers=1)
        assert "single_wallet_trading" not in codes(run(cand))


class TestTooLittleActivity:
    def test_no_verdict_without_enough_trades(self):
        cand = candidate(buys=4, sells=2, buyers=1, sellers=1)
        assert cand.trades_per_wallet is None
        assert "activity_too_low_to_judge" in codes(run(cand))

    def test_low_activity_is_not_an_all_clear(self):
        """Wichtig: keine Aussage heisst nicht 'unauffaellig'."""
        cand = candidate(buys=4, sells=2, buyers=1, sellers=1)
        assert "trading_looks_organic" not in codes(run(cand))

    def test_missing_data_yields_nothing(self):
        cand = make_candidate(buys_h1=None, sells_h1=None, buyers_h1=None, sellers_h1=None)
        assert "activity_too_low_to_judge" in codes(run(cand))

    def test_without_a_candidate_nothing_happens(self):
        assert check_botting(TokenData(mint="x"), RISK) == []


class TestWashTrading:
    def test_volume_without_price_movement_is_flagged(self):
        cand = candidate(
            buys=60,
            sells=55,
            buyers=40,
            sellers=38,
            liquidity_usd=5_000.0,
            volume_h1_usd=150_000.0,
            price_change_h1_pct=1.2,
        )
        result = run(cand)
        assert "wash_trading" in codes(result)

    def test_volume_with_price_movement_is_normal(self):
        """Hoher Umsatz ist fuer sich genommen kein Verdacht - erst wenn er
        den Kurs nicht bewegt, dreht sich offenbar dasselbe Geld im Kreis."""
        cand = candidate(
            buys=60,
            sells=55,
            buyers=40,
            sellers=38,
            liquidity_usd=5_000.0,
            volume_h1_usd=150_000.0,
            price_change_h1_pct=180.0,
        )
        assert "wash_trading" not in codes(run(cand))

    def test_modest_volume_is_not_flagged(self):
        cand = candidate(
            buys=30,
            sells=25,
            buyers=25,
            sellers=20,
            liquidity_usd=50_000.0,
            volume_h1_usd=40_000.0,
            price_change_h1_pct=0.5,
        )
        assert "wash_trading" not in codes(run(cand))


class TestScreenIntegration:
    def test_extreme_case_is_filtered_before_the_deep_check(self):
        """Spart das teure Pruefbudget bei offensichtlichem Wash-Trading."""
        cand = candidate(
            buys=60, sells=20, buyers=2, sellers=1, liquidity_usd=20_000.0,
            volume_h1_usd=30_000.0,
        )
        assert cand.trades_per_wallet >= EXTREME_TRADE_RATIO
        result = screen(cand, ScreenThresholds())
        assert not result.passed
        assert any("maschinell" in r for r in result.reasons)

    def test_moderate_botting_still_reaches_the_deep_check(self):
        """Der Vorfilter soll nur die Ausreisser wegnehmen - die Abstufung
        passiert spaeter, mit vollstaendigen Daten."""
        # Bewusst so gewaehlt, dass alle uebrigen Schwellen erfuellt sind -
        # sonst wuerde der Test etwas anderes pruefen als er behauptet.
        cand = candidate(
            buys=200,
            sells=60,
            buyers=30,
            sellers=20,
            liquidity_usd=20_000.0,
            volume_h1_usd=30_000.0,
        )
        ratio = cand.trades_per_wallet
        assert 5 < ratio < EXTREME_TRADE_RATIO
        result = screen(cand, ScreenThresholds())
        assert result.passed, result.reasons

    def test_normal_token_is_unaffected(self):
        cand = candidate(
            buys=80, sells=30, buyers=60, sellers=25,
            liquidity_usd=20_000.0, volume_h1_usd=30_000.0,
        )
        assert screen(cand, ScreenThresholds()).passed


class TestWalletCounting:
    def test_uses_the_larger_side(self):
        """Kaeufer und Verkaeufer ueberschneiden sich - das Maximum ist die
        vorsichtige Schaetzung und laesst Bot-Handel nicht harmloser wirken."""
        cand = candidate(buys=50, sells=50, buyers=5, sellers=30)
        assert cand.wallet_count_h1 == 30

    def test_counts_both_sides_of_the_trade(self):
        cand = candidate(buys=30, sells=20, buyers=10)
        assert cand.trade_count_h1 == 50
