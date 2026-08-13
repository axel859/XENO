"""Tests der Aufwach-Erkennung.

Zwei Gruppen von Tests tragen hier die Last. ``TestArtefakte`` sichert die
beiden Faelle ab, die beim Messen an echten Daten aufgefallen sind und die
eine naive Umsetzung als Treffer melden wuerde - ein frischer Token, bei
dem der Faktor rechnerisch gar nicht anders sein kann, und ein toter Pool,
in dem ein einzelner Kauf 499465% Kursaenderung erzeugt. ``TestRuhe``
sichert die Gegenrichtung: was kein Aufwachen ist, darf keines melden,
sonst ist die Meldung wertlos.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from xeno.models import TokenCandidate
from xeno.wake import (
    COOLDOWN_SECONDS,
    MAX_BURST,
    MIN_AGE_MINUTES,
    MIN_BURST,
    Wake,
    WakeWatcher,
    burst_factor,
    detect,
)
from xeno.watchstate import TokenState, WatchState

#: Echte Uhrzeit als Bezugspunkt. ``TokenCandidate.age_minutes`` rechnet
#: gegen die Systemzeit - ein erfundenes "jetzt" in der Zukunft ergaebe ein
#: negatives Alter, und die Erkennung liefe nie an.
NOW = time.time()
MINT = "25a7whvEzPqceUt5vVSfxVbqzjJrg4oEqbCHvAsupump"
OTHER = "9tPeuW6hAw6YuKGypfuMwBrSrN2ktpdhqXQqxR9z5PCq"


def token(
    mint: str = MINT,
    volume_h1: float | None = 50_000.0,
    volume_h24: float | None = 100_000.0,
    change: float | None = 40.0,
    age_days: float = 5.0,
) -> TokenCandidate:
    created = datetime.fromtimestamp(NOW, tz=timezone.utc) - timedelta(days=age_days)
    return TokenCandidate(
        mint=mint,
        symbol="TEST",
        created_at=created,
        volume_h1_usd=volume_h1,
        volume_h24_usd=volume_h24,
        price_change_h1_pct=change,
        price_usd=0.0004,
    )


def known(
    mint: str = MINT,
    verdict: str = "CAUTION",
    woke_at: float = 0.0,
    first_seen: float = NOW - 5 * 86400,
) -> TokenState:
    return TokenState(
        mint=mint,
        symbol="TEST",
        first_seen=first_seen,
        last_checked=first_seen + 60,
        check_count=1,
        verdict=verdict,
        woke_at=woke_at,
    )


class TestBurst:
    def test_normal_token_sits_near_one(self):
        """Gleichmaessiger Handel: eine Stunde ist ein Vierundzwanzigstel."""
        assert burst_factor(1_000, 24_000) == 1.0

    def test_a_quiet_day_and_a_loud_hour(self):
        assert burst_factor(12_000, 24_000) == 12.0

    def test_everything_in_one_hour_is_the_maximum(self):
        """Mehr als das Tagesvolumen in einer Stunde gibt es nicht."""
        assert burst_factor(24_000, 24_000) == MAX_BURST

    def test_inconsistent_data_is_capped(self):
        assert burst_factor(50_000, 24_000) == MAX_BURST

    def test_without_a_day_there_is_no_reference(self):
        """None heisst 'keine Aussage', nicht 'unauffaellig'."""
        assert burst_factor(5_000, None) is None
        assert burst_factor(None, 24_000) is None
        assert burst_factor(5_000, 0) is None


class TestArtefakte:
    """Die beiden Faelle, die beim Messen an echten Daten aufgefallen sind."""

    def test_a_fresh_token_is_not_a_wake_up(self):
        """Unter 24h ist vol_h1 == vol_h24 - der Faktor ist dann immer 24.

        In der ersten Messung lagen saemtliche Neuzugaenge auf dem
        Maximalwert. Wiederaufwachen setzt ein vorheriges Leben voraus.
        """
        fresh = token(volume_h1=20_000, volume_h24=20_000, age_days=0.5)
        assert detect(fresh, known(first_seen=NOW - 12 * 3600), NOW) is None

    def test_one_buy_into_a_dead_pool_is_not_a_wake_up(self):
        """Echt gemessen: 421 USD Tagesumsatz, "+499465% in einer Stunde"."""
        dust = token(volume_h1=421, volume_h24=442, change=499_465.0)
        assert detect(dust, known(), NOW) is None

    def test_the_age_limit_is_a_full_day(self):
        assert MIN_AGE_MINUTES == 24 * 60


class TestRuhe:
    """Was kein Aufwachen ist, darf keines melden."""

    def test_a_normal_hour_is_not_a_wake_up(self):
        assert detect(token(volume_h1=5_000, volume_h24=100_000), known(), NOW) is None

    def test_volume_with_a_falling_price_is_a_sell_off(self):
        """Da verlaesst gerade jemand die Position - das Gegenteil."""
        assert detect(token(change=-30.0), known(), NOW) is None

    def test_volume_with_a_flat_price_is_not_enough(self):
        assert detect(token(change=1.0), known(), NOW) is None

    def test_missing_price_change_is_no_wake_up(self):
        """Fehlende Daten sind nie ein Treffer."""
        assert detect(token(change=None), known(), NOW) is None

    def test_missing_volume_is_no_wake_up(self):
        assert detect(token(volume_h1=None), known(), NOW) is None

    def test_an_unknown_token_is_not_a_wake_up(self):
        """Aufwachen kann nur, was XENO vorher schon kannte."""
        assert detect(token(), None, NOW) is None


class TestTreffer:
    def test_the_clear_case(self):
        result = detect(token(), known(), NOW)
        assert result is not None
        assert result.mint == MINT
        assert result.burst == 12.0

    def test_the_previous_verdict_travels_along(self):
        """Ohne es liest sich die Meldung wie ein neuer Fund."""
        result = detect(token(), known(verdict="AVOID"), NOW)
        assert result.previous_verdict == "AVOID"

    def test_a_written_off_token_can_still_wake_up(self):
        """Genau der Fall, um den es geht: abgehakt, und dann laeuft er."""
        state = known(verdict="AVOID")
        state.dead = True
        assert detect(token(), state, NOW) is not None

    def test_the_reasons_carry_the_numbers(self):
        result = detect(token(), known(), NOW)
        assert any("12x" in r for r in result.reasons)
        assert any("+40%" in r for r in result.reasons)

    def test_exactly_at_the_threshold_counts(self):
        at_limit = token(volume_h1=MIN_BURST * 4_000, volume_h24=24 * 4_000)
        assert detect(at_limit, known(), NOW) is not None

    def test_an_absurd_percentage_is_worded_not_printed(self):
        """Echt gemessen: 365.000 USD Stundenumsatz, "+497937%". Das
        Ereignis ist echt, die Zahl in der Meldung waere nur Rauschen -
        sie entsteht, weil der Vergleichskurs praktisch null war."""
        revived = token(volume_h1=365_000, volume_h24=370_000, change=497_937.0)
        result = detect(revived, known(), NOW)
        assert result is not None
        assert not any("497937" in r for r in result.reasons)
        assert any("praktisch still" in r for r in result.reasons)

    def test_just_below_the_threshold_does_not(self):
        below = token(volume_h1=(MIN_BURST - 0.5) * 4_000, volume_h24=24 * 4_000)
        assert detect(below, known(), NOW) is None


class TestSperrfrist:
    def test_a_running_move_is_reported_once(self):
        """Sonst meldet jeder Durchlauf denselben Token, stundenlang."""
        recent = known(woke_at=NOW - 600)
        assert detect(token(), recent, NOW) is None

    def test_after_the_cooldown_it_can_report_again(self):
        old = known(woke_at=NOW - COOLDOWN_SECONDS - 1)
        assert detect(token(), old, NOW) is not None


class FakeDex:
    """Gibt vorbereitete Marktdaten zurueck und merkt sich die Anfragen."""

    def __init__(self, tokens: list[TokenCandidate] | None = None) -> None:
        self.tokens = tokens or []
        self.asked: list[list[str]] = []
        self.error: Exception | None = None

    def enrich_many(self, candidates, batch_size: int = 30):
        self.asked.append([c.mint for c in candidates])
        if self.error is not None:
            raise self.error
        wanted = {c.mint for c in candidates}
        return [t for t in self.tokens if t.mint in wanted]


@pytest.fixture
def store(tmp_path):
    """Leerer Zustand auf einem eigenen Pfad.

    ``WatchState()`` ohne Pfad laedt die echte Datei des Benutzers - ein
    Test daraus haenge davon ab, was der Bot zuletzt gesehen hat.
    """
    def build(*entries: TokenState) -> WatchState:
        state = WatchState(tmp_path / "state.json")
        for entry in entries:
            state.tokens[entry.mint] = entry
        return state

    return build


class TestPuls:
    def test_finds_the_woken_token(self, store):
        watcher = WakeWatcher(FakeDex([token()]))
        found = watcher.scan(store(known()), NOW)
        assert [w.mint for w in found] == [MINT]

    def test_young_tokens_are_not_even_asked_about(self, store):
        """Fuer sie ist der Faktor nicht berechenbar - die Anfrage waere
        verschenkt, und in einem frischen Zustand sind das die meisten."""
        dex = FakeDex([token()])
        young = known(first_seen=NOW - 3600)
        WakeWatcher(dex).scan(store(young), NOW)
        assert dex.asked == []

    def test_the_strongest_comes_first(self, store):
        loud = token(mint=OTHER, volume_h1=90_000, volume_h24=100_000)
        quiet = token(mint=MINT, volume_h1=50_000, volume_h24=100_000)
        watcher = WakeWatcher(FakeDex([quiet, loud]))
        found = watcher.scan(store(known(MINT), known(OTHER)), NOW)
        assert [w.mint for w in found] == [OTHER, MINT]

    def test_the_number_per_cycle_is_capped(self, store):
        """Bei einem Ereignis, das den Markt bewegt, wachen viele auf -
        jede Pruefung danach kostet Credits."""
        mints = [f"{i}" * 40 for i in range(1, 6)]
        tokens = [token(mint=m) for m in mints]
        watcher = WakeWatcher(FakeDex(tokens), max_per_cycle=2)
        found = watcher.scan(store(*[known(m) for m in mints]), NOW)
        assert len(found) == 2

    def test_the_interval_is_respected(self, store):
        watcher = WakeWatcher(FakeDex([]))
        assert watcher.due(NOW)
        watcher.scan(store(known()), NOW)
        assert not watcher.due(NOW + 10)
        assert watcher.due(NOW + watcher.interval)

    def test_a_failing_source_is_not_swallowed(self, store):
        """Ein stiller Puls waere schlimmer als gar keiner - man verliesse
        sich auf eine Erkennung, die seit Tagen nichts abfragt."""
        dex = FakeDex([token()])
        dex.error = RuntimeError("DexScreener antwortet nicht")
        watcher = WakeWatcher(dex)
        try:
            watcher.scan(store(known()), NOW)
        except RuntimeError as exc:
            assert "antwortet nicht" in str(exc)
        else:
            raise AssertionError("Fehler wurde verschluckt")

    def test_an_empty_state_asks_nothing(self, store):
        dex = FakeDex([])
        assert WakeWatcher(dex).scan(store(), NOW) == []
        assert dex.asked == []


class TestSerialisierung:
    def test_the_wake_survives_as_json(self):
        data = Wake(
            mint=MINT, symbol="T", burst=8.0, volume_h1_usd=50_000,
            price_change_h1_pct=40.0, previous_verdict="AVOID", known_days=3.2,
        ).to_dict()
        assert data["burst"] == 8.0
        assert data["previous_verdict"] == "AVOID"

    def test_the_cooldown_survives_a_restart(self, tmp_path):
        """Sonst meldete jeder Neustart alle laufenden Bewegungen erneut."""
        import json

        path = tmp_path / "state.json"
        state = WatchState(path)
        state.tokens[MINT] = known()
        state.mark_woken(MINT, NOW)
        state.save()

        assert WatchState(path).get(MINT).woke_at == NOW
        assert json.loads(path.read_text())["tokens"][MINT]["woke_at"] == NOW


# -- Zusammenspiel mit dem Watcher -------------------------------------------


class FakeDiscovery:
    def collect(self, **kwargs):
        return []


class FakeAnalyzer:
    """Analyzer-Ersatz ohne Netzwerk. Merkt sich, was geprueft wurde."""

    def __init__(self) -> None:
        self.checked: list[str] = []

    def analyze(self, mint, candidate=None, test_trade=True):
        from conftest import make_mint_info

        from xeno.models import RiskReport

        self.checked.append(mint)
        return RiskReport(mint=mint, symbol="TEST", mint_info=make_mint_info())


class CollectingNotifier:
    def __init__(self) -> None:
        self.alerts: list = []

    def send(self, alert) -> None:
        self.alerts.append(alert)


class FakeWake:
    """Meldet vorbereitete Aufwacher, ohne etwas abzufragen."""

    def __init__(self, wakes: list[Wake]) -> None:
        self.wakes = wakes
        self.scans = 0

    def due(self, now=None) -> bool:
        return True

    def scan(self, state, now=None) -> list[Wake]:
        self.scans += 1
        return list(self.wakes)


def not_due(mint: str = MINT) -> TokenState:
    """Bekannter Token, der gerade erst geprueft wurde.

    Wichtig fuer die Aussagekraft der Tests darunter: ein Eintrag mit altem
    ``last_checked`` waere ohnehin zur Wiederholung faellig und wuerde auch
    ohne jede Aufwach-Erkennung geprueft. Der erste Anlauf dieser Tests war
    genau deshalb gruen - er hat nichts bewiesen.
    """
    state = known(mint)
    state.last_checked = time.time()
    return state


def a_wake(mint: str = MINT) -> Wake:
    return Wake(
        mint=mint,
        symbol="TEST",
        burst=12.0,
        volume_h1_usd=50_000.0,
        price_change_h1_pct=40.0,
        previous_verdict="AVOID",
        known_days=3.2,
        reasons=["Volumen 12x ueber dem eigenen Tagesschnitt"],
    )


def make_watcher(tmp_path, wakes: list[Wake]):
    from xeno.config import Settings
    from xeno.watcher import Watcher

    notifier = CollectingNotifier()
    analyzer = FakeAnalyzer()
    watcher = Watcher(
        settings=Settings(),
        state=WatchState(tmp_path / "s.json"),
        notifier=notifier,
        discovery=FakeDiscovery(),
        analyzer=analyzer,
        log=lambda _m: None,
        wake=FakeWake(wakes),
    )
    return watcher, notifier, analyzer


class TestImWatcher:
    def test_a_wake_up_gets_checked_even_without_candidates(self, tmp_path):
        """Der Punkt der ganzen Schicht: die Discovery findet ihn nicht -
        er ist ja nicht neu - und faellig ist er auch nicht."""
        watcher, _notifier, analyzer = make_watcher(tmp_path, [a_wake()])
        watcher.state.tokens[MINT] = not_due()

        watcher.cycle(budget=5, test_trade=False)
        assert analyzer.checked == [MINT]

    def test_the_alert_says_what_happened(self, tmp_path):
        from xeno.notify import AlertKind

        watcher, notifier, _ = make_watcher(tmp_path, [a_wake()])
        watcher.state.tokens[MINT] = not_due()
        watcher.cycle(budget=5, test_trade=False)

        alert = notifier.alerts[-1]
        assert alert.kind is AlertKind.WAKE
        assert any("12x" in line for line in alert.extra_lines)

    def test_the_previous_verdict_is_named_when_it_changed(self, tmp_path):
        """"War AVOID, ist jetzt CAUTION" ist die eigentliche Nachricht."""
        watcher, notifier, _ = make_watcher(tmp_path, [a_wake()])
        watcher.state.tokens[MINT] = not_due()
        watcher.cycle(budget=5, test_trade=False)

        lines = notifier.alerts[-1].extra_lines
        assert any("damals AVOID" in line for line in lines)

    def test_the_cooldown_starts_after_the_alert(self, tmp_path):
        """Sonst meldet der naechste Durchlauf denselben Token erneut."""
        watcher, _notifier, _ = make_watcher(tmp_path, [a_wake()])
        watcher.state.tokens[MINT] = not_due()
        watcher.cycle(budget=5, test_trade=False)
        assert watcher.state.get(MINT).woke_at > 0

    def test_the_cycle_counts_it(self, tmp_path):
        watcher, _notifier, _ = make_watcher(tmp_path, [a_wake()])
        watcher.state.tokens[MINT] = not_due()
        assert watcher.cycle(budget=5, test_trade=False).wakes == 1

    def test_a_broken_pulse_does_not_stop_the_cycle(self, tmp_path):
        """Eine Luecke ist besser als ein Abbruch - aber sie wird genannt."""
        watcher, _notifier, _ = make_watcher(tmp_path, [])

        class Broken(FakeWake):
            def scan(self, state, now=None):
                raise RuntimeError("DexScreener antwortet nicht")

        watcher.wake = Broken([])
        stats = watcher.cycle(budget=5, test_trade=False)
        assert any("Puls" in e for e in stats.errors)

    def test_no_candidates_means_no_check(self, tmp_path):
        """Gegenprobe: ohne Aufwacher prueft der Durchlauf nichts."""
        watcher, _notifier, analyzer = make_watcher(tmp_path, [])
        watcher.state.tokens[MINT] = not_due()
        watcher.cycle(budget=5, test_trade=False)
        assert analyzer.checked == []

    def test_a_silent_pulse_is_named(self, tmp_path):
        """``enrich_many`` faengt Ausfaelle je Block selbst ab. Kam auf
        hundert Fragen keine Antwort, heisst das nicht "nichts passiert"."""
        watcher, _notifier, _ = make_watcher(tmp_path, [])

        class Silent(FakeWake):
            blind = True

        watcher.wake = Silent([])
        stats = watcher.cycle(budget=5, test_trade=False)
        assert any("keine Entwarnung" in e for e in stats.errors)
