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
    DEFAULT_RETENTION,
    DEFAULT_SIZE_USD,
    MAX_HOLD_SECONDS,
    STOP_LOSS,
    TAKE_PROFIT,
    PaperBook,
    Position,
    PositionTracker,
)

NOW = 1_700_000_000.0


def call(mint: str = MINT, price: float = 1.0, strength: int = 3) -> Call:
    return Call(
        mint=mint,
        symbol="TEST",
        price_usd=price,
        signals=[Signal(f"s{i}", True, f"Grund {i}") for i in range(strength)],
    )


def buy(book, mint: str = MINT, price: float = 1.0, *, costs: bool = False, **kwargs):
    """Kurzform fuer die Tests - ein Einstieg mit sinnvollen Vorgaben.

    Ohne ``costs=True`` werden die Handelskosten abgeschaltet. Das ist
    Absicht und keine Bequemlichkeit: die Tests hier pruefen Regeln, und
    eine Erwartung von ``-50.00`` sagt darueber mehr aus als ``-52.60``.
    Die Kosten sind in ``TestKosten`` die gemessene Groesse, dort stehen sie
    ungerundet.
    """
    kwargs.setdefault("symbol", "TEST")
    kwargs.setdefault("group", "OK")
    kwargs.setdefault("now", NOW)
    position = book.enter(mint, price, **kwargs)
    if position is not None and not costs:
        position.retention = 1.0
        position.fee_usd = 0.0
    return position


def costless(**kwargs) -> Position:
    """Eine Position ohne Handelskosten - siehe ``buy``."""
    kwargs.setdefault("retention", 1.0)
    kwargs.setdefault("fee_usd", 0.0)
    return Position(**kwargs)


@pytest.fixture
def book(tmp_path):
    return PaperBook(tmp_path / "paper.json")


class TestEntry:
    def test_a_call_opens_a_position(self, book):
        position = buy(book)
        assert position is not None
        assert position.entry_price == 1.0
        assert book.holds(MINT)

    def test_the_reasons_are_kept(self, book):
        position = buy(book, strength=3, reasons=['a','b','c'])
        assert len(position.reasons) == 3

    def test_no_second_position_in_the_same_token(self, book):
        """Sonst zaehlte derselbe Kursverlauf doppelt und die Bilanz waere
        verfaelscht."""
        buy(book)
        assert buy(book, now=NOW + 60) is None
        assert len(book.open_positions) == 1

    def test_without_a_price_there_is_no_entry(self, book):
        assert buy(book, price=0) is None


class TestExitRules:
    def _opened(self, book):
        return buy(book)

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

    def test_a_zero_price_does_not_leave_the_position_hanging(self, book):
        """Beim Aufsetzen der Trades-Seite aufgefallen: ein Kurs von exakt
        null liess die Position ewig offen - die Ausstiegsregel kann mit dem
        Wert nichts anfangen, und selbst das Zeitlimit griff nicht."""
        self._opened(book)
        assert book.update({MINT: 0.0}, now=NOW + 60) == []
        closed = book.update({MINT: 0.0}, now=NOW + MAX_HOLD_SECONDS + 1)
        assert closed and closed[0].exit_reason == "markt_weg"


class TestPeak:
    def test_the_high_water_mark_is_tracked(self, book):
        buy(book)
        book.update({MINT: 1.8}, now=NOW + 60)
        book.update({MINT: 1.2}, now=NOW + 120)
        assert book.open_positions[0].peak_price == 1.8

    def test_the_peak_shows_what_a_better_exit_would_have_given(self, book):
        buy(book)
        book.update({MINT: 1.9}, now=NOW + 60)
        position = book.open_positions[0]
        assert position.multiple == pytest.approx(1.9)


class TestResults:
    def test_a_win_is_counted_in_dollars(self, book):
        buy(book)
        book.update({MINT: 2.0}, now=NOW + 60)
        assert book.closed_positions[0].result_usd() == pytest.approx(100.0)

    def test_a_loss_is_counted_in_dollars(self, book):
        buy(book)
        book.update({MINT: 0.5}, now=NOW + 60)
        assert book.closed_positions[0].result_usd() == pytest.approx(-50.0)

    def test_the_summary_adds_up(self, book):
        buy(book, "a")
        buy(book, "b")
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
        buy(book)
        result = book.summary({MINT: 1.5})
        assert result["open"] == 1
        assert result["unrealised_usd"] == pytest.approx(50.0)


class TestTrackRecord:
    def test_without_history_it_says_so(self, book):
        assert "noch keine" in book.hit_rate_text()

    def test_the_number_that_belongs_on_every_call(self, book):
        buy(book, "a")
        buy(book, "b")
        book.update({"a": 2.0, "b": 0.5}, now=NOW + 60)
        assert "von 2" in book.hit_rate_text()
        assert "1 im Plus" in book.hit_rate_text()


class TestPersistence:
    def test_positions_survive_a_restart(self, tmp_path):
        path = tmp_path / "paper.json"
        first = PaperBook(path)
        buy(first)
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
        buy(book)

        def boom(*args, **kwargs):
            raise OSError("Platte voll")

        monkeypatch.setattr("tempfile.mkstemp", boom)
        book.save(force=True)


class TestGroups:
    """Die Auswertung, um die es eigentlich geht.

    Simuliert wird jedes Urteil, nicht nur die Calls. Sonst gaebe es zwar
    eine Zahl fuer die Vorschlaege, aber keine Vergleichszahl - und ob die
    strengen Bedingungen ueberhaupt etwas bringen, bliebe offen.
    """

    def _mixed(self, book):
        buy(book, "gut", group="OK", is_call=True)
        buy(book, "auch-gut", group="OK")
        buy(book, "mittel", group="CAUTION")
        buy(book, "meiden", group="AVOID")
        book.update(
            {"gut": 2.0, "auch-gut": 0.5, "mittel": 1.0, "meiden": 3.0},
            now=NOW + MAX_HOLD_SECONDS + 1,
        )

    def test_each_verdict_gets_its_own_line(self, book):
        self._mixed(book)
        groups = book.by_group()
        assert set(groups) >= {"OK", "CAUTION", "AVOID", "CALL"}

    def test_calls_count_inside_their_verdict_too(self, book):
        """Ein Call ist eine Teilmenge von OK, keine eigene Kategorie - erst
        der Vergleich beider Zeilen zeigt, ob die Bedingungen etwas bringen."""
        self._mixed(book)
        groups = book.by_group()
        assert groups["OK"]["closed"] == 2
        assert groups["CALL"]["closed"] == 1

    def test_the_comparison_that_started_all_this(self, book):
        """Genau die Frage: liefen die abgelehnten besser als die guten?"""
        self._mixed(book)
        groups = book.by_group()
        assert groups["AVOID"]["result_usd"] > groups["OK"]["result_usd"]

    def test_turnover_counts_every_position(self, book):
        self._mixed(book)
        assert book.summary()["turnover_usd"] == pytest.approx(400.0)

    def test_an_empty_book_has_no_groups(self, book):
        assert book.by_group() == {}


class TestMarketCap:
    def test_the_entry_valuation_is_kept(self, book):
        position = buy(book, mcap=50_000)
        assert position.entry_mcap_usd == 50_000

    def test_the_current_valuation_follows_the_price(self, book):
        """Hochgerechnet statt abgefragt: die Supply liegt fest, also bewegt
        sich die Bewertung genau wie der Kurs - das erspart eine zweite
        Abfrage je Position."""
        position = buy(book, price=1.0, mcap=50_000)
        assert position.mcap_at(2.0) == pytest.approx(100_000)
        assert position.mcap_at(0.5) == pytest.approx(25_000)

    def test_the_exit_valuation_is_derived(self, book):
        buy(book, price=1.0, mcap=50_000)
        book.update({MINT: 2.0}, now=NOW + 60)
        assert book.closed_positions[0].exit_mcap_usd == pytest.approx(100_000)

    def test_without_an_entry_valuation_nothing_is_invented(self, book):
        position = buy(book, mcap=None)
        assert position.mcap_at(2.0) is None

    def test_the_last_price_is_remembered(self, book):
        """Fuer die Vergleichsgruppe gibt es nie einen Bericht, aus dem sich
        ein aktueller Kurs ablesen liesse - ohne dieses Gedaechtnis haetten
        ihre Positionen in der Oberflaeche nie einen Wert."""
        buy(book, price=1.0, mcap=50_000, group="CONTROL")
        book.update({MINT: 1.4}, now=NOW + 60)
        position = book.open_positions[0]
        assert position.last_price == pytest.approx(1.4)
        assert position.mcap_at(position.last_price) == pytest.approx(70_000)

    def test_the_unrealised_result_uses_it(self, book):
        buy(book, price=1.0, group="CONTROL")
        book.update({MINT: 1.5}, now=NOW + 60)
        assert book.summary()["unrealised_usd"] == pytest.approx(50.0)


class FakePrices:
    """Kursquelle, die zaehlt und auf Wunsch ausfaellt."""

    def __init__(self, prices=None, error: Exception | None = None) -> None:
        self.values = prices or {}
        self.error = error
        self.calls: list[list[str]] = []

    def prices(self, mints, **kwargs):
        self.calls.append(list(mints))
        if self.error is not None:
            raise self.error
        return {m: self.values[m] for m in mints if m in self.values}


class TestPositionTracker:
    """Die Verfolgung laeuft in einer eigenen, schnelleren Schleife.

    Der Pruefzyklus ist dafuer zu grob: er laeuft je nach Kontingent alle
    ein bis fuenf Minuten. Ein Token, der dazwischen auf 2.5x schiesst und
    auf 1.1x zurueckfaellt, hat sein Ziel erreicht, ohne dass es jemand
    gesehen haette - die Bilanz faellt dadurch schlechter aus als die
    Strategie wirklich ist.
    """

    def test_a_tick_closes_what_is_due(self, book):
        buy(book, price=1.0)
        source = FakePrices({MINT: TAKE_PROFIT})
        tracker = PositionTracker(book, source, log=lambda m: None)

        closed = tracker.tick(now=NOW + 60)

        assert len(closed) == 1
        assert closed[0].exit_reason == "ziel"

    def test_without_open_positions_nothing_is_fetched(self, book):
        source = FakePrices()
        assert PositionTracker(book, source).tick() == []
        assert source.calls == []

    def test_only_open_positions_are_asked_for(self, book):
        buy(book, "offen", price=1.0)
        buy(book, "zu", price=1.0)
        book.update({"zu": TAKE_PROFIT}, now=NOW + 60)

        source = FakePrices({"offen": 1.2})
        PositionTracker(book, source).tick(now=NOW + 120)
        assert source.calls == [["offen"]]

    def test_a_broken_price_source_does_not_stop_the_loop(self, book):
        """Sonst stuenden die Positionen still, ohne dass es auffiele."""
        buy(book, price=1.0)
        messages = []
        tracker = PositionTracker(
            book, FakePrices(error=OSError("weg")), log=messages.append
        )

        assert tracker.tick(now=NOW + 60) == []
        assert tracker.errors == 1
        assert messages and "Kurse" in messages[0]
        assert book.open_positions  # Position bleibt bestehen

    def test_closures_are_reported(self, book):
        buy(book, price=1.0, group="AVOID")
        messages = []
        tracker = PositionTracker(
            book, FakePrices({MINT: TAKE_PROFIT}), log=messages.append
        )
        tracker.tick(now=NOW + 60)
        assert messages and "AVOID" in messages[0]

    def test_the_interval_has_a_floor(self, book):
        """Sekundentakt bringt nichts ausser Last auf einer fremden API."""
        assert PositionTracker(book, FakePrices(), interval=0.1).interval >= 5.0

    def test_start_and_stop(self, book):
        tracker = PositionTracker(book, FakePrices(), interval=5.0)
        assert tracker.start() is True
        assert tracker.running is True
        assert tracker.start() is False       # kein zweiter Thread
        assert tracker.stop() is True
        assert tracker.running is False
        assert tracker.stop() is False


def test_decide_needs_a_sane_price():
    position = Position(mint=MINT, opened_at=NOW, entry_price=1.0)
    assert position.decide(0.0, NOW + 60) == ""
    assert Position(mint=MINT).decide(1.0, NOW) == ""


class TestKaputterEinstieg:
    """Der Fall, der die Bilanz auf 20,5 Millionen Dollar gehoben hat.

    Echt aufgetreten: 20.800 USD Umsatz, angeblich +20.595.246 USD
    Gewinn. Bei einer Ausstiegsregel, die bei 2x verkauft, ist ein
    Vielfaches von zweihunderttausend rechnerisch unmoeglich - da war
    nicht der Kurs so hoch, sondern der Einstiegswert nahe null.
    """

    def broken_position(self) -> Position:
        position = costless(
            mint="kaputt", symbol="X", group="CONTROL",
            entry_price=1e-12, opened_at=1.0,
        )
        position.exit_price = 2.06e-7
        position.closed_at = 2.0
        position.exit_reason = "ziel"
        return position

    def good_position(self, multiple: float = 2.0) -> Position:
        position = costless(
            mint="gut", symbol="Y", group="OK", entry_price=1.0, opened_at=1.0
        )
        position.exit_price = multiple
        position.closed_at = 2.0
        position.exit_reason = "ziel"
        return position

    def test_it_is_recognised(self, tmp_path):
        assert self.broken_position().broken
        assert not self.good_position().broken

    def test_it_is_kept_out_of_the_balance(self, tmp_path):
        book = PaperBook(tmp_path / "paper.json")
        book.positions.extend([self.good_position(), self.broken_position()])
        result = book.summary()
        assert result["result_usd"] == 100.0     # nur die echte Position
        assert result["broken"] == 1
        assert result["closed"] == 1

    def test_it_does_not_poison_the_group_comparison(self, tmp_path):
        """Eine einzige kaputte Position machte jede Gruppe daneben
        unlesbar - genau das ist die Auswertung, um die es geht."""
        book = PaperBook(tmp_path / "paper.json")
        book.positions.extend([self.good_position(), self.broken_position()])
        groups = book.by_group()
        assert groups["CONTROL"]["result_usd"] == 0.0
        assert groups["CONTROL"]["broken"] == 1
        assert groups["OK"]["result_usd"] == 100.0

    def test_it_is_not_counted_as_a_win(self, tmp_path):
        book = PaperBook(tmp_path / "paper.json")
        book.positions.append(self.broken_position())
        assert book.summary()["wins"] == 0

    def test_a_real_double_survives(self, tmp_path):
        """Die Grenze darf keine echten Treffer wegwerfen."""
        book = PaperBook(tmp_path / "paper.json")
        book.positions.append(self.good_position(2.4))
        assert book.summary()["broken"] == 0
        # 2.4x wird mit 2.0x gutgeschrieben - siehe TestDeckel.
        assert book.summary()["result_usd"] == 100.0

    def test_the_hit_rate_ignores_it(self, tmp_path):
        book = PaperBook(tmp_path / "paper.json")
        book.positions.extend([self.good_position(), self.broken_position()])
        assert "von 1 abgeschlossenen" in book.hit_rate_text()


class TestDeckel:
    """Die Simulation darf sich keine Ausfuehrung gutschreiben, die es nicht
    gibt.

    Aufgefallen an einer echten Tabelle: 69 abgeschlossene Trades zu 100 USD
    bei Ausstieg auf 2x - hoechstens +6.900 USD moeglich. Dastand +8.789 USD,
    bei 41% Treffern und einem Median von 0.52x. Die Regel loest bei 2x aus
    und notiert den Kurs, der beim Nachsehen dasteht; springt der weit
    darueber, wanderte der ganze Sprung in die Bilanz. Bekommen wuerde ihn
    niemand: bei ein paar tausend Dollar Liquiditaet zahlt einem das
    Orderbuch den Buchkurs nicht aus.
    """

    def closed(self, tmp_path, multiple: float) -> PaperBook:
        book = PaperBook(tmp_path / "paper.json")
        position = costless(
            mint="m", symbol="X", group="OK", entry_price=1.0, opened_at=1.0
        )
        position.exit_price = multiple
        position.closed_at = 2.0
        position.exit_reason = "ziel"
        book.positions.append(position)
        return book

    def test_the_overshoot_is_not_credited(self, tmp_path):
        assert self.closed(tmp_path, 9.0).summary()["result_usd"] == 100.0

    def test_a_normal_win_is_unaffected(self, tmp_path):
        assert self.closed(tmp_path, 2.0).summary()["result_usd"] == 100.0

    def test_losses_are_not_capped(self, tmp_path):
        """Nach unten ist derselbe Effekt real: wer unter die Grenze
        rutscht, verkauft tatsaechlich schlechter als geplant."""
        assert self.closed(tmp_path, 0.2).summary()["result_usd"] == -80.0

    def test_a_total_loss_stays_a_total_loss(self, tmp_path):
        assert self.closed(tmp_path, 0.0).summary()["result_usd"] == -100.0

    def test_the_arithmetic_limit_holds(self, tmp_path):
        """Der Test, der den Fehler gefunden haette.

        Mit Ausstieg bei 2x und festem Einsatz kann die Bilanz aus n
        geschlossenen Trades niemals mehr als n * Einsatz * (2 - 1)
        ergeben. Genau diese Grenze war in der echten Tabelle gerissen.
        """
        import random

        random.seed(3)
        book = PaperBook(tmp_path / "paper.json")
        for i in range(69):
            position = costless(
                mint=f"m{i}", symbol="X", group="AVOID",
                entry_price=1.0, opened_at=1.0,
            )
            # Auch mit ein paar kaputten Bezugspunkten dazwischen.
            position.exit_price = random.choice([0.6, 0.6, 2.05, 3.4, 91.0, 0.0])
            position.closed_at = 2.0
            position.exit_reason = "ziel"
            book.positions.append(position)

        result = book.summary()
        grenze = result["closed"] * DEFAULT_SIZE_USD * (TAKE_PROFIT - 1.0)
        assert result["result_usd"] <= grenze



class TestKosten:
    """Was zwischen Chart und Kontostand steht.

    Bis hierher rechnete die Bilanz, als koste Handeln nichts. Das ist bei
    einer Strategie, die auf 2x zielt, kein Rundungsfehler: gemessen an neun
    Token des early-Profils frisst allein der Swap-Weg im Median 4,4% -
    hochgerechnet auf eine echte Nacht mit 1.360 Positionen waren das
    5.053 USD, also mehr als ein Drittel des ausgewiesenen Verlusts.

    Zwei Posten stecken darin. Der **Swap-Weg** ist Gebuehr plus
    Preiseinfluss und wird von Jupiter gemessen; die **Netzgebuehr** ist
    eine Annahme. Beide stehen getrennt, damit sich die gemessene Zahl nicht
    hinter der geschaetzten versteckt.
    """

    def make(self, multiple: float, **kwargs) -> Position:
        position = Position(
            mint="m", symbol="X", group="OK", entry_price=1.0, opened_at=1.0, **kwargs
        )
        position.exit_price = multiple
        position.closed_at = 2.0
        position.exit_reason = "ziel"
        return position

    def test_a_win_is_smaller_than_the_chart_says(self):
        """100 USD auf 2x: der Kurs gibt +100 her, ankommen 90,80.

        Fast ein Zehntel des Gewinns bleibt im Pool und im Netz. Bei einer
        Strategie, die auf 2x zielt, ist das keine Nebensache.
        """
        position = self.make(2.0, retention=0.956, fee_usd=0.40)
        assert position.gross_result_usd() == pytest.approx(100.0)
        assert position.result_usd() == pytest.approx(90.8)

    def test_a_loss_is_bigger_than_the_chart_says(self):
        position = self.make(0.5, retention=0.956, fee_usd=0.40)
        assert position.gross_result_usd() == pytest.approx(-50.0)
        assert position.result_usd() == pytest.approx(-52.6)

    def test_costs_are_exactly_the_difference(self):
        """Sonst waere die getrennte Ausweisung eine zweite, eigene
        Rechnung - und irgendwann wichen beide voneinander ab."""
        for multiple in (0.0, 0.5, 1.0, 2.0, 9.0):
            position = self.make(multiple, retention=0.93, fee_usd=0.4)
            assert position.cost_usd() == pytest.approx(
                position.gross_result_usd() - position.result_usd()
            )

    def test_a_win_costs_more_than_a_loss(self):
        """Der Rueckweg bewegt bei 2x das Doppelte durch den Pool."""
        gewinn = self.make(2.0, retention=0.95, fee_usd=0.0)
        verlust = self.make(0.5, retention=0.95, fee_usd=0.0)
        assert gewinn.cost_usd() > verlust.cost_usd()

    def test_a_total_loss_still_costs_the_fee(self):
        """Wer bei null aussteigt, verliert den Einsatz und hat trotzdem
        zweimal Netzgebuehr gezahlt."""
        position = self.make(0.0, retention=0.956, fee_usd=0.40)
        assert position.result_usd() == pytest.approx(-100.4)

    def test_the_cap_applies_before_the_costs(self):
        """Sonst wuerden Kosten auf einen Kurs gerechnet, der nie
        ausgefuehrt worden waere."""
        assert self.make(9.0, retention=0.956, fee_usd=0.4).result_usd() == (
            pytest.approx(self.make(2.0, retention=0.956, fee_usd=0.4).result_usd())
        )

    def test_the_arithmetic_limit_still_holds(self, tmp_path):
        """Der Deckel darf durch die Kosten nicht durchlaessig werden - mit
        Kosten muss die Grenze sogar unterschritten werden."""
        book = PaperBook(tmp_path / "paper.json")
        for i in range(20):
            position = self.make(9.0, retention=0.956, fee_usd=0.4)
            position.mint = f"m{i}"
            book.positions.append(position)
        result = book.summary()
        grenze = result["closed"] * DEFAULT_SIZE_USD * (TAKE_PROFIT - 1.0)
        assert result["result_usd"] < grenze

    def test_an_open_position_already_carries_the_way_out(self):
        """Der Betrag beantwortet "was bekaeme ich, wenn ich jetzt
        verkaufe" - und dieses Verkaufen kostet noch etwas."""
        position = Position(
            mint="m", entry_price=1.0, opened_at=1.0, retention=0.9, fee_usd=0.4
        )
        assert position.result_usd(1.0) == pytest.approx(-10.4)


class TestGemessenerRueckweg:
    """Gemessen schlaegt geschaetzt - aber nur, wenn die Messung taugt."""

    def trip(self, retention: float | None):
        from xeno.sources.jupiter import RoundTrip

        if retention is None:
            return RoundTrip(lamports_in=1_000, error="keine Verkaufs-Route")
        return RoundTrip(
            buy_ok=True,
            sell_ok=True,
            lamports_in=1_000_000,
            lamports_out=int(1_000_000 * retention),
        )

    def test_the_measured_value_is_used(self, book):
        position = buy(book, costs=True, round_trip=self.trip(0.88))
        assert position.retention == pytest.approx(0.88)
        assert position.retention_measured is True

    def test_without_a_measurement_the_default_applies(self, book):
        from xeno.paper import DEFAULT_RETENTION

        position = buy(book, costs=True)
        assert position.retention == DEFAULT_RETENTION
        assert position.retention_measured is False

    def test_a_failed_trip_does_not_count_as_measured(self, book):
        """Sonst gaebe sich eine Schaetzung als Messung aus - und die Frage
        "wie belastbar ist die Kostenseite" waere nicht mehr zu beantworten."""
        from xeno.paper import DEFAULT_RETENTION

        position = buy(book, costs=True, round_trip=self.trip(None))
        assert position.retention == DEFAULT_RETENTION
        assert position.retention_measured is False

    def test_free_money_is_rejected(self, book):
        """Bei frischen Bonding-Curve-Pools liefert Jupiter Werte ueber 1.0,
        weil Kauf- und Verkaufsseite gegen unterschiedlich aktuelle
        Poolstaende gerechnet werden. Wer die uebernimmt, eroeffnet eine
        Position mit Gewinn, bevor sich der Kurs bewegt hat."""
        from xeno.paper import DEFAULT_RETENTION

        for wert in (1.0, 1.02, 1.4):
            book.positions.clear()
            position = buy(book, costs=True, round_trip=self.trip(wert))
            assert position.retention == DEFAULT_RETENTION, wert
            assert position.retention_measured is False, wert

    def test_a_bad_measurement_is_taken_seriously(self, book):
        """Wenn Jupiter sagt, es kaeme die Haelfte zurueck, ist das das
        Ergebnis und kein Ausreisser."""
        position = buy(book, costs=True, round_trip=self.trip(0.5))
        assert position.retention == pytest.approx(0.5)
        assert position.result_usd(1.0) == pytest.approx(-50.4)

    def test_the_report_hands_it_through(self, book):
        """Der Punkt der ganzen Aenderung - sonst greift sie im Betrieb
        nirgends."""
        from xeno.models import RiskReport, TokenCandidate

        report = RiskReport(
            mint="m",
            symbol="X",
            candidate=TokenCandidate(mint="m", symbol="X", price_usd=1.0),
            round_trip=self.trip(0.9),
        )
        position = book.enter_report(report, now=NOW)
        assert position.retention == pytest.approx(0.9)
        assert position.retention_measured is True

    def test_the_control_group_gets_the_default(self, book):
        """Abgelehnte Token werden nie tief geprueft, es gibt zu ihnen also
        nie eine Messung. Sie mit 0% Kosten zu rechnen waere die
        schlechteste Variante: dann saehe ausgerechnet der Massstab
        guenstiger aus als das, was er messen soll."""
        from xeno.paper import DEFAULT_RETENTION

        position = buy(book, costs=True, group="CONTROL")
        assert position.retention == DEFAULT_RETENTION


class TestKostenInDerBilanz:
    """Die Kosten stecken im Ergebnis - und stehen daneben noch einmal
    einzeln. Ohne das kann niemand sehen, wieviel an der Strategie liegt und
    wieviel am Handel selbst."""

    def filled(self, book):
        buy(book, "a", costs=True)
        buy(book, "b", costs=True)
        book.update({"a": 2.0, "b": 0.5}, now=NOW + 60)
        return book.summary()

    def test_gross_and_net_are_both_reported(self, book):
        result = self.filled(book)
        assert result["gross_result_usd"] == pytest.approx(50.0)
        assert result["result_usd"] < result["gross_result_usd"]

    def test_the_difference_is_the_reported_cost(self, book):
        result = self.filled(book)
        assert result["costs_usd"] == pytest.approx(
            result["gross_result_usd"] - result["result_usd"]
        )

    def test_the_fee_share_is_named_separately(self, book):
        """Die Gebuehr ist eine Annahme, der Swap-Weg gemessen. Zusammen
        ausgewiesen liesse sich das eine nicht vom anderen trennen."""
        from xeno.paper import DEFAULT_FEE_USD

        result = self.filled(book)
        assert result["fees_usd"] == pytest.approx(2 * DEFAULT_FEE_USD)
        assert result["fees_usd"] < result["costs_usd"]

    def test_how_many_were_measured_is_visible(self, book):
        from xeno.sources.jupiter import RoundTrip

        trip = RoundTrip(
            buy_ok=True, sell_ok=True, lamports_in=1_000_000, lamports_out=900_000
        )
        buy(book, "a", costs=True, round_trip=trip)
        buy(book, "b", costs=True)
        result = book.summary()
        assert result["measured"] == 1
        assert result["avg_retention"] == pytest.approx((0.9 + DEFAULT_RETENTION) / 2)

    def test_an_empty_book_says_nothing_about_costs(self, book):
        result = book.summary()
        assert result["costs_usd"] == 0
        assert result["measured"] == 0
        assert result["avg_retention"] is None

    def test_the_fee_can_be_changed(self, book, monkeypatch):
        """Die Gebuehr haengt an der Netzauslastung - wer eine bessere Zahl
        hat, soll sie einsetzen koennen."""
        monkeypatch.setenv("XENO_FEE_USD", "1.25")
        assert buy(book, costs=True).fee_usd == pytest.approx(1.25)

    def test_a_nonsense_fee_falls_back(self, book, monkeypatch):
        from xeno.paper import DEFAULT_FEE_USD

        monkeypatch.setenv("XENO_FEE_USD", "viel")
        assert buy(book, costs=True).fee_usd == DEFAULT_FEE_USD


class TestBuchPfad:
    """Das Papierbuch gehoert zur Zustandsdatei.

    Wer mit ``--state-file`` eine zweite Messreihe aufmacht, will sie
    getrennt auswerten. Lief der Papierhandel weiter in dasselbe Buch,
    mischten sich beide Versuche - und ausgerechnet die Trades-Tabelle,
    die eigentliche Antwort auf "taugt es etwas", waere ein Brei aus
    beidem.
    """

    def test_the_default_stays_where_it_was(self):
        """Sonst waere ein bestehendes Buch nach einem Update verwaist."""
        from xeno.paper import BOOK_FILE_NAME, book_path_for
        from xeno.paths import STATE_FILE_NAME, data_dir, target_path

        assert book_path_for(target_path(STATE_FILE_NAME)) == data_dir() / BOOK_FILE_NAME

    def test_an_own_state_gets_an_own_book(self, tmp_path):
        from xeno.paper import book_path_for

        got = book_path_for(tmp_path / "XENO-balanced.json")
        assert got == tmp_path / "XENO-balanced-paper.json"

    def test_two_experiments_do_not_share_a_book(self, tmp_path):
        from xeno.paper import book_path_for

        a = book_path_for(tmp_path / "early.json")
        b = book_path_for(tmp_path / "balanced.json")
        assert a != b

    def test_the_watcher_uses_it(self, tmp_path):
        """Der Punkt der ganzen Aenderung - sonst greift sie nirgends."""
        from xeno.config import Settings
        from xeno.watcher import Watcher
        from xeno.watchstate import WatchState

        state = WatchState(tmp_path / "balanced.json")
        watcher = Watcher(settings=Settings(), state=state, log=lambda _m: None)
        assert watcher.book.path == tmp_path / "balanced-paper.json"

    def test_positions_land_in_the_right_file(self, tmp_path):
        from xeno.paper import PaperBook, book_path_for

        book = PaperBook(book_path_for(tmp_path / "balanced.json"))
        book.positions.append(
            Position(mint="m", symbol="X", group="OK", entry_price=1.0, opened_at=1.0)
        )
        book.save(force=True)
        assert (tmp_path / "balanced-paper.json").is_file()
        assert not (tmp_path / "paper-trades.json").exists()
