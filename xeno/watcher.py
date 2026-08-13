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

import random
import sys
import time
from dataclasses import dataclass, field

from .analyzer import TokenAnalyzer
from .calls import worth_calling
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

#: So viele der im Vorfilter abgelehnten Token werden je Durchlauf als
#: Vergleichsstichprobe mitgenommen. Kostet nichts - die Kursabfrage laeuft
#: ohnehin und liefert dreissig Kurse je Anfrage.
CONTROL_SAMPLE = 3


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
    #: Verbrauchte Credits in diesem Durchlauf. Das Kontingent laesst sich
    #: sonst nur auf der Webseite des Anbieters ablesen - und faellt dort erst
    #: auf, wenn es leer ist.
    credits: int = 0
    #: Wallet-Abfragen, die aus dem Gedaechtnis kamen statt aus dem Netz.
    api_saved: int = 0
    #: Abfragen, die das Budget verweigert hat.
    denied: int = 0
    #: Verbleibende Credits fuer heute.
    credits_left: int = 0
    #: Neu aufgenommene Vergleichsstichproben aus den abgelehnten Token.
    control: int = 0
    #: Vorschlaege, die alle Bedingungen erfuellt haben.
    calls: int = 0
    #: Laengst abgelegte Token, die wieder gehandelt werden.
    wakes: int = 0
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
        book=None,
        tracker_thread=None,
        wake=None,
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
        # Papierhandel. Er ist der Auto-Trader ohne Geld: dieselbe
        # Entscheidungskette, nur dass am Ende keine Transaktion steht.
        if book is None:
            from .paper import PaperBook

            book = PaperBook()
        self.book = book
        # Eigene, schnellere Schleife fuer die offenen Positionen. Sie haengt
        # bewusst nicht am Pruefzyklus: der laeuft je nach Kontingent alle
        # ein bis fuenf Minuten, und eine Ausstiegsregel, die nur so oft
        # hinsieht, verpasst genau die Bewegungen, um die es geht.
        prices = getattr(self.analyzer, "dexscreener", None)
        if tracker_thread is None and self.book is not None and prices is not None:
            from .paper import PositionTracker

            tracker_thread = PositionTracker(self.book, prices, log=self.log)
        self.tracker_thread = tracker_thread
        # Aufwach-Erkennung. Sie schaut auf alles, was XENO je gesehen hat -
        # der Fall, den der Pruefzyklus nicht abdecken kann, weil er ab Tag
        # zwei nur noch alle vier Stunden hinsieht und Token mit kritischem
        # Befund gar nicht mehr.
        if wake is None and prices is not None:
            from .wake import WakeWatcher

            wake = WakeWatcher(prices)
        self.wake = wake

    # -- Ein Durchlauf ----------------------------------------------------

    def build_queue(
        self,
        candidates: list[TokenCandidate],
        budget: int,
        now: float | None = None,
        woken: list[str] | None = None,
    ) -> list[tuple[str, TokenCandidate | None]]:
        """Stellt zusammen, was in diesem Durchlauf geprueft wird.

        Reihenfolge: Watchlist, Aufwacher, neue Token, faellige
        Wiederholungen. Die Watchlist zuerst, weil dort eine Verschlechterung
        unmittelbar Geld kostet - ein verpasster Neuzugang dagegen nur eine
        Gelegenheit.

        Die Aufwacher gleich danach, und vor allen Neuzugaengen: dass ein
        Token nach Tagen der Ruhe das Achtfache seines Tagesschnitts
        umsetzt, ist ein selteneres Ereignis als ein neuer Pool. Von denen
        entstehen vierzig in der Minute.
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

        for mint in woken or []:
            add(mint, by_mint.get(mint))

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

    def _call_alert(self, report: RiskReport, call) -> Alert:
        """Baut die Meldung zu einem Call - mit der eigenen Bilanz daneben.

        Die Trefferquote gehoert an jeden Vorschlag. Ohne sie liest sich ein
        Call wie eine Gewissheit, und genau das ist er nicht.
        """
        return Alert(
            kind=AlertKind.CALL,
            report=report,
            extra_lines=[
                f"{call.strength} Signale: " + ", ".join(call.reasons),
                self.book.hit_rate_text(),
            ],
        )

    def _wake_alert(self, report: RiskReport, wake) -> Alert:
        """Baut die Meldung zu einem Aufwacher.

        Das frische Urteil steht daneben, und das ist der Punkt: dass ein
        Token wieder gehandelt wird, heisst nicht, dass er in Ordnung ist.
        Ein Honeypot bleibt einer, auch wenn er gerade laeuft - die Meldung
        sagt "hier passiert etwas", nicht "hier kannst du kaufen".
        """
        lines = list(wake.reasons)
        if wake.previous_verdict and wake.previous_verdict != report.verdict.value:
            lines.append(
                f"Urteil damals {wake.previous_verdict}, jetzt {report.verdict.value}"
            )
        return Alert(kind=AlertKind.WAKE, report=report, extra_lines=lines)

    def _pulse(self, stats: CycleStats, now: float) -> dict:
        """Herzschlag ueber alle bekannten Token. Kostet keine RPC-Credits."""
        if self.wake is None or not self.wake.due(now):
            return {}
        try:
            found = self.wake.scan(self.state, now)
        except Exception as exc:  # noqa: BLE001
            # Eine Luecke ist besser als ein Abbruch - aber sie wird genannt.
            stats.errors.append(f"Puls fehlgeschlagen: {exc}")
            return {}
        if getattr(self.wake, "blind", False):
            stats.errors.append(
                "Puls ohne Antwort - die Aufwach-Erkennung hat in diesem "
                "Durchlauf nichts gesehen. Das ist keine Entwarnung."
            )
        for wake in found:
            self.log(
                f"Aufgewacht: {wake.symbol or wake.mint[:10]} - "
                f"Volumen {wake.burst:.0f}x, Kurs +{wake.price_change_h1_pct:.0f}%"
            )
        stats.wakes = len(found)
        return {w.mint: w for w in found}

    def _sample_control(self, screened, now: float) -> int:
        """Zieht ein paar abgelehnte Token als Vergleichsgruppe.

        Zufaellig ausgewaehlt, damit die Gruppe nicht systematisch aus den
        knapp Gescheiterten besteht - sonst verglichen wir die
        Durchgelassenen mit ihren naechsten Verwandten statt mit dem Feld.
        """
        minimum_age = self.settings.screen.min_age_minutes
        rejected = []
        for result in screened:
            if result.passed or not result.candidate.price_usd:
                continue
            if self.state.known(result.candidate.mint):
                continue
            # "Zu frisch" ist keine Ablehnung, sondern ein "noch nicht". Der
            # Token wird in wenigen Minuten regulaer geprueft. Landete er
            # jetzt in der Vergleichsgruppe, waere er dort fuer immer
            # gefangen - und die Gruppe bestuende ueberwiegend aus Token,
            # die nie wirklich beurteilt wurden.
            age = result.candidate.age_minutes
            if age is not None and age < minimum_age:
                continue
            rejected.append(result)
        if not rejected:
            return 0
        taken = 0
        for result in random.sample(rejected, min(CONTROL_SAMPLE, len(rejected))):
            reason = result.reasons[0] if result.reasons else ""
            if self.state.record_control(result.candidate, reason, now) is None:
                continue
            taken += 1
            # Auch die Abgelehnten laufen als Papierposition mit. Nur so
            # steht ihr Ergebnis in derselben Waehrung wie das der
            # empfohlenen - ein Median laesst sich nicht mit einer
            # Gewinnrechnung vergleichen.
            if self.book is not None:
                candidate = result.candidate
                self.book.enter(
                    candidate.mint,
                    candidate.price_usd,
                    symbol=candidate.symbol,
                    group="CONTROL",
                    mcap=candidate.mcap_usd,
                    reasons=[reason] if reason else [],
                    now=now,
                )
        return taken

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

        cache = getattr(self.analyzer, "origin_cache", None)
        meter = getattr(self.analyzer, "meter", None)
        hits_before = getattr(cache, "hits", 0)
        spent_before = getattr(meter, "day_spent", 0)
        denied_before = getattr(meter, "denied", 0)

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

        screened = screen_all(candidates, self.settings.screen)
        passed = [r.candidate for r in screened if r.passed]
        stats.passed_screen = len(passed)

        # Vergleichsstichprobe. Bisher verschwanden die abgelehnten Token
        # spurlos - damit liess sich zwar sagen, wie sich die
        # durchgelassenen entwickelt haben, aber nie, was der Filter
        # faelschlich aussortiert hat.
        stats.control = self._sample_control(screened, now)

        # Aufwach-Erkennung vor der Warteschlange: sie entscheidet mit,
        # wofuer das Pruefbudget ausgegeben wird.
        woken = self._pulse(stats, now)

        for mint, candidate in self.build_queue(passed, budget, now, list(woken)):
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

            # Papierhandel. Simuliert wird **jedes** Urteil, nicht nur die
            # Calls - sonst gaebe es zwar eine Zahl fuer die Vorschlaege,
            # aber keine Vergleichszahl, und ob die strengen Bedingungen
            # ueberhaupt etwas bringen, bliebe offen.
            if self.book is not None:
                call = worth_calling(report)
                self.book.enter_report(report, call, now=time.time())
                if call is not None:
                    stats.calls += 1
                    self.notifier.send(self._call_alert(report, call))

            # Ein Aufwacher hat Vorrang vor der gewoehnlichen Meldung: die
            # sagt "Urteil unveraendert" und faellt damit meist ganz aus -
            # ausgerechnet in dem Moment, in dem etwas passiert.
            wake = woken.get(mint)
            if wake is not None:
                self.notifier.send(self._wake_alert(report, wake))
                self.state.mark_alerted(mint, report.verdict)
                self.state.mark_woken(mint, time.time())
                stats.alerts += 1
            elif alert is not None:
                self.notifier.send(alert)
                self.state.mark_alerted(mint, report.verdict)
                stats.alerts += 1

        # Nachverfolgung: was ist aus frueher geprueften Token geworden?
        # Kostet keine RPC-Anfragen und konkurriert damit nicht mit dem
        # Pruefbudget - DexScreener liefert 30 Kurse pro Request.
        if self.tracker is not None:
            follow = self.tracker.run(time.time(), on_error=stats.errors.append)
            stats.measured = follow.measured

        stats.api_saved = getattr(cache, "hits", 0) - hits_before
        if meter is not None:
            stats.credits = meter.day_spent - spent_before
            stats.denied = meter.denied - denied_before
            stats.credits_left = meter.remaining_today
            if stats.denied:
                stats.errors.append(
                    f"Tagesbudget erreicht - {stats.denied} Abfragen ausgelassen. "
                    "Die betroffenen Pruefungen melden Wissensluecken."
                )
            meter.save()

        try:
            self.state.save()
        except OSError as exc:
            stats.errors.append(f"Zustand konnte nicht gespeichert werden: {exc}")
        if cache is not None:
            cache.save()

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

        # Positionen laufen in einer eigenen, schnelleren Schleife mit. Der
        # Pruefzyklus ist dafuer zu grob: eine Ausstiegsregel, die nur alle
        # fuenf Minuten hinsieht, verpasst genau die Spitzen, um die es geht.
        if self.tracker_thread is not None and self.tracker_thread.start():
            self.log(
                f"Positionsverfolgung alle {self.tracker_thread.interval:.0f}s"
            )

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
                        + (f", {stats.calls} CALL" if stats.calls else "")
                        + (f", {stats.wakes} aufgewacht" if stats.wakes else "")
                        + (f", {stats.control} Vergleich" if stats.control else "")
                        + (f", {stats.measured} nachverfolgt" if stats.measured else "")
                        + (
                            f", {stats.credits} Credits"
                            + (f" ({stats.credits_left} heute frei)"
                               if stats.credits_left else "")
                            if stats.credits
                            else ""
                        )
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
            if self.tracker_thread is not None:
                self.tracker_thread.stop()
            if self.book is not None:
                self.book.save(force=True)
            try:
                self.state.save()
            except OSError as exc:
                self.log(f"! Zustand konnte nicht gespeichert werden: {exc}")
