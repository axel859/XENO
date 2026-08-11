"""Tests fuer Nachverfolgung, Auswertung und Momentum.

Hintergrund aus dem Betrieb: die als "gut" eingestuften Token bewegten sich
kaum, waehrend die mit AVOID markierten deutlich stiegen. Ob das ein Muster
ist oder Zufall bei einer Handvoll Token, laesst sich nur mit Zahlen klaeren -
und dafuer muessen die Ergebnisse ueberhaupt erst festgehalten werden.
"""

from __future__ import annotations

import time

from conftest import MINT, make_candidate, make_mint_info

from xeno.follow import (
    HORIZONS,
    OutcomeTracker,
    due_measurements,
    is_dead,
    summarise,
)
from xeno.models import Finding, RiskReport, Severity, Verdict
from xeno.watchstate import TokenState, WatchState

OTHER = "B" * 43


def report_with(verdict_findings, mint=MINT, symbol="TEST", price=1.0):
    report = RiskReport(
        mint=mint,
        symbol=symbol,
        mint_info=make_mint_info(),
        candidate=make_candidate(mint=mint, price_usd=price, fdv_usd=price * 1e9),
    )
    report.findings = verdict_findings
    return report


def clean(mint=MINT, price=1.0):
    return report_with(
        [Finding(check="t", code="clean", severity=Severity.INFO, message="ok")],
        mint=mint,
        price=price,
    )


def bad(mint=MINT, price=1.0):
    return report_with(
        [Finding(check="t", code="lp_unlocked", severity=Severity.CRITICAL, message="lp")],
        mint=mint,
        price=price,
    )


class TestBaseline:
    def test_first_check_records_the_starting_point(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(clean(price=0.5))
        entry = state.get(MINT)
        assert entry.baseline_price_usd == 0.5
        assert entry.first_verdict == Verdict.OK.value
        assert entry.baseline_at > 0

    def test_later_checks_do_not_move_the_starting_point(self, tmp_path):
        """Sonst misst man gegen einen mitgewanderten Kurs und die Statistik
        haette im Nachhinein immer recht."""
        state = WatchState(tmp_path / "s.json")
        state.record(clean(price=0.5))
        first = state.get(MINT).baseline_at

        state.record(clean(price=5.0), now=time.time() + 600)
        entry = state.get(MINT)
        assert entry.baseline_price_usd == 0.5
        assert entry.baseline_at == first

    def test_first_verdict_is_kept_even_when_the_verdict_changes(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(clean(price=1.0))
        state.record(bad(price=1.0), now=time.time() + 600)
        entry = state.get(MINT)
        assert entry.first_verdict == Verdict.OK.value
        assert entry.verdict == Verdict.AVOID.value

    def test_without_a_price_nothing_is_tracked(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        report = clean()
        report.candidate.price_usd = None
        state.record(report)
        assert state.get(MINT).baseline_at == 0.0


class TestDueMeasurements:
    def _state(self, tmp_path, age_seconds: float, recorded=None):
        state = WatchState(tmp_path / "s.json")
        state.record(clean(price=1.0))
        entry = state.get(MINT)
        entry.baseline_at = time.time() - age_seconds
        entry.outcomes = recorded or {}
        return state

    def test_nothing_due_right_away(self, tmp_path):
        pending, missed = due_measurements(self._state(tmp_path, 60), time.time())
        assert pending == {} and missed == {}

    def test_first_horizon_becomes_due(self, tmp_path):
        pending, _ = due_measurements(self._state(tmp_path, 16 * 60), time.time())
        assert pending.get("15m") == [MINT]
        assert "1h" not in pending

    def test_long_downtime_marks_horizons_as_missed(self, tmp_path):
        """Der entscheidende Fall bei einem Bot, der nicht durchlaeuft: nach
        sieben Stunden Pause darf der aktuelle Kurs nicht als '15-Minuten-Wert'
        eingetragen werden. Nur der 6h-Punkt liegt noch im Rahmen."""
        pending, missed = due_measurements(self._state(tmp_path, 7 * 3600), time.time())
        assert set(missed) == {"15m", "1h"}
        assert set(pending) == {"6h"}

    def test_slightly_late_is_still_accepted(self, tmp_path):
        """Ein Durchlauf alle 60s trifft die Zeitpunkte nie exakt - eine
        knappe Verspaetung muss deshalb zaehlen."""
        pending, missed = due_measurements(self._state(tmp_path, 70 * 60), time.time())
        assert pending.get("1h") == [MINT]
        assert "1h" not in missed

    def test_already_measured_is_skipped(self, tmp_path):
        state = self._state(tmp_path, 16 * 60, recorded={"15m": 1.2})
        pending, missed = due_measurements(state, time.time())
        assert pending == {} and missed == {}


class FakePrices:
    def __init__(self, prices):
        self.prices_map = prices
        self.calls = 0

    def prices(self, mints, batch_size=30):
        self.calls += 1
        return {m: p for m, p in self.prices_map.items() if m in mints}


class TestOutcomeTracker:
    def _ready(self, tmp_path, price=1.0):
        state = WatchState(tmp_path / "s.json")
        state.record(clean(price=price))
        state.get(MINT).baseline_at = time.time() - 16 * 60
        return state

    def test_records_the_multiple(self, tmp_path):
        state = self._ready(tmp_path, price=2.0)
        OutcomeTracker(state, FakePrices({MINT: 6.0})).run(time.time())
        assert state.get(MINT).outcomes["15m"] == 3.0

    def test_vanished_market_counts_as_worthless(self, tmp_path):
        """Kein Paar mehr auffindbar ist keine fehlende Messung, sondern das
        Ergebnis - sonst faellt genau der schlimmste Ausgang aus der Statistik."""
        state = self._ready(tmp_path)
        OutcomeTracker(state, FakePrices({})).run(time.time())
        assert state.get(MINT).outcomes["15m"] == 0.0

    def test_many_tokens_cost_one_request(self, tmp_path):
        """Der eigentliche Sinn der Buendelung: 30 faellige Token, ein Abruf."""
        state = WatchState(tmp_path / "s.json")
        mints = [f"mint{i:039d}" for i in range(12)]
        for mint in mints:
            state.record(clean(mint=mint, price=1.0))
            state.get(mint).baseline_at = time.time() - 16 * 60

        source = FakePrices({m: 2.0 for m in mints})
        stats = OutcomeTracker(state, source).run(time.time())
        assert source.calls == 1
        assert stats.measured == 12

    def test_missed_horizons_are_recorded_as_gaps(self, tmp_path):
        """War der Bot lange aus, wird die Luecke vermerkt statt geraten."""
        state = self._ready(tmp_path)
        state.get(MINT).baseline_at = time.time() - 30 * 3600
        stats = OutcomeTracker(state, FakePrices({MINT: 50.0})).run(time.time())
        outcomes = state.get(MINT).outcomes
        assert outcomes["15m"] is None
        assert outcomes["1h"] is None
        assert stats.missing >= 2

    def test_gaps_are_not_retried(self, tmp_path):
        state = self._ready(tmp_path)
        state.get(MINT).baseline_at = time.time() - 30 * 3600
        OutcomeTracker(state, FakePrices({MINT: 50.0})).run(time.time())
        source = FakePrices({MINT: 50.0})
        stats = OutcomeTracker(state, source).run(time.time())
        assert stats.missing == 0

    def test_failure_is_reported_not_raised(self, tmp_path):
        class Broken:
            def prices(self, mints, batch_size=30):
                raise RuntimeError("API weg")

        state = self._ready(tmp_path)
        errors: list[str] = []
        stats = OutcomeTracker(state, Broken()).run(time.time(), on_error=errors.append)
        assert stats.measured == 0
        assert errors

    def test_nothing_due_costs_no_request(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(clean(price=1.0))
        source = FakePrices({MINT: 1.0})
        OutcomeTracker(state, source).run(time.time())
        assert source.calls == 0


class TestSummary:
    def _entry(self, verdict: str, outcomes: dict[str, float], mint="m") -> TokenState:
        return TokenState(mint=mint, first_verdict=verdict, outcomes=outcomes)

    def test_groups_by_first_verdict(self):
        summary = summarise(
            [
                self._entry("OK", {"1h": 1.5}, "a"),
                self._entry("AVOID", {"1h": 4.0}, "b"),
            ]
        )
        assert set(summary) == {"OK", "AVOID"}

    def test_uses_the_median_not_the_average(self):
        """Ein einzelner Hunderter darf zwanzig Totalverluste nicht verdecken."""
        entries = [self._entry("OK", {"1h": 0.1}, f"m{i}") for i in range(4)]
        entries.append(self._entry("OK", {"1h": 100.0}, "ausreisser"))
        row = summarise(entries)["OK"]["1h"]
        assert row["median"] < 1.0
        assert row["best"] == 100.0

    def test_counts_total_losses(self):
        entries = [
            self._entry("AVOID", {"1h": 0.02}, "a"),
            self._entry("AVOID", {"1h": 3.0}, "b"),
        ]
        assert summarise(entries)["AVOID"]["1h"]["dead_pct"] == 50.0

    def test_counts_doublers(self):
        entries = [
            self._entry("OK", {"1h": 2.5}, "a"),
            self._entry("OK", {"1h": 1.1}, "b"),
        ]
        assert summarise(entries)["OK"]["1h"]["winners_pct"] == 50.0

    def test_entries_without_results_are_ignored(self):
        assert summarise([self._entry("OK", {}, "a")]) == {}

    def test_gaps_do_not_count_as_zero(self):
        """Eine verpasste Messung ist keine Null - sonst saehe jede Pause des
        Bots wie eine Reihe von Totalverlusten aus."""
        entries = [
            self._entry("OK", {"1h": None}, "a"),
            self._entry("OK", {"1h": 2.0}, "b"),
        ]
        row = summarise(entries)["OK"]["1h"]
        assert row["count"] == 1
        assert row["dead_pct"] == 0.0

    def test_dead_threshold(self):
        assert is_dead(0.05) and is_dead(0.1)
        assert not is_dead(0.11)


class TestMomentum:
    def test_neutral_without_data(self):
        candidate = make_candidate(
            price_change_h1_pct=None, buys_h1=None, sells_h1=None, buyers_h1=None
        )
        assert 40 <= candidate.momentum <= 60

    def test_rising_price_scores_higher(self):
        up = make_candidate(price_change_h1_pct=180.0)
        down = make_candidate(price_change_h1_pct=-60.0)
        assert up.momentum > down.momentum

    def test_extreme_gains_are_capped(self):
        big = make_candidate(price_change_h1_pct=400.0)
        huge = make_candidate(price_change_h1_pct=9000.0)
        assert big.momentum == huge.momentum

    def test_stays_within_bounds(self):
        extreme = make_candidate(
            price_change_h1_pct=99999.0, buys_h1=9999, sells_h1=1, buyers_h1=5000
        )
        assert 0 <= extreme.momentum <= 100

    def test_is_independent_of_risk(self):
        """Der Kern der Trennung: ein gefaehrlicher Token darf hohen Schwung
        haben - genau das war die Beobachtung, die dazu gefuehrt hat."""
        dangerous_but_pumping = make_candidate(
            price_change_h1_pct=250.0, buys_h1=300, sells_h1=20, buyers_h1=150
        )
        assert dangerous_but_pumping.momentum >= 80
