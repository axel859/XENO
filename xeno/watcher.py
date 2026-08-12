"""Die Ueberwachungsschleife.

Sucht laufend neue Kandidaten und behaelt gleichzeitig eine selbst gepflegte
Watchlist im Blick. Gemeldet wird nur, was neu oder anders ist.

Die zwei Gestaltungsfragen, an denen ein Watcher scheitert oder taugt:

**Budget.** Ein Deep-Check kostet rund fuenf Requests. Wuerde jeder Durchlauf
alles pruefen, was der Vorfilter durchlaesst, waere das Kontingent in Minuten
weg. Deshalb ist die Zahl der Tiefpruefungen pro Durchlauf fest gedeckelt und
die Reihenfolge priorisiert: Watchlist zuerst, dann neue Token, dann faellige
Wiederholungen.

**Meldungsdisziplin.** Wer bei jedem Durchlauf denselben Token meldet, wird
nach zehn Minuten ignoriert. Gemeldet wird deshalb nur bei echtem
Zustandswechsel - und am dringendsten dann, wenn bei einem bereits bekannten
Token ein neuer kritischer Befund auftaucht. Das ist der Fall, in dem der Rug
gerade laeuft.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from .analyzer import TokenAnalyzer
from .config import Settings
from .discovery import Discovery, merge_candidates
from .models import Finding, RiskReport, Severity, TokenCandidate, Verdict
from .notify import Alert, AlertKind, ConsoleNotifier, Notifier
from .pipeline import rank_key
from .screen import screen_all
from .watchstate import TokenState, WatchState, is_better, is_worse

#: Urteile, die eine Erstmeldung wert sind. Alles darunter waere Rauschen -
#: die grosse Mehrheit neuer Token faellt durch.
ALERT_ON_NEW = frozenset({Verdict.OK, Verdict.CAUTION})


@dataclass
class CycleStats:
    discovered: int = 0
    passed_screen: int = 0
    checked: int = 0
    alerts: int = 0
    #: Nachtraeglich gemessene Kursverlaeufe frueher gepruefter Token.
    measured: int = 0
    #: Kandidaten, die aus dem Live-Strom kamen statt aus der Abfrage.
    live: int = 0
    errors: list[str] = field(default_factory=list)


def decide_alert(
    report: RiskReport,
    previous: TokenState | None,
    watchlisted: bool = False,
) -> Alert | None:
    """Entscheidet, ob dieses Ergebnis eine Meldung rechtfertigt."""
    verdict = report.verdict

    # Erstkontakt: nur melden, wenn der Token die Pruefungen auch besteht.
    if previous is None or previous.check_count == 0:
        if watchlisted or verdict in ALERT_ON_NEW:
            return Alert(
                kind=AlertKind.NEW,
                report=report,
                new_findings=_notable(report.findings),
                watchlisted=watchlisted,
            )
        return None

    previous_verdict = previous.verdict_enum
    known_codes = set(previous.finding_codes)
    fresh = [f for f in report.findings if f.code not in known_codes]

    # Dringendster Fall: ein kritischer Befund, den es vorher nicht gab.
    fresh_critical = [f for f in fresh if f.severity is Severity.CRITICAL]
    if fresh_critical:
        return Alert(
            kind=AlertKind.CRITICAL_CHANGE,
            report=report,
            previous_verdict=previous_verdict,
            new_findings=fresh_critical,
            watchlisted=watchlisted,
        )

    # Verschlechterung - relevant vor allem fuer gehaltene Token.
    if is_worse(verdict, previous_verdict):
        return Alert(
            kind=AlertKind.DEGRADED,
            report=report,
            previous_verdict=previous_verdict,
            new_findings=_notable(fresh),
            watchlisted=watchlisted,
        )

    # Verbesserung nur melden, wenn sie zu einem brauchbaren Urteil fuehrt
    # und darueber nicht schon einmal gemeldet wurde.
    if (
        is_better(verdict, previous_verdict)
        and verdict in ALERT_ON_NEW
        and previous.alerted_verdict != verdict.value
    ):
        return Alert(
            kind=AlertKind.IMPROVED,
            report=report,
            previous_verdict=previous_verdict,
            watchlisted=watchlisted,
        )

    return None


def _notable(findings: list[Finding]) -> list[Finding]:
    return [
        f
        for f in findings
        if f.severity in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
    ]


class Watcher:
    def __init__(
        self,
        settings: Settings | None = None,
        state: WatchState | None = None,
        notifier: Notifier | None = None,
        discovery: Discovery | None = None,
        analyzer: TokenAnalyzer | None = None,
        log=None,
        tracker=None,
        live=None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.state = state or WatchState()
        self.notifier = notifier or ConsoleNotifier()
        self.discovery = discovery or Discovery()
        # Beide teilen sich denselben GeckoTerminal-Zugang. Zwei getrennte
        # Clients wuerden ihr Tempo je fuer sich drosseln und zusammen das
        # Limit der API reissen - der Fehler, der schon einmal dazu gefuehrt
        # hat, dass die Suche ab dem dritten Durchlauf nichts mehr fand.
        self.analyzer = analyzer or TokenAnalyzer(
            self.settings, gecko=self.discovery.gecko
        )
        self.log = log or (lambda message: print(f"  {message}", file=sys.stderr))
        if tracker is None:
            from .follow import OutcomeTracker

            tracker = OutcomeTracker(self.state)
        self.tracker = tracker
        #: Optionaler Live-Strom. Ohne ihn arbeitet der Watcher wie bisher.
        self.live = live

    # -- Ein Durchlauf ----------------------------------------------------

    def build_queue(
        self,
        candidates: list[TokenCandidate],
        budget: int,
        now: float | None = None,
    ) -> list[tuple[str, TokenCandidate | None]]:
        """Stellt zusammen, was in diesem Durchlauf geprueft wird.

        Reihenfolge: Watchlist, dann neue Token, dann faellige Wiederholungen.
        Die Watchlist zuerst, weil dort eine Verschlechterung unmittelbar Geld
        kostet - ein verpasster Neuzugang dagegen nur eine Gelegenheit.
        """
        now = now or time.time()
        by_mint = {c.mint: c for c in candidates}
        queue: list[tuple[str, TokenCandidate | None]] = []
        seen: set[str] = set()

        def add(mint: str, candidate: TokenCandidate | None) -> None:
            if mint not in seen:
                seen.add(mint)
                queue.append((mint, candidate))

        for state in self.state.watchlist:
            if self.state.is_due(state.mint, now):
                add(state.mint, by_mint.get(state.mint))

        # Neue Kandidaten in der Reihenfolge des Profils - bei frischen Token
        # nach Beteiligung, sonst nach Groesse. Das entscheidet, welche das
        # knappe Budget bekommen, wenn mehr durchkommen als geprueft werden
        # koennen.
        fresh = [c for c in candidates if not self.state.known(c.mint)]
        fresh.sort(key=lambda c: rank_key(c, self.settings.profile), reverse=True)
        for candidate in fresh:
            add(candidate.mint, candidate)

        rechecks = [
            s
            for s in self.state.due_for_recheck(now)
            if not s.watchlisted and s.mint not in seen
        ]
        # Aussichtsreiche zuerst - bei knappem Budget lieber die pruefen,
        # bei denen eine Aenderung ueberhaupt interessant waere.
        rechecks.sort(key=lambda s: s.score, reverse=True)
        for state in rechecks:
            add(state.mint, by_mint.get(state.mint))

        return queue[:budget]

    def _collect_live(self, stats: CycleStats) -> list[TokenCandidate]:
        """Holt gereifte Token aus dem Live-Strom und ergaenzt Marktdaten."""
        from datetime import datetime, timezone

        min_age = self.settings.screen.min_age_minutes * 60.0
        matured = self.live.take_matured(min_age_seconds=min_age, limit=60)
        # Wer nach dem Vielfachen des Zielfensters immer noch wartet, wird
        # nicht mehr geprueft - sonst waechst die Liste unbegrenzt.
        self.live.drop_stale(self.settings.screen.max_age_hours * 3600)
        if not matured:
            return []

        candidates = [
            TokenCandidate(
                mint=token.mint,
                symbol=token.symbol,
                name=token.name,
                source="live:pumpfun",
                # Der genaueste Zeitpunkt, den es gibt - direkt vom Ereignis.
                created_at=datetime.fromtimestamp(token.seen_at, tz=timezone.utc),
            )
            for token in matured
        ]

        try:
            return self.analyzer.dexscreener.enrich_many(candidates)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"Marktdaten fuer Live-Token fehlgeschlagen: {exc}")
            return []

    def cycle(
        self,
        budget: int = 8,
        test_trade: bool = True,
        on_report=None,
        stop_event=None,
    ) -> CycleStats:
        """Ein vollstaendiger Durchlauf: suchen, filtern, pruefen, melden.

        ``on_report`` bekommt jedes Ergebnis - die Weboberflaeche haelt damit
        die vollstaendigen Befunde vor, die im Zustand nur verkuerzt liegen.

        ``stop_event`` wird zwischen den einzelnen Token geprueft. Ein
        Durchlauf kann mehrere Minuten dauern; ohne diese Pruefung wuerde ein
        Stopp erst danach greifen und die Oberflaeche haenge fest.
        """
        stats = CycleStats()
        now = time.time()

        profile = self.settings.profile
        try:
            candidates = self.discovery.collect(
                include_new=profile.include_new if profile else True,
                include_trending=profile.include_trending if profile else True,
                # Im Dauerbetrieb bewusst weniger Seiten als bei einem
                # einmaligen Scan - sonst sperrt die kostenlose API nach
                # wenigen Durchlaeufen.
                pages=profile.watch_pages if profile else 1,
                on_error=stats.errors.append,
            )
            stats.discovered = len(candidates)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"Discovery fehlgeschlagen: {exc}")
            candidates = []

        # Token aus dem Live-Strom dazunehmen. Sie kommen mit dem exakten
        # Geburtszeitpunkt und werden erst herausgegeben, wenn sie alt genug
        # fuer eine Beurteilung sind. Wo die Abfrage denselben Token spaeter
        # mit reicheren Daten liefert, gewinnt die vollstaendigere Fassung.
        if self.live is not None:
            live_candidates = self._collect_live(stats)
            if live_candidates:
                candidates = merge_candidates([candidates, live_candidates])
                stats.live = len(live_candidates)

        # Null Kandidaten ohne gemeldeten Fehler waere frueher stumm
        # geblieben - genau der Fall, der wie ein defekter Bot aussieht.
        if not candidates and not stats.errors:
            stats.errors.append(
                "Keine Kandidaten von der Discovery - Quelle liefert gerade nichts"
            )

        passed = [r.candidate for r in screen_all(candidates, self.settings.screen) if r.passed]
        stats.passed_screen = len(passed)

        for mint, candidate in self.build_queue(passed, budget, now):
            if stop_event is not None and stop_event.is_set():
                break
            previous = self.state.get(mint)
            watchlisted = bool(previous and previous.watchlisted)
            try:
                report = self.analyzer.analyze(
                    mint, candidate=candidate, test_trade=test_trade
                )
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{mint[:10]}: {exc}")
                continue

            stats.checked += 1
            if on_report is not None:
                try:
                    on_report(report)
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"on_report fehlgeschlagen: {exc}")

            alert = decide_alert(report, previous, watchlisted=watchlisted)
            # Erst nach der Entscheidung speichern - decide_alert vergleicht
            # gegen den vorherigen Stand.
            self.state.record(report, now=time.time())

            if alert is not None:
                self.notifier.send(alert)
                self.state.mark_alerted(mint, report.verdict)
                stats.alerts += 1

        # Nachverfolgung: was ist aus frueher geprueften Token geworden?
        # Kostet keine RPC-Anfragen und konkurriert damit nicht mit dem
        # Pruefbudget - DexScreener liefert 30 Kurse pro Request.
        if self.tracker is not None:
            follow = self.tracker.run(time.time(), on_error=stats.errors.append)
            stats.measured = follow.measured

        try:
            self.state.save()
        except OSError as exc:
            stats.errors.append(f"Zustand konnte nicht gespeichert werden: {exc}")

        return stats

    # -- Dauerbetrieb -----------------------------------------------------

    def run(
        self,
        interval: float = 60.0,
        budget: int = 8,
        test_trade: bool = True,
        max_cycles: int | None = None,
        stop_event=None,
        on_report=None,
        on_cycle=None,
    ) -> None:
        """Laeuft bis Strg-C oder bis ``stop_event`` gesetzt wird.

        Ein Fehler in einem Durchlauf beendet die Ueberwachung nicht - sonst
        stirbt der Watcher nachts an einem Netzwerkaussetzer und niemand
        bemerkt es.

        ``stop_event`` wird im Dashboard-Betrieb gebraucht: die Wartezeit
        zwischen den Durchlaeufen laeuft dann ueber ``Event.wait`` statt
        ``sleep``, damit ein Stopp sofort greift und nicht erst nach dem
        vollen Intervall.
        """
        self.log(
            f"Watcher laeuft. Intervall {interval:.0f}s, "
            f"max. {budget} Tiefpruefungen pro Durchlauf. Strg-C beendet."
        )
        if self.state.watchlist:
            self.log(f"{len(self.state.watchlist)} Token auf der Watchlist")

        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                if stop_event is not None and stop_event.is_set():
                    break

                started = time.monotonic()
                try:
                    stats = self.cycle(
                        budget=budget,
                        test_trade=test_trade,
                        on_report=on_report,
                        stop_event=stop_event,
                    )
                    self.log(
                        f"Durchlauf {cycles + 1}: {stats.discovered} gefunden, "
                        f"{stats.passed_screen} gefiltert, {stats.checked} geprueft, "
                        f"{stats.alerts} gemeldet"
                        + (f", {stats.live} live" if stats.live else "")
                        + (f", {stats.measured} nachverfolgt" if stats.measured else "")
                    )
                    for error in stats.errors:
                        self.log(f"! {error}")
                    if on_cycle is not None:
                        on_cycle(stats)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"! Durchlauf fehlgeschlagen: {exc}")

                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break

                remaining = interval - (time.monotonic() - started)
                if remaining > 0:
                    if stop_event is not None:
                        if stop_event.wait(remaining):
                            break
                    else:
                        time.sleep(remaining)
        except KeyboardInterrupt:
            self.log("Beendet.")
        finally:
            try:
                self.state.save()
            except OSError as exc:
                self.log(f"! Zustand konnte nicht gespeichert werden: {exc}")
