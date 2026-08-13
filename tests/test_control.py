"""Tests der Vergleichsgruppe.

Bisher verschwanden die im Vorfilter abgelehnten Token spurlos. Damit liess
sich sagen, wie sich die *durchgelassenen* entwickelt haben - nie aber, was
der Filter faelschlich aussortiert hat. Ein Filter, der seine eigenen Fehler
nicht kennt, kann sich nicht verbessern.

Die zwei Eigenschaften, an denen so eine Gruppe steht und faellt: sie muss
zufaellig gezogen sein, und sie darf nie in die geprueften ueberlaufen.
"""

from __future__ import annotations

from conftest import MINT, make_candidate

from xeno.models import ScreenResult
from xeno.watcher import CONTROL_SAMPLE, Watcher
from xeno.watchstate import WatchState

NOW = 1_700_000_000.0


def screen_result(
    mint: str, passed: bool, price: float | None = 0.001, age_minutes: float = 120.0
) -> ScreenResult:
    candidate = make_candidate(age_minutes=age_minutes, price_usd=price)
    candidate.mint = mint
    return ScreenResult(
        candidate=candidate,
        passed=passed,
        reasons=[] if passed else ["Liquiditaet zu gering"],
    )


def watcher(state: WatchState) -> Watcher:
    """Ein Watcher, von dem nur die Stichprobenlogik gebraucht wird."""
    from xeno.config import Settings

    instance = Watcher.__new__(Watcher)
    instance.state = state
    instance.settings = Settings()
    instance.book = None
    return instance


class TestSampling:
    def test_rejected_tokens_are_kept(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        screened = [screen_result(f"m{i}", passed=False) for i in range(10)]

        taken = watcher(state)._sample_control(screened, NOW)

        assert taken == CONTROL_SAMPLE
        assert sum(1 for s in state.tokens.values() if s.control) == CONTROL_SAMPLE

    def test_passed_tokens_are_not_sampled(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        screened = [screen_result(f"m{i}", passed=True) for i in range(10)]
        assert watcher(state)._sample_control(screened, NOW) == 0

    def test_without_a_price_there_is_nothing_to_measure(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        screened = [screen_result(f"m{i}", passed=False, price=None) for i in range(5)]
        assert watcher(state)._sample_control(screened, NOW) == 0

    def test_already_known_tokens_stay_in_their_group(self, tmp_path):
        """Ein Token, der schon geprueft wurde, gehoert nicht in die
        Vergleichsgruppe - sonst zaehlte er doppelt."""
        state = WatchState(tmp_path / "s.json")
        state.add_to_watchlist("m0")
        screened = [screen_result("m0", passed=False)]
        assert watcher(state)._sample_control(screened, NOW) == 0

    def test_fewer_rejects_than_the_sample_size(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        assert watcher(state)._sample_control([screen_result("m0", False)], NOW) == 1

    def test_nothing_rejected_is_fine(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        assert watcher(state)._sample_control([], NOW) == 0

    def test_too_young_is_not_a_rejection(self, tmp_path):
        """Beim ersten echten Durchlauf bestand die halbe Stichprobe aus
        Token, die schlicht noch keine drei Minuten alt waren. Die werden
        gleich reguler geprueft - kaemen sie jetzt in die Vergleichsgruppe,
        waeren sie dort fuer immer gefangen."""
        state = WatchState(tmp_path / "s.json")
        screened = [
            screen_result(f"m{i}", passed=False, age_minutes=0.5) for i in range(10)
        ]
        assert watcher(state)._sample_control(screened, NOW) == 0

    def test_genuinely_rejected_tokens_are_still_taken(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        screened = [
            screen_result("jung", passed=False, age_minutes=0.5),
            screen_result("alt", passed=False, age_minutes=300.0),
        ]
        watcher(state)._sample_control(screened, NOW)
        assert state.known("alt")
        assert not state.known("jung")


class TestRecording:
    def test_the_baseline_is_set_so_it_can_be_measured(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        candidate = make_candidate(price_usd=0.005)

        entry = state.record_control(candidate, "zu wenig Volumen", NOW)

        assert entry.control is True
        assert entry.baseline_price_usd == 0.005
        assert entry.baseline_at == NOW
        assert entry.control_reason == "zu wenig Volumen"

    def test_it_gets_its_own_group_in_the_evaluation(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        entry = state.record_control(make_candidate(price_usd=0.001), "", NOW)
        assert entry.first_verdict == "CONTROL"

    def test_no_price_no_entry(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        assert state.record_control(make_candidate(price_usd=None), "", NOW) is None

    def test_an_existing_token_is_never_overwritten(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.add_to_watchlist(MINT, "TEST")
        assert state.record_control(make_candidate(price_usd=0.1), "", NOW) is None
        assert state.get(MINT).control is False


class TestIsolation:
    def test_a_control_token_is_never_deep_checked(self, tmp_path):
        """Sonst waere es keine Vergleichsgruppe mehr, sondern Teil der
        geprueften - und der Vergleich waere wertlos."""
        state = WatchState(tmp_path / "s.json")
        state.record_control(make_candidate(price_usd=0.001), "", NOW)
        assert state.is_due(MINT, NOW + 10 * 3600) is False

    def test_normal_tokens_are_still_due(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        assert state.is_due("unbekannt", NOW) is True


class TestMeasurement:
    def test_control_tokens_are_picked_up_by_the_tracker(self, tmp_path):
        """Der ganze Zweck: sie werden genauso gemessen wie die geprueften."""
        from xeno.follow import due_measurements

        state = WatchState(tmp_path / "s.json")
        state.record_control(make_candidate(price_usd=0.001), "", NOW)

        pending, _ = due_measurements(state, NOW + 16 * 60)
        assert MINT in pending["15m"]

    def test_rejected_tokens_are_paper_traded_too(self, tmp_path):
        """Nur so steht ihr Ergebnis in derselben Waehrung wie das der
        empfohlenen - ein Median laesst sich nicht mit einer
        Gewinnrechnung vergleichen."""
        from xeno.paper import PaperBook

        state = WatchState(tmp_path / "s.json")
        instance = watcher(state)
        instance.book = PaperBook(tmp_path / "paper.json")

        instance._sample_control([screen_result("abgelehnt", passed=False)], NOW)

        assert instance.book.holds("abgelehnt")
        assert instance.book.positions[0].group == "CONTROL"

    def test_they_appear_as_their_own_group(self, tmp_path):
        from xeno.follow import summarise

        state = WatchState(tmp_path / "s.json")
        entry = state.record_control(make_candidate(price_usd=0.001), "", NOW)
        entry.outcomes["1h"] = 0.4

        summary = summarise(list(state.tokens.values()))
        assert "CONTROL" in summary
        assert summary["CONTROL"]["1h"]["count"] == 1
