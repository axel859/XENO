"""Tests fuer den Watch-Modus.

Schwerpunkt liegt auf den Regeln, an denen ein Watcher praktisch scheitert:
Doppelmeldungen, verpasste Verschlechterungen und ein Budget, das sich an den
falschen Token verbraucht.
"""

from __future__ import annotations

import json
import time

from conftest import MINT, make_candidate, make_mint_info

from xeno.models import Finding, RiskReport, Severity, Verdict
from xeno.notify import Alert, AlertKind, ConsoleNotifier, MultiNotifier, TelegramNotifier
from xeno.watcher import Watcher, decide_alert
from xeno.watchstate import (
    WATCHLIST_MAX_INTERVAL,
    TokenState,
    WatchState,
    is_better,
    is_worse,
    recheck_interval,
)

OTHER_MINT = "So11111111111111111111111111111111111111112"


def make_report(
    verdict_findings: list[Finding] | None = None,
    mint: str = MINT,
    symbol: str = "TEST",
    candidate=None,
) -> RiskReport:
    report = RiskReport(
        mint=mint, symbol=symbol, mint_info=make_mint_info(), candidate=candidate
    )
    report.findings = verdict_findings or [
        Finding(check="t", code="clean", severity=Severity.INFO, message="ok")
    ]
    return report


def f(severity: Severity, code: str) -> Finding:
    return Finding(check="t", code=code, severity=severity, message=f"befund {code}")


class TestRecheckInterval:
    def test_young_tokens_are_checked_often(self):
        assert recheck_interval(10) == 5 * 60

    def test_older_tokens_are_checked_rarely(self):
        assert recheck_interval(5 * 24 * 60) == 4 * 3600

    def test_interval_grows_monotonically_with_age(self):
        ages = [5, 90, 12 * 60, 5 * 24 * 60]
        intervals = [recheck_interval(a) for a in ages]
        assert intervals == sorted(intervals)

    def test_watchlist_is_capped(self):
        """Gehaltene Token muessen haeufig geprueft werden, egal wie alt."""
        assert recheck_interval(5 * 24 * 60, watchlisted=True) == WATCHLIST_MAX_INTERVAL

    def test_unknown_age_gets_a_sane_default(self):
        assert 0 < recheck_interval(None) <= 3600


class TestVerdictComparison:
    def test_ordering(self):
        assert is_worse(Verdict.RISKY, Verdict.OK)
        assert is_better(Verdict.OK, Verdict.RISKY)
        assert not is_worse(Verdict.OK, Verdict.OK)

    def test_unknown_is_never_a_change(self):
        """UNKNOWN heisst 'nicht bewertet' - daraus folgt keine Richtung."""
        assert not is_worse(Verdict.UNKNOWN, Verdict.OK)
        assert not is_better(Verdict.OK, Verdict.UNKNOWN)


class TestAlertDecision:
    def test_new_good_token_is_reported(self):
        alert = decide_alert(make_report(), previous=None)
        assert alert is not None
        assert alert.kind is AlertKind.NEW

    def test_new_bad_token_is_silent(self):
        report = make_report([f(Severity.CRITICAL, "mint_authority_active")])
        assert report.verdict is Verdict.AVOID
        assert decide_alert(report, previous=None) is None

    def test_new_bad_token_on_watchlist_is_reported(self):
        """Bei bewusst beobachteten Token zaehlt auch die schlechte Nachricht."""
        report = make_report([f(Severity.CRITICAL, "mint_authority_active")])
        alert = decide_alert(report, previous=None, watchlisted=True)
        assert alert is not None

    def test_unchanged_token_is_silent(self):
        """Der wichtigste Fall - sonst meldet der Watcher im Minutentakt dasselbe."""
        previous = TokenState(
            mint=MINT,
            check_count=3,
            verdict=Verdict.OK.value,
            finding_codes=["clean"],
            alerted_verdict=Verdict.OK.value,
        )
        assert decide_alert(make_report(), previous) is None

    def test_new_critical_finding_is_reported(self):
        """LP war gelockt, ist es nicht mehr - der Rug laeuft gerade."""
        previous = TokenState(
            mint=MINT,
            check_count=2,
            verdict=Verdict.OK.value,
            finding_codes=["lp_locked"],
            alerted_verdict=Verdict.OK.value,
        )
        report = make_report([f(Severity.CRITICAL, "lp_unlocked")])
        alert = decide_alert(report, previous)
        assert alert is not None
        assert alert.kind is AlertKind.CRITICAL_CHANGE
        assert [x.code for x in alert.new_findings] == ["lp_unlocked"]

    def test_known_critical_is_not_reported_again(self):
        previous = TokenState(
            mint=MINT,
            check_count=2,
            verdict=Verdict.AVOID.value,
            finding_codes=["lp_unlocked"],
            alerted_verdict=Verdict.AVOID.value,
        )
        report = make_report([f(Severity.CRITICAL, "lp_unlocked")])
        assert decide_alert(report, previous) is None

    def test_degradation_is_reported(self):
        previous = TokenState(
            mint=MINT,
            check_count=2,
            verdict=Verdict.OK.value,
            finding_codes=["clean"],
            alerted_verdict=Verdict.OK.value,
        )
        report = make_report([f(Severity.HIGH, "top10_concentration")])
        alert = decide_alert(report, previous)
        assert alert is not None
        assert alert.kind is AlertKind.DEGRADED
        assert alert.previous_verdict is Verdict.OK

    def test_improvement_is_reported_once(self):
        previous = TokenState(
            mint=MINT,
            check_count=2,
            verdict=Verdict.CAUTION.value,
            finding_codes=["few_holders"],
            alerted_verdict=Verdict.CAUTION.value,
        )
        alert = decide_alert(make_report(), previous)
        assert alert is not None and alert.kind is AlertKind.IMPROVED

        # Nach der Meldung darf derselbe Sprung nicht erneut gemeldet werden.
        previous.verdict = Verdict.CAUTION.value
        previous.alerted_verdict = Verdict.OK.value
        assert decide_alert(make_report(), previous) is None


class TestWatchState:
    def test_roundtrip_through_disk(self, tmp_path):
        path = tmp_path / "state.json"
        state = WatchState(path)
        state.record(make_report(candidate=make_candidate()))
        state.add_to_watchlist(OTHER_MINT, symbol="SOL")
        state.save()

        reloaded = WatchState(path)
        assert reloaded.get(MINT) is not None
        assert reloaded.get(MINT).verdict == Verdict.OK.value
        assert [s.mint for s in reloaded.watchlist] == [OTHER_MINT]

    def test_corrupt_state_file_does_not_crash(self, tmp_path):
        """Ein kaputter Zustand darf den Watcher nicht am Start hindern."""
        path = tmp_path / "state.json"
        path.write_text("{kaputt", encoding="utf-8")
        assert WatchState(path).tokens == {}

    def test_unknown_fields_are_ignored(self, tmp_path):
        """Aeltere oder neuere Dateiversionen duerfen nicht zum Absturz fuehren."""
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"tokens": {MINT: {"mint": MINT, "erfundenes_feld": 1}}}),
            encoding="utf-8",
        )
        assert WatchState(path).get(MINT) is not None

    def test_save_is_atomic_and_leaves_no_temp_files(self, tmp_path):
        state = WatchState(tmp_path / "state.json")
        state.record(make_report())
        state.save()
        state.save()
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_unknown_token_is_always_due(self, tmp_path):
        assert WatchState(tmp_path / "s.json").is_due("neuer-mint")

    def test_recently_checked_token_is_not_due(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        entry = state.record(make_report(candidate=make_candidate(age_minutes=10)))
        assert not state.is_due(entry.mint)

    def test_dead_token_is_skipped(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(make_report([f(Severity.CRITICAL, "mint_authority_active")]))
        entry = state.get(MINT)
        assert entry.dead is True

        entry.last_checked = time.time() - 10 * 3600
        assert not state.is_due(MINT)

    def test_watchlisted_dead_token_is_still_checked(self, tmp_path):
        """Wer den Token haelt, will auch bei schlechtem Stand Bescheid wissen."""
        state = WatchState(tmp_path / "s.json")
        state.record(make_report([f(Severity.CRITICAL, "lp_unlocked")]))
        state.add_to_watchlist(MINT)
        state.get(MINT).last_checked = time.time() - 3600
        assert state.is_due(MINT)

    def test_prune_keeps_watchlist(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(make_report())
        state.get(MINT).last_checked = time.time() - 30 * 86400
        state.add_to_watchlist(MINT)
        assert state.prune(max_age_days=7) == 0
        assert state.known(MINT)

    def test_prune_removes_stale_entries(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(make_report())
        state.get(MINT).last_checked = time.time() - 30 * 86400
        assert state.prune(max_age_days=7) == 1
        assert not state.known(MINT)


class FakeDiscovery:
    def __init__(self, candidates):
        self.candidates = candidates

    def collect(self, **kwargs):
        return list(self.candidates)


class FakeAnalyzer:
    def __init__(self, reports: dict[str, RiskReport]):
        self.reports = reports
        self.calls: list[str] = []

    def analyze(self, mint, candidate=None, test_trade=True):
        self.calls.append(mint)
        report = self.reports.get(mint) or make_report(mint=mint)
        report.candidate = candidate
        return report


class CollectingNotifier:
    def __init__(self):
        self.alerts: list[Alert] = []

    def send(self, alert):
        self.alerts.append(alert)


def make_watcher(tmp_path, candidates, reports=None):
    from xeno.config import Settings

    notifier = CollectingNotifier()
    analyzer = FakeAnalyzer(reports or {})
    watcher = Watcher(
        settings=Settings(),
        state=WatchState(tmp_path / "s.json"),
        notifier=notifier,
        discovery=FakeDiscovery(candidates),
        analyzer=analyzer,
        log=lambda _m: None,
    )
    return watcher, notifier, analyzer


class TestWatcherCycle:
    def test_reports_new_token_then_stays_quiet(self, tmp_path):
        candidate = make_candidate()
        watcher, notifier, _ = make_watcher(tmp_path, [candidate])

        watcher.cycle(budget=5, test_trade=False)
        assert len(notifier.alerts) == 1

        # Zweiter Durchlauf: nichts hat sich geaendert -> keine Meldung.
        watcher.state.get(MINT).last_checked = 0  # faellig machen
        watcher.cycle(budget=5, test_trade=False)
        assert len(notifier.alerts) == 1

    def test_budget_is_respected(self, tmp_path):
        candidates = [
            make_candidate(mint=f"mint{i}", symbol=f"T{i}") for i in range(20)
        ]
        watcher, _, analyzer = make_watcher(tmp_path, candidates)
        watcher.cycle(budget=3, test_trade=False)
        assert len(analyzer.calls) == 3

    def test_watchlist_is_checked_first(self, tmp_path):
        candidates = [make_candidate(mint=f"mint{i}", symbol=f"T{i}") for i in range(10)]
        watcher, _, analyzer = make_watcher(tmp_path, candidates)
        watcher.state.add_to_watchlist("mint9")

        watcher.cycle(budget=2, test_trade=False)
        assert analyzer.calls[0] == "mint9"

    def test_analysis_error_does_not_stop_the_cycle(self, tmp_path):
        candidates = [make_candidate(mint="bad"), make_candidate(mint="good")]
        watcher, _, analyzer = make_watcher(tmp_path, candidates)

        original = analyzer.analyze

        def flaky(mint, **kwargs):
            if mint == "bad":
                raise RuntimeError("RPC weg")
            return original(mint, **kwargs)

        analyzer.analyze = flaky
        stats = watcher.cycle(budget=5, test_trade=False)
        assert stats.checked == 1
        assert stats.errors

    def test_discovery_failure_is_survived(self, tmp_path):
        watcher, _, _ = make_watcher(tmp_path, [])

        def boom(**kwargs):
            raise RuntimeError("Netz weg")

        watcher.discovery.collect = boom
        stats = watcher.cycle(budget=5, test_trade=False)
        assert stats.errors and stats.checked == 0

    def test_state_persists_between_cycles(self, tmp_path):
        watcher, _, _ = make_watcher(tmp_path, [make_candidate()])
        watcher.cycle(budget=5, test_trade=False)
        assert WatchState(tmp_path / "s.json").known(MINT)


class TestNotifiers:
    def test_console_output_contains_the_essentials(self, capsys):
        ConsoleNotifier(color=False).send(
            Alert(kind=AlertKind.NEW, report=make_report(symbol="ABC"))
        )
        out = capsys.readouterr().out
        assert "ABC" in out and "Neuer Kandidat" in out and MINT in out

    def test_broken_channel_does_not_block_the_others(self, capsys):
        class Broken:
            def send(self, alert):
                raise RuntimeError("kaputt")

        good = CollectingNotifier()
        MultiNotifier([Broken(), good]).send(
            Alert(kind=AlertKind.NEW, report=make_report())
        )
        assert len(good.alerts) == 1

    def test_telegram_escapes_html(self):
        report = make_report(symbol="<b>böse</b>")
        text = TelegramNotifier("t", "c")._format(
            Alert(kind=AlertKind.NEW, report=report)
        )
        assert "&lt;b&gt;" in text
        assert "<b>böse</b>" not in text

    def test_telegram_failure_is_swallowed(self, monkeypatch):
        """Ein Telegram-Ausfall darf die Ueberwachung nicht beenden."""
        errors = []
        notifier = TelegramNotifier("t", "c", on_error=errors.append)

        def boom(*args, **kwargs):
            raise RuntimeError("offline")

        monkeypatch.setattr(notifier.http, "post", boom)
        assert notifier.send_text("hallo") is False
        assert errors

    def test_from_env_requires_both_values(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert TelegramNotifier.from_env() is None


class TestSilentFailures:
    """Der Fall aus dem echten Betrieb: das Log zeigte neun Durchlaeufe mit
    '0 gefunden, 0 geprueft, 0 gemeldet' - ohne jeden Hinweis darauf, dass die
    Datenquelle wegen Ueberschreitung des Anfragelimits gar nichts lieferte.
    Ein gedrosselter Zugang sah damit genauso aus wie ein ruhiger Markt."""

    def test_discovery_reports_failures(self):
        from xeno.discovery import Discovery

        class Broken:
            def new_pools(self, pages=1):
                raise RuntimeError("HTTP 429 bei api.geckoterminal.com")

            def trending_pools(self, pages=1):
                return []

        errors: list[str] = []
        result = Discovery(gecko=Broken()).collect(on_error=errors.append)
        assert result == []
        assert errors, "Ein Ausfall der Quelle muss gemeldet werden"

    def test_rate_limit_gets_an_actionable_message(self):
        from xeno.discovery import Discovery

        class Limited:
            def new_pools(self, pages=1):
                raise RuntimeError("HTTP 429 bei api.geckoterminal.com")

            def trending_pools(self, pages=1):
                return []

        errors: list[str] = []
        Discovery(gecko=Limited()).collect(on_error=errors.append)
        assert "Anfragelimit" in errors[0]
        assert "--pages" in errors[0] or "Intervall" in errors[0]

    def test_one_broken_source_does_not_stop_the_other(self):
        from conftest import make_candidate

        from xeno.discovery import Discovery

        class HalfBroken:
            def new_pools(self, pages=1):
                raise RuntimeError("kaputt")

            def trending_pools(self, pages=1):
                return [make_candidate()]

        errors: list[str] = []
        result = Discovery(gecko=HalfBroken()).collect(
            include_trending=True, on_error=errors.append
        )
        assert len(result) == 1
        assert errors

    def test_empty_result_is_flagged(self, tmp_path):
        """Auch ohne Ausnahme darf ein leerer Durchlauf nicht kommentarlos
        bleiben - sonst sieht ein defekter Zugang aus wie Marktruhe."""
        watcher, _, _ = make_watcher(tmp_path, [])
        stats = watcher.cycle(budget=5, test_trade=False)
        assert stats.discovered == 0
        assert stats.errors

    def test_watcher_uses_the_smaller_page_count(self, tmp_path):
        """Im Dauerbetrieb weniger Seiten - sonst sperrt die Quelle."""
        from xeno.config import Settings
        from xeno.profiles import EARLY

        seen: dict[str, int] = {}

        class Counting:
            def collect(self, pages=1, **kwargs):
                seen["pages"] = pages
                return []

        watcher, _, _ = make_watcher(tmp_path, [])
        watcher.settings = Settings.from_env("early")
        watcher.discovery = Counting()
        watcher.cycle(budget=1, test_trade=False)

        assert seen["pages"] == EARLY.watch_pages
        assert seen["pages"] < EARLY.pages


class TestMeldungsfilter:
    """Was eine Unterbrechung wert ist - und was nur in die Liste gehoert.

    In einer Nacht kamen ueber hundert Meldungen an. Wer hundert bekommt,
    liest keine davon - auch die drei nicht, auf die es ankam.
    """

    def make(self, kind):
        from xeno.notify import Alert

        return Alert(kind=kind, report=make_report())

    def test_a_call_gets_through(self):
        from xeno.notify import AlertKind, OnlyImportant

        inner = CollectingNotifier()
        OnlyImportant(inner).send(self.make(AlertKind.CALL))
        assert len(inner.alerts) == 1

    def test_a_critical_change_gets_through(self):
        from xeno.notify import AlertKind, OnlyImportant

        inner = CollectingNotifier()
        OnlyImportant(inner).send(self.make(AlertKind.CRITICAL_CHANGE))
        assert len(inner.alerts) == 1

    def test_a_wake_up_gets_through(self):
        from xeno.notify import AlertKind, OnlyImportant

        inner = CollectingNotifier()
        OnlyImportant(inner).send(self.make(AlertKind.WAKE))
        assert len(inner.alerts) == 1

    def test_an_ordinary_find_stays_quiet(self):
        """Der haeufigste Anlass - und der, der die Nacht geflutet hat."""
        from xeno.notify import AlertKind, OnlyImportant

        inner = CollectingNotifier()
        OnlyImportant(inner).send(self.make(AlertKind.NEW))
        OnlyImportant(inner).send(self.make(AlertKind.IMPROVED))
        OnlyImportant(inner).send(self.make(AlertKind.DEGRADED))
        assert inner.alerts == []

    def test_the_dashboard_still_sees_everything(self, tmp_path):
        """Der Filter haengt vor Telegram und Systemmeldungen, nicht vor
        der Oberflaeche - dort ist eine lange Liste der Zweck."""
        from xeno.config import Settings
        from xeno.notify import AlertKind
        from xeno.server import build_server
        from xeno.watchstate import WatchState

        httpd, app, watcher_thread = build_server(
            host="127.0.0.1", port=0, settings=Settings(),
            state=WatchState(tmp_path / "s.json"), use_telegram=False,
        )
        try:
            watcher_thread.watcher.notifier.send(self.make(AlertKind.NEW))
            assert len(app.alerts) == 1
        finally:
            httpd.server_close()


class TestMarktdatenImZustand:
    """Die Anzeigewerte muessen den Bericht ueberleben.

    Die vollstaendigen Berichte liegen nur im Speicher und fallen nach 300
    Eintraegen hinten heraus - erst recht bei einem Neustart. Vorher stand
    auf jeder aelteren Karte im Dashboard nur noch "-", obwohl der Token
    laengst geprueft war.
    """

    def test_the_numbers_are_kept(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        report = make_report()
        report.candidate = make_candidate()
        state.record(report)

        market = state.get(MINT).market
        assert market["liquidity_usd"] == report.candidate.liquidity_usd
        assert market["volume_h1_usd"] == report.candidate.volume_h1_usd

    def test_they_survive_a_restart(self, tmp_path):
        path = tmp_path / "s.json"
        state = WatchState(path)
        report = make_report()
        report.candidate = make_candidate()
        state.record(report)
        state.save()

        assert WatchState(path).get(MINT).market["liquidity_usd"] is not None

    def test_only_the_display_values_are_kept(self, tmp_path):
        """Das steht fuer jeden je gesehenen Token in der Datei - der ganze
        Kandidat waere ueber Wochen zu viel."""
        from xeno.watchstate import MARKET_KEEP

        state = WatchState(tmp_path / "s.json")
        report = make_report()
        report.candidate = make_candidate()
        state.record(report)
        assert set(state.get(MINT).market) <= set(MARKET_KEEP)

    def test_a_token_without_market_data_stays_empty(self, tmp_path):
        state = WatchState(tmp_path / "s.json")
        state.record(make_report())
        assert state.get(MINT).market == {}

    def test_the_dashboard_falls_back_to_them(self, tmp_path):
        """Ohne Bericht im Speicher liefert die API trotzdem Zahlen."""
        from xeno.config import Settings
        from xeno.server import build_server

        state = WatchState(tmp_path / "s.json")
        report = make_report()
        report.candidate = make_candidate()
        state.record(report)

        httpd, app, _thread = build_server(
            host="127.0.0.1", port=0, settings=Settings(), state=state,
            use_telegram=False,
        )
        try:
            handler = httpd.RequestHandlerClass
            entry = next(e for e in handler._tokens(handler) if e["mint"] == MINT)
            assert entry["market"]["liquidity_usd"] is not None
        finally:
            httpd.server_close()


class TestAusblenden:
    """Ausblenden statt Loeschen.

    Der Wunsch war "die Liste leer, aber im Hintergrund weiterlaufen". Der
    wichtigste Teil dieser Tests ist deshalb nicht, dass etwas verschwindet
    - sondern dass alles, was im Hintergrund passiert, weiterlaeuft.
    Loeschen wuerde die Messreihe zerstoeren, aus der sich ueberhaupt erst
    beantworten laesst, ob die Urteile etwas taugen.
    """

    def filled(self, tmp_path, count: int = 5) -> WatchState:
        state = WatchState(tmp_path / "s.json")
        for i in range(count):
            mint = f"{i}" * 40
            state.tokens[mint] = TokenState(
                mint=mint, symbol=f"T{i}", first_seen=time.time() - 86400,
                last_checked=time.time() - 7200, check_count=1, verdict="AVOID",
                baseline_at=time.time() - 86400, baseline_price_usd=1.0,
                first_verdict="AVOID",
            )
        return state

    def test_hiding_one(self, tmp_path):
        state = self.filled(tmp_path)
        assert state.set_hidden("0" * 40)
        assert state.get("0" * 40).hidden

    def test_bringing_it_back(self, tmp_path):
        state = self.filled(tmp_path)
        state.set_hidden("0" * 40)
        state.set_hidden("0" * 40, False)
        assert not state.get("0" * 40).hidden

    def test_an_unknown_mint_is_no_crash(self, tmp_path):
        assert not self.filled(tmp_path).set_hidden("gibtsnicht" * 4)

    def test_hiding_all(self, tmp_path):
        state = self.filled(tmp_path)
        assert state.hide_all() == 5
        assert all(s.hidden for s in state.tokens.values())

    def test_the_watchlist_is_spared(self, tmp_path):
        """Was jemand ausdruecklich beobachten wollte, mit auszublenden
        waere die aergerlichste Variante von "aufgeraeumt"."""
        state = self.filled(tmp_path)
        state.tokens["0" * 40].watchlisted = True
        state.hide_all()
        assert not state.get("0" * 40).hidden

    def test_hiding_twice_counts_once(self, tmp_path):
        state = self.filled(tmp_path)
        state.hide_all()
        assert state.hide_all() == 0

    def test_it_survives_a_restart(self, tmp_path):
        state = self.filled(tmp_path)
        state.hide_all()
        state.save()
        assert WatchState(tmp_path / "s.json").get("0" * 40).hidden

    # -- der eigentliche Punkt: im Hintergrund laeuft alles weiter --------

    def test_it_is_still_rechecked(self, tmp_path):
        state = self.filled(tmp_path)
        state.hide_all()
        due = [s.mint for s in state.due_for_recheck(time.time())]
        assert len(due) == 5

    def test_the_price_is_still_measured(self, tmp_path):
        """``xeno stats`` lebt von diesen Messungen."""
        from xeno.follow import due_measurements

        state = self.filled(tmp_path)
        state.hide_all()
        pending, _missed = due_measurements(state, time.time())
        assert pending

    def test_it_can_still_wake_up(self, tmp_path):
        """Ein ausgeblendeter Token, der ploetzlich laeuft, soll sich
        melden - sonst waere Ausblenden doch ein Loeschen."""
        from xeno.wake import WakeWatcher

        state = self.filled(tmp_path)
        state.hide_all()
        assert len(WakeWatcher().candidates(state, time.time())) == 5

    def test_tokens_found_later_are_still_shown(self, tmp_path):
        """Die Frage, die sich beim Leeren sofort stellt: bleibt die Liste
        dann fuer immer leer?

        Nein - ``hide_all`` fasst nur an, was in dem Moment existiert. Jeder
        danach gefundene Token startet sichtbar. Waere es anders, waere der
        Knopf eine Falle: einmal gedrueckt, und der Bot arbeitet fuer
        niemanden mehr sichtbar.
        """
        state = self.filled(tmp_path)
        state.hide_all()
        state.record(make_report(mint="neu" + "9" * 37))
        assert not state.get("neu" + "9" * 37).hidden

    def test_the_history_is_untouched(self, tmp_path):
        state = self.filled(tmp_path)
        state.hide_all()
        entry = state.get("0" * 40)
        assert entry.baseline_price_usd == 1.0
        assert entry.first_verdict == "AVOID"
        assert entry.check_count == 1

    def test_only_important_narrows_but_keeps_calls(self, tmp_path):
        """``--only-important`` engt auf den einen Fall ein, bei dem gerade
        Geld verloren geht - schluckt aber keinen Call mehr."""
        from xeno.config import Settings
        from xeno.notify import AlertKind
        from xeno.server import build_server
        from xeno.watchstate import WatchState

        httpd, app, thread = build_server(
            host="127.0.0.1", port=0, settings=Settings(),
            state=WatchState(tmp_path / "s.json"), use_telegram=False,
            use_desktop=False, only_important=True,
        )
        try:
            collected = CollectingNotifier()
            wrapper = next(
                n for n in thread.watcher.notifier.notifiers
                if hasattr(n, "kinds")
            )
            wrapper.inner = collected
            from xeno.notify import Alert

            for kind in (AlertKind.CRITICAL_CHANGE, AlertKind.CALL, AlertKind.NEW):
                thread.watcher.notifier.send(Alert(kind=kind, report=make_report()))
            kinds = [a.kind for a in collected.alerts]
            assert AlertKind.CRITICAL_CHANGE in kinds
            assert AlertKind.NEW not in kinds
        finally:
            httpd.server_close()
