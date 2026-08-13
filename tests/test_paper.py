"""Tests des Papierhandels.

Das hier ist der Auto-Trader ohne Geld - dieselbe Entscheidungskette, nur
dass am Ende keine Transaktion steht. Wenn diese Tests gruen sind und die
Bilanz nach einer Woche positiv ist, wird daraus durch einen Schalter der
echte Handel. Entsprechend genau muessen die Ausstiegsregeln stimmen.

Der wichtigste Test ist der unbequeme: ein Token, dessen Markt verschwindet,
ist nicht "unbekannt", sondern wertlos. Wer das als fehlende Messung
verbucht, rechnet sich seine Bilanz schoen.
"""

from __future__ import annotations

import pytest
from conftest import MINT

from xeno.calls import Call, Signal
from xeno.paper import (
    MAX_HOLD_SECONDS,
    STOP_LOSS,
    TAKE_PROFIT,
    PaperBook,
    Position,
)

NOW = 1_700_000_000.0


def call(mint: str = MINT, price: float = 1.0, strength: int = 3) -> Call:
    return Call(
        mint=mint,
        symbol="TEST",
        price_usd=price,
        signals=[Signal(f"s{i}", True, f"Grund {i}") for i in range(strength)],
    )


@pytest.fixture
def book(tmp_path):
    return PaperBook(tmp_path / "paper.json")


class TestEntry:
    def test_a_call_opens_a_position(self, book):
        position = book.enter(call(), now=NOW)
        assert position is not None
        assert position.entry_price == 1.0
        assert book.holds(MINT)

    def test_the_reasons_are_kept(self, book):
        position = book.enter(call(strength=3), now=NOW)
        assert len(position.reasons) == 3

    def test_no_second_position_in_the_same_token(self, book):
        """Sonst zaehlte derselbe Kursverlauf doppelt und die Bilanz waere
        verfaelscht."""
        book.enter(call(), now=NOW)
        assert book.enter(call(), now=NOW + 60) is None
        assert len(book.open_positions) == 1

    def test_without_a_price_there_is_no_entry(self, book):
        assert book.enter(call(price=0), now=NOW) is None


class TestExitRules:
    def _opened(self, book):
        return book.enter(call(price=1.0), now=NOW)

    def test_target_reached(self, book):
        self._opened(book)
        closed = book.update({MINT: TAKE_PROFIT}, now=NOW + 60)
        assert len(closed) == 1
        assert closed[0].exit_reason == "ziel"

    def test_stop_loss(self, book):
        self._opened(book)
        closed = book.update({MINT: STOP_LOSS - 0.01}, now=NOW + 60)
        assert closed[0].exit_reason == "verlustgrenze"

    def test_time_limit(self, book):
        self._opened(book)
        closed = book.update({MINT: 1.1}, now=NOW + MAX_HOLD_SECONDS + 1)
        assert closed[0].exit_reason == "zeitlimit"

    def test_nothing_happens_in_between(self, book):
        self._opened(book)
        assert book.update({MINT: 1.3}, now=NOW + 60) == []
        assert book.open_positions

    def test_a_vanished_market_is_a_total_loss_not_a_gap(self, book):
        """Der unbequeme Fall. Wer ihn als fehlende Messung verbucht, rechnet
        sich die Bilanz schoen."""
        self._opened(book)
        closed = book.update({}, now=NOW + MAX_HOLD_SECONDS + 1)
        assert closed[0].exit_reason == "markt_weg"
        assert closed[0].result_usd() == -100.0

    def test_a_missing_price_alone_does_not_close(self, book):
        """Eine Quelle kann kurz aussetzen - das ist kein Totalverlust."""
        self._opened(book)
        assert book.update({}, now=NOW + 60) == []


class TestPeak:
    def test_the_high_water_mark_is_tracked(self, book):
        book.enter(call(price=1.0), now=NOW)
        book.update({MINT: 1.8}, now=NOW + 60)
        book.update({MINT: 1.2}, now=NOW + 120)
        assert book.open_positions[0].peak_price == 1.8

    def test_the_peak_shows_what_a_better_exit_would_have_given(self, book):
        book.enter(call(price=1.0), now=NOW)
        book.update({MINT: 1.9}, now=NOW + 60)
        position = book.open_positions[0]
        assert position.multiple == pytest.approx(1.9)


class TestResults:
    def test_a_win_is_counted_in_dollars(self, book):
        book.enter(call(price=1.0), now=NOW)
        book.update({MINT: 2.0}, now=NOW + 60)
        assert book.closed_positions[0].result_usd() == pytest.approx(100.0)

    def test_a_loss_is_counted_in_dollars(self, book):
        book.enter(call(price=1.0), now=NOW)
        book.update({MINT: 0.5}, now=NOW + 60)
        assert book.closed_positions[0].result_usd() == pytest.approx(-50.0)

    def test_the_summary_adds_up(self, book):
        book.enter(call(mint="a", price=1.0), now=NOW)
        book.enter(call(mint="b", price=1.0), now=NOW)
        book.update({"a": 2.0, "b": 0.5}, now=NOW + 60)

        result = book.summary()
        assert result["closed"] == 2
        assert result["wins"] == 1
        assert result["win_rate"] == 50.0
        assert result["result_usd"] == pytest.approx(50.0)

    def test_an_empty_book_summarises_cleanly(self, book):
        result = book.summary()
        assert result["closed"] == 0
        assert result["result_usd"] == 0
        assert result["median_multiple"] is None

    def test_open_positions_are_shown_unrealised(self, book):
        book.enter(call(price=1.0), now=NOW)
        result = book.summary({MINT: 1.5})
        assert result["open"] == 1
        assert result["unrealised_usd"] == pytest.approx(50.0)


class TestTrackRecord:
    def test_without_history_it_says_so(self, book):
        assert "noch keine" in book.hit_rate_text()

    def test_the_number_that_belongs_on_every_call(self, book):
        book.enter(call(mint="a", price=1.0), now=NOW)
        book.enter(call(mint="b", price=1.0), now=NOW)
        book.update({"a": 2.0, "b": 0.5}, now=NOW + 60)
        assert "von 2" in book.hit_rate_text()
        assert "1 im Plus" in book.hit_rate_text()


class TestPersistence:
    def test_positions_survive_a_restart(self, tmp_path):
        path = tmp_path / "paper.json"
        first = PaperBook(path)
        first.enter(call(price=1.0), now=NOW)
        first.save(force=True)

        second = PaperBook(path)
        assert second.holds(MINT)
        assert second.open_positions[0].entry_price == 1.0

    def test_a_broken_file_does_not_stop_the_start(self, tmp_path):
        path = tmp_path / "paper.json"
        path.write_text("{kaputt", encoding="utf-8")
        assert PaperBook(path).positions == []

    def test_an_unwritable_place_does_not_raise(self, tmp_path, monkeypatch):
        book = PaperBook(tmp_path / "paper.json")
        book.enter(call(price=1.0), now=NOW)

        def boom(*args, **kwargs):
            raise OSError("Platte voll")

        monkeypatch.setattr("tempfile.mkstemp", boom)
        book.save(force=True)


def test_decide_needs_a_sane_price():
    position = Position(mint=MINT, opened_at=NOW, entry_price=1.0)
    assert position.decide(0.0, NOW + 60) == ""
    assert Position(mint=MINT).decide(1.0, NOW) == ""
