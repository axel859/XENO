"""Weboberflaeche und JSON-API.

Dashboard und Watcher laufen im selben Prozess: ``xeno serve`` startet beides.
Am eigenen Rechner ist das genau richtig - solange das Fenster offen ist,
laeuft der Bot, und die Oberflaeche ist im Browser erreichbar. Auf einem
Server spaeter aendert sich daran nichts, nur dass der Prozess dort dauerhaft
laeuft.

Nur Standardbibliothek, kein Webframework.

**Zugriff:** Standardmaessig lauscht der Server nur auf 127.0.0.1, ist also
ausschliesslich vom eigenen Rechner erreichbar. Wer ihn fuer das Handy im
selben WLAN oeffnet (``--host 0.0.0.0``), bekommt automatisch einen Token
vorgeschaltet - sonst koennte jedes Geraet im Netz die Watchlist aendern.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analyzer import TokenAnalyzer
from .config import Settings
from .discovery import Discovery
from .models import RiskReport
from .notify import Alert, ConsoleNotifier, MultiNotifier, TelegramNotifier
from .watcher import Watcher
from .watchstate import WatchState

WEB_DIR = Path(__file__).parent / "web"
#: So viele Meldungen und Berichte werden im Speicher vorgehalten.
ALERT_HISTORY = 200
REPORT_HISTORY = 300


class AppState:
    """Gemeinsamer Zustand von Watcher-Thread und HTTP-Threads."""

    def __init__(self, settings: Settings, state: WatchState) -> None:
        self.settings = settings
        self.state = state
        self.lock = threading.Lock()
        #: Vollstaendige Berichte - im Zustand liegen nur die Befund-Codes.
        self.reports: dict[str, dict[str, Any]] = {}
        self.report_order: deque[str] = deque(maxlen=REPORT_HISTORY)
        self.alerts: deque[dict[str, Any]] = deque(maxlen=ALERT_HISTORY)
        self.log_lines: deque[str] = deque(maxlen=200)
        self.cycles = 0
        #: Laufender Zaehler gepruefter Token. Ein Durchlauf kann mehrere
        #: Minuten dauern - ohne diesen Wert saehe die Oberflaeche waehrend
        #: des ersten Durchlaufs aus, als passiere nichts.
        self.checked_total = 0
        self.last_cycle: dict[str, Any] | None = None
        self.started_at = time.time()

    def log(self, message: str) -> None:
        with self.lock:
            self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def add_report(self, report: RiskReport) -> None:
        with self.lock:
            self.checked_total += 1
            if report.mint not in self.reports:
                self.report_order.append(report.mint)
                # deque wirft den aeltesten Eintrag raus - den Bericht dazu
                # auch, sonst waechst das Dictionary unbegrenzt.
                if len(self.report_order) == self.report_order.maxlen:
                    for mint in list(self.reports):
                        if mint not in self.report_order:
                            del self.reports[mint]
            self.reports[report.mint] = report.to_dict()

    def add_alert(self, alert: Alert) -> None:
        with self.lock:
            self.alerts.appendleft(
                {
                    "at": time.time(),
                    "kind": alert.kind.value,
                    "title": alert.title,
                    "mint": alert.report.mint,
                    "symbol": alert.report.symbol,
                    "verdict": alert.report.verdict.value,
                    "score": alert.report.score,
                    "previous_verdict": (
                        alert.previous_verdict.value if alert.previous_verdict else None
                    ),
                    "watchlisted": alert.watchlisted,
                    "lines": alert.summary_lines(),
                }
            )

    def note_cycle(self, stats) -> None:
        with self.lock:
            self.cycles += 1
            self.last_cycle = {
                "at": time.time(),
                "discovered": stats.discovered,
                "passed_screen": stats.passed_screen,
                "checked": stats.checked,
                "alerts": stats.alerts,
                "errors": list(stats.errors),
            }


class WatcherThread:
    """Startet und stoppt den Watcher im Hintergrund."""

    def __init__(self, app: AppState, watcher: Watcher) -> None:
        self.app = app
        self.watcher = watcher
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.interval = 60.0
        self.budget = 8
        self.test_trade = True

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def stopping(self) -> bool:
        """Stopp angefordert, Thread laeuft aber noch aus."""
        return self.running and self.stop_event.is_set()

    def start(self, interval: float | None = None, budget: int | None = None) -> bool:
        if self.running:
            return False
        if interval is not None:
            self.interval = max(15.0, interval)
        if budget is not None:
            self.budget = max(1, budget)

        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="xeno-watcher")
        self.thread.start()
        return True

    def _run(self) -> None:
        try:
            self.watcher.run(
                interval=self.interval,
                budget=self.budget,
                test_trade=self.test_trade,
                stop_event=self.stop_event,
                on_report=self.app.add_report,
                on_cycle=self.app.note_cycle,
            )
        except Exception as exc:  # noqa: BLE001
            self.app.log(f"! Watcher abgestuerzt: {exc}")

    def stop(self, timeout: float = 1.0) -> bool:
        """Fordert den Stopp an und wartet nur kurz.

        Bewusst nicht blockierend: eine gerade laufende Tokenpruefung haengt
        womoeglich mehrere Sekunden im Netz. Wuerde hier auf ihr Ende
        gewartet, bliebe der Stopp-Knopf in der Oberflaeche so lange stehen.
        Der Thread beendet sich selbst, sobald die laufende Pruefung durch ist.
        """
        if not self.running:
            return False
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        return True


class QuietHTTPServer(ThreadingHTTPServer):
    """HTTP-Server, der abgebrochene Verbindungen still hinnimmt.

    Die Voreinstellung schreibt bei jedem Abbruch einen vollstaendigen
    Stacktrace in die Konsole. Bricht ein Browser eine laufende Anfrage ab -
    etwa beim Neuladen waehrend einer laengeren Pruefung - sieht das aus wie
    ein Absturz, obwohl schlicht niemand mehr zuhoert.
    """

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    """Bedient die Oberflaeche und die API."""

    server_version = "xeno"
    app: AppState
    watcher_thread: WatcherThread
    analyzer: TokenAnalyzer
    auth_token: str = ""

    # Der Standard-Logger schreibt jede Anfrage nach stderr und uebertoent
    # die Watcher-Ausgabe.
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    # -- Hilfsmittel ------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Die Oberflaeche laedt nichts von aussen - das hier verhindert,
            # dass eine fremde Seite die API im Namen des Browsers aufruft.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            # Der Browser hat die Verbindung vor der Antwort geschlossen -
            # etwa weil die Seite neu geladen wurde, waehrend eine laengere
            # Pruefung lief. Voellig normal und kein Grund fuer eine Meldung.
            return

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length)) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if not self.auth_token:
            return True
        supplied = (
            self.headers.get("X-Auth-Token")
            or (query.get("token") or [""])[0]
            or ""
        )
        # Zeitkonstanter Vergleich, damit der Token nicht ueber die
        # Antwortzeit erraten werden kann.
        return secrets.compare_digest(supplied, self.auth_token)

    # -- Routen -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if not self._authorized(query):
            return self._error(401, "Token fehlt oder ist falsch")

        if path == "/api/status":
            return self._json(self._status())
        if path == "/api/tokens":
            return self._json({"tokens": self._tokens()})
        if path.startswith("/api/token/"):
            return self._json(self._token_detail(path.rsplit("/", 1)[-1]))
        if path == "/api/alerts":
            with self.app.lock:
                return self._json({"alerts": list(self.app.alerts)})
        if path == "/api/log":
            with self.app.lock:
                return self._json({"lines": list(self.app.log_lines)})
        if path == "/api/config":
            return self._json(
                {
                    "screen": asdict(self.app.settings.screen),
                    "risk": asdict(self.app.settings.risk),
                    "uses_public_rpc": self.app.settings.uses_public_rpc,
                }
            )

        return self._error(404, "Unbekannter Pfad")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorized(query):
            return self._error(401, "Token fehlt oder ist falsch")

        body = self._body()

        if path == "/api/watcher/start":
            started = self.watcher_thread.start(
                interval=_as_float(body.get("interval")),
                budget=_as_int(body.get("budget")),
            )
            self.app.log("Watcher gestartet" if started else "Watcher laeuft bereits")
            return self._json(self._status())

        if path == "/api/watcher/stop":
            stopped = self.watcher_thread.stop()
            self.app.log("Watcher gestoppt" if stopped else "Watcher laeuft nicht")
            return self._json(self._status())

        if path == "/api/watchlist":
            mint = str(body.get("mint") or "").strip()
            if not _looks_like_mint(mint):
                return self._error(400, "Das sieht nicht wie eine Mint-Adresse aus")
            self.app.state.add_to_watchlist(mint)
            self.app.state.save()
            self.app.log(f"Watchlist: {mint[:10]}... hinzugefuegt")
            return self._json({"ok": True, "tokens": self._tokens()})

        if path == "/api/check":
            mint = str(body.get("mint") or "").strip()
            if not _looks_like_mint(mint):
                return self._error(400, "Das sieht nicht wie eine Mint-Adresse aus")
            try:
                report = self.analyzer.analyze(mint)
            except Exception as exc:  # noqa: BLE001
                return self._error(502, f"Pruefung fehlgeschlagen: {exc}")
            self.app.add_report(report)
            self.app.state.record(report)
            self.app.state.save()
            return self._json(report.to_dict())

        return self._error(404, "Unbekannter Pfad")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authorized(parse_qs(parsed.query)):
            return self._error(401, "Token fehlt oder ist falsch")

        path = parsed.path.rstrip("/")
        if path.startswith("/api/watchlist/"):
            mint = path.rsplit("/", 1)[-1]
            removed = self.app.state.remove_from_watchlist(mint)
            self.app.state.save()
            return self._json({"ok": removed, "tokens": self._tokens()})

        return self._error(404, "Unbekannter Pfad")

    # -- Datenaufbereitung ------------------------------------------------

    def _status(self) -> dict[str, Any]:
        with self.app.lock:
            last_cycle = dict(self.app.last_cycle) if self.app.last_cycle else None
            cycles = self.app.cycles
            checked_total = self.app.checked_total
            alerts = len(self.app.alerts)
        return {
            "running": self.watcher_thread.running,
            "stopping": self.watcher_thread.stopping,
            "interval": self.watcher_thread.interval,
            "budget": self.watcher_thread.budget,
            "cycles": cycles,
            "checked_total": checked_total,
            "alerts": alerts,
            "last_cycle": last_cycle,
            "uptime": time.time() - self.app.started_at,
            "uses_public_rpc": self.app.settings.uses_public_rpc,
            "watchlist": len(self.app.state.watchlist),
        }

    def _tokens(self) -> list[dict[str, Any]]:
        entries = self.app.state.snapshot()
        with self.app.lock:
            reports = dict(self.app.reports)
        for entry in entries:
            report = reports.get(entry["mint"])
            if report:
                entry["market"] = report.get("market")
                entry["holders"] = report.get("holders")
                entry["top_findings"] = [
                    f
                    for f in report.get("findings", [])
                    if f.get("severity") in ("critical", "high", "medium")
                ][:3]
        entries.sort(key=lambda e: (not e["watchlisted"], -e["score"]))
        return entries

    def _token_detail(self, mint: str) -> dict[str, Any]:
        with self.app.lock:
            report = self.app.reports.get(mint)
        state = self.app.state.get(mint)
        return {
            "mint": mint,
            "state": asdict(state) if state else None,
            "report": report,
        }

    def _serve_file(self, name: str, content_type: str) -> None:
        path = WEB_DIR / name
        if not path.is_file():
            return self._error(500, f"{name} fehlt im Paket")
        self._send(200, path.read_bytes(), content_type)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_mint(value: str) -> bool:
    """Grobe Form einer Solana-Adresse: Base58, 32-44 Zeichen."""
    if not 32 <= len(value) <= 44:
        return False
    allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return set(value) <= allowed


def build_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    settings: Settings | None = None,
    state: WatchState | None = None,
    auth_token: str | None = None,
    use_telegram: bool = True,
    use_desktop: bool = True,
    sound: bool = True,
    only_important: bool = False,
) -> tuple[ThreadingHTTPServer, AppState, WatcherThread]:
    settings = settings or Settings.from_env()
    state = state or WatchState()
    app = AppState(settings, state)

    channels: list[Any] = [ConsoleNotifier()]

    if use_desktop:
        from .desktop import DesktopNotifier

        desktop = DesktopNotifier(
            sound=sound, only_important=only_important, on_error=app.log
        )
        if desktop.available:
            channels.append(desktop)

    if use_telegram:
        telegram = TelegramNotifier.from_env(on_error=app.log)
        if telegram is not None:
            channels.append(telegram)

    class Collector:
        def send(self, alert: Alert) -> None:
            app.add_alert(alert)

    notifier = MultiNotifier([*channels, Collector()], on_error=app.log)
    analyzer = TokenAnalyzer(settings)
    watcher = Watcher(
        settings,
        state=state,
        notifier=notifier,
        discovery=Discovery(),
        analyzer=analyzer,
        log=app.log,
    )
    watcher_thread = WatcherThread(app, watcher)

    # Ohne Token waere die API fuer jedes Geraet im Netz offen, sobald der
    # Server nicht mehr nur auf localhost lauscht.
    token = auth_token
    if token is None:
        token = "" if host in ("127.0.0.1", "localhost", "::1") else secrets.token_urlsafe(16)

    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "app": app,
            "watcher_thread": watcher_thread,
            "analyzer": analyzer,
            "auth_token": token,
        },
    )

    httpd = QuietHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.auth_token = token  # type: ignore[attr-defined]
    return httpd, app, watcher_thread
