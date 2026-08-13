"""Tests der Marktstrukturanalyse.

Der Kern dieser Schicht ist die Entscheidung, an Kerzen**koerpern** zu
messen statt an Dochten. Bei duenner Liquiditaet reisst ein einzelner Kauf
den Kurs kurz nach oben; auf dem Chart bleibt ein langer Docht. Wer daran
Wendepunkte festmacht, liest die Handlung einer Wallet und haelt sie fuer
eine Marktaussage. Genau dieser Fall wird unten geprueft.
"""

from __future__ import annotations

from conftest import MINT, make_candidate

from xeno.checks.base import TokenData
from xeno.checks.structure import check_structure
from xeno.config import RiskThresholds
from xeno.models import Severity
from xeno.structure import (
    MIN_CANDLES,
    Break,
    Candle,
    Trend,
    analyse,
    classify,
    swing_highs,
    swing_lows,
    thin,
)

RISK = RiskThresholds()


def candles(closes: list[float], wicks: float = 0.0) -> list[Candle]:
    """Baut Kerzen aus Schlusskursen; ``wicks`` haengt Dochte an."""
    out = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        body_high, body_low = max(open_, close), min(open_, close)
        out.append(
            Candle(
                time=1_700_000_000 + i * 300,
                open=open_,
                high=body_high * (1 + wicks),
                low=body_low * (1 - wicks),
                close=close,
            )
        )
    return out


def legs(pivots: list[float], steps: int = 4) -> list[float]:
    """Interpoliert zwischen Wendepunkten.

    Ein Verlauf, der jede Kerze die Richtung wechselt, hat keine Struktur -
    er ist Rauschen. Echte Bewegungen brauchen mehrere Kerzen je Bein, und
    genau darauf ist die Erkennung ausgelegt.
    """
    closes = [pivots[0]]
    for target in pivots[1:]:
        start = closes[-1]
        for k in range(1, steps + 1):
            closes.append(start + (target - start) * k / steps)
    return closes


def rising() -> list[float]:
    """Hoehere Hochs (15, 20) und hoehere Tiefs (12, 16)."""
    return legs([10, 15, 12, 20, 16, 26])


def falling() -> list[float]:
    return list(reversed(rising()))


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def run(structure) -> list:
    data = TokenData(mint=MINT, candidate=make_candidate(), structure=structure)
    return check_structure(data, RISK)


class TestSwings:
    def test_finds_peaks_and_valleys(self):
        data = candles(rising())
        assert len(swing_highs(data)) >= 2
        assert len(swing_lows(data)) >= 2

    def test_a_single_wick_does_not_create_a_swing(self):
        """Der Fall, um den es hier geht: ein einzelner Kauf reisst den Kurs
        kurz hoch. Der Docht darf keinen Wendepunkt erzeugen."""
        flat = candles([10.0] * 15)
        spiked = list(flat)
        spiked[7] = Candle(
            time=spiked[7].time,
            open=10.0,
            high=40.0,  # jemand kauft in einen duennen Pool
            low=10.0,
            close=10.0,
        )
        assert swing_highs(spiked) == swing_highs(flat)

    def test_flat_stretch_yields_no_trend(self):
        result = analyse(candles([10.0] * 30))
        assert result.trend is Trend.SIDEWAYS

    def test_empty_input(self):
        result = analyse([])
        assert result.trend is Trend.UNKNOWN
        assert result.candle_count == 0


class TestTrend:
    def test_uptrend(self):
        assert analyse(candles(rising())).trend is Trend.UP

    def test_downtrend(self):
        assert analyse(candles(falling())).trend is Trend.DOWN

    def test_tiny_moves_are_not_a_trend(self):
        """Ein Zehntelprozent Unterschied ist kein hoeheres Hoch."""
        wobble = [10 + (i % 2) * 0.005 for i in range(30)]
        assert analyse(candles(wobble)).trend is not Trend.UP

    def test_needs_two_of_each_swing(self):
        assert classify([], []) is Trend.UNKNOWN


class TestDrawdown:
    def test_measures_distance_from_peak(self):
        result = analyse(candles([10, 50, 40, 30, 20, 10, 5] + [5] * 10))
        assert result.drawdown_pct is not None
        assert 88 <= result.drawdown_pct <= 92

    def test_at_the_high_there_is_no_drawdown(self):
        result = analyse(candles(list(range(10, 40))))
        assert result.drawdown_pct is not None
        assert result.drawdown_pct < 1.0


class TestBreak:
    def test_close_below_last_low_is_bearish(self):
        """Ein Tief, das halten sollte, haelt nicht."""
        data = candles(rising() + [8.0, 8.0, 8.0])
        assert analyse(data).structure_break is Break.BEARISH

    def test_no_break_inside_the_range(self):
        data = candles(legs([10, 12, 10.5, 12, 10.5, 12], steps=3))
        assert analyse(data).structure_break is Break.NONE


class TestSpark:
    """Der ausgeduennte Verlauf fuer die Anzeige.

    Ein Werkzeug, das Charts bewertet, sollte auch einen zeigen koennen -
    aus 120 Kerzen wird auf einem Handydisplay aber nichts Lesbares.
    """

    def test_short_series_stay_complete(self):
        closes = [1.0, 2.0, 3.0]
        assert thin(candles(closes)) == [c.close for c in candles(closes)]

    def test_long_series_are_thinned(self):
        result = thin(candles(list(range(1, 201))), points=20)
        assert len(result) == 20

    def test_the_last_candle_always_survives(self):
        """Ein Verlauf, der kurz vor der Gegenwart endet, waere irrefuehrend."""
        closes = [float(i) for i in range(1, 201)]
        assert thin(candles(closes), points=20)[-1] == closes[-1]

    def test_the_first_candle_always_survives(self):
        closes = [float(i) for i in range(1, 201)]
        assert thin(candles(closes), points=20)[0] == closes[0]

    def test_the_order_is_kept(self):
        result = thin(candles([float(i) for i in range(1, 101)]), points=10)
        assert result == sorted(result)

    def test_empty_input(self):
        assert thin([]) == []

    def test_the_analysis_carries_it(self):
        result = analyse(candles(rising()))
        assert result.spark
        assert result.to_dict()["spark"] == result.spark

    def test_an_empty_analysis_has_an_empty_series(self):
        assert analyse([]).spark == []


class TestCheck:
    def test_not_fetched_yields_nothing(self):
        """None heisst 'keine Kerzen abgerufen' - das ist kein Befund."""
        assert check_structure(TokenData(mint=MINT), RISK) == []

    def test_short_history_is_not_an_all_clear(self):
        result = run(analyse(candles([10, 11, 12])))
        assert "too_little_history" in codes(result)
        assert "uptrend" not in codes(result)

    def test_downtrend_counts_against_the_token(self):
        result = run(analyse(candles(falling())))
        assert "downtrend" in codes(result)
        assert any(f.severity is Severity.MEDIUM for f in result)

    def test_uptrend_is_reported_without_penalty(self):
        result = run(analyse(candles(rising())))
        assert "uptrend" in codes(result)
        assert all(f.severity is Severity.INFO for f in result)

    def test_faded_token_is_flagged(self):
        """Genau der Fall, der vorher unsichtbar war: ruhig aussehender
        Token, der seinen Lauf laengst hinter sich hat."""
        result = run(analyse(candles([10, 100, 80, 60, 40, 20, 12] + [11] * 10)))
        assert "past_its_peak" in codes(result)

    def test_a_normal_pullback_is_not_flagged(self):
        result = run(analyse(candles([10, 14, 12, 15, 13, 16, 14] + [13] * 8)))
        assert "past_its_peak" not in codes(result)

    def test_minimum_candles_is_respected(self):
        assert len(candles([1.0] * (MIN_CANDLES - 1))) < MIN_CANDLES
        assert not analyse(candles([1.0] * (MIN_CANDLES - 1))).readable
