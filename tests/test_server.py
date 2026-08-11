"""Tests fuer Weboberflaeche und API.

Der Server wird echt gestartet, aber nur auf 127.0.0.1 mit einem zufaelligen
Port. Netzwerkzugriffe nach aussen finden nicht statt: der Watcher wird nicht
gestartet, und ``/api/check`` bekommt einen Ersatz-Analyzer.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import MINT, make_mint_info

from xeno.config import Settings
from xeno.models import Finding, RiskReport, Severity
from xeno.notify import Alert, AlertKind
from xeno.server import AppState, _looks_like_mint, build_server
from xeno.watchstate import WatchState


def make_report(mint: str = MINT, symbol: str = "TEST") -> RiskReport:
    report = RiskReport(mint=mint, symbol=symbol, mint_info=make_mint_info())
    report.findings = [
        Finding(check="t", code="clean", severity=Severity.INFO, message="alles ok")
    ]
    return report


@pytest.fixture
def server(tmp_path):
    """Laufender Server auf einem freien Port, ohne aktiven Watcher."""
    state = WatchState(tmp_path / "state.json")
    httpd, app, watcher_thread = build_server(
        host="127.0.0.1", port=0, settings=Settings(), state=state, use_telegram=False
    )
    # Die Discovery wird ersetzt, damit kein Test ins Netz geht - ein
    # gestarteter Watcher dreht dann sofort leer durch.
    watcher_thread.watcher.discovery = _NoDiscovery()

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", app, state
    finally:
        watcher_thread.stop(timeout=2)
        httpd.shutdown()
        httpd.server_close()


class _NoDiscovery:
    def collect(self, **kwargs):
        return []


def call(url: str, method: str = "GET", body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)


class TestMintValidation:
    def test_accepts_real_mint(self):
        assert _looks_like_mint(MINT)

    def test_rejects_short_input(self):
        assert not _looks_like_mint("quatsch")

    def test_rejects_non_base58_characters(self):
        # 0, O, I und l gibt es in Base58 nicht.
        assert not _looks_like_mint("0OIl" + "1" * 36)

    def test_rejects_empty(self):
        assert not _looks_like_mint("")


class TestAppState:
    def test_counts_checked_tokens(self, tmp_path):
        app = AppState(Settings(), WatchState(tmp_path / "s.json"))
        app.add_report(make_report())
        app.add_report(make_report(mint="andere" + "1" * 38))
        assert app.checked_total == 2

    def test_report_cache_stays_bounded(self, tmp_path):
        """Sonst waechst der Speicher bei tagelangem Betrieb unbegrenzt."""
        app = AppState(Settings(), WatchState(tmp_path / "s.json"))
        app.report_order = type(app.report_order)(maxlen=5)
        for i in range(40):
            app.add_report(make_report(mint=f"mint{i:040d}"))
        assert len(app.reports) <= 6

    def test_alerts_are_newest_first(self, tmp_path):
        app = AppState(Settings(), WatchState(tmp_path / "s.json"))
        app.add_alert(Alert(kind=AlertKind.NEW, report=make_report(symbol="ERST")))
        app.add_alert(Alert(kind=AlertKind.NEW, report=make_report(symbol="ZWEIT")))
        assert app.alerts[0]["symbol"] == "ZWEIT"


class TestApi:
    def test_serves_the_dashboard(self, server):
        base, _, _ = server
        with urllib.request.urlopen(f"{base}/", timeout=10) as response:
            body = response.read().decode()
        assert response.status == 200
        assert "<title>XENO</title>" in body

    def test_status_reports_stopped_watcher(self, server):
        base, _, _ = server
        status, payload = call(f"{base}/api/status")
        assert status == 200
        assert payload["running"] is False
        assert payload["cycles"] == 0

    def test_watchlist_add_and_remove(self, server):
        base, _, state = server
        _, payload = call(f"{base}/api/watchlist", "POST", {"mint": MINT})
        assert payload["ok"] is True
        assert [s.mint for s in state.watchlist] == [MINT]

        _, payload = call(f"{base}/api/watchlist/{MINT}", "DELETE")
        assert payload["ok"] is True
        assert state.watchlist == []

    def test_watchlist_rejects_garbage(self, server):
        base, _, _ = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(f"{base}/api/watchlist", "POST", {"mint": "quatsch"})
        assert exc.value.code == 400

    def test_tokens_endpoint_includes_findings(self, server):
        base, app, state = server
        report = make_report()
        report.findings.append(
            Finding(check="t", code="bad", severity=Severity.HIGH, message="schlimm")
        )
        app.add_report(report)
        state.record(report)

        _, payload = call(f"{base}/api/tokens")
        entry = next(t for t in payload["tokens"] if t["mint"] == MINT)
        assert entry["top_findings"][0]["message"] == "schlimm"

    def test_watchlist_entries_come_first(self, server):
        base, app, state = server
        other = "B" * 43
        state.record(make_report(mint=other, symbol="ANDERE"))
        state.record(make_report())
        state.add_to_watchlist(MINT)

        _, payload = call(f"{base}/api/tokens")
        assert payload["tokens"][0]["mint"] == MINT

    def test_token_detail(self, server):
        base, app, state = server
        app.add_report(make_report())
        state.record(make_report())
        _, payload = call(f"{base}/api/token/{MINT}")
        assert payload["report"]["symbol"] == "TEST"
        assert payload["state"]["mint"] == MINT

    def test_alerts_endpoint(self, server):
        base, app, _ = server
        app.add_alert(Alert(kind=AlertKind.NEW, report=make_report()))
        _, payload = call(f"{base}/api/alerts")
        assert payload["alerts"][0]["kind"] == "new"

    def test_config_endpoint_exposes_thresholds(self, server):
        base, _, _ = server
        _, payload = call(f"{base}/api/config")
        assert "min_liquidity_usd" in payload["screen"]
        assert "max_top10_pct" in payload["risk"]

    def test_unknown_path_is_404(self, server):
        base, _, _ = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(f"{base}/api/gibtsnicht")
        assert exc.value.code == 404

    def test_watcher_start_and_stop(self, server):
        base, _, _ = server
        _, payload = call(f"{base}/api/watcher/start", "POST", {"interval": 3600, "budget": 1})
        assert payload["running"] is True
        _, payload = call(f"{base}/api/watcher/stop", "POST", {})
        assert payload["running"] is False

    def test_interval_has_a_floor(self, server):
        """Ein Intervall von einer Sekunde wuerde jedes Kontingent sprengen."""
        base, _, _ = server
        _, payload = call(f"{base}/api/watcher/start", "POST", {"interval": 1})
        assert payload["interval"] >= 15
        call(f"{base}/api/watcher/stop", "POST", {})


class TestAuth:
    @pytest.fixture
    def guarded(self, tmp_path):
        httpd, app, watcher_thread = build_server(
            host="127.0.0.1",
            port=0,
            settings=Settings(),
            state=WatchState(tmp_path / "s.json"),
            auth_token="geheim123",
            use_telegram=False,
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            watcher_thread.stop(timeout=2)
            httpd.shutdown()
            httpd.server_close()

    def test_rejects_missing_token(self, guarded):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(f"{guarded}/api/status")
        assert exc.value.code == 401

    def test_rejects_wrong_token(self, guarded):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(f"{guarded}/api/status", token="falsch")
        assert exc.value.code == 401

    def test_accepts_correct_token(self, guarded):
        status, _ = call(f"{guarded}/api/status", token="geheim123")
        assert status == 200

    def test_accepts_token_from_query_string(self, guarded):
        status, _ = call(f"{guarded}/api/status?token=geheim123")
        assert status == 200

    def test_write_endpoints_are_guarded(self, guarded):
        """Ohne Schutz koennte jedes Geraet im Netz die Watchlist aendern."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(f"{guarded}/api/watchlist", "POST", {"mint": MINT})
        assert exc.value.code == 401

    def test_dashboard_itself_stays_reachable(self, guarded):
        """Die Seite muss laden - sie holt sich den Token aus der URL."""
        with urllib.request.urlopen(f"{guarded}/", timeout=10) as response:
            assert response.status == 200


class TestLanDefaults:
    def test_local_binding_needs_no_token(self, tmp_path):
        httpd, _, watcher = build_server(
            host="127.0.0.1", port=0, state=WatchState(tmp_path / "a.json"),
            settings=Settings(), use_telegram=False,
        )
        try:
            assert httpd.auth_token == ""
        finally:
            watcher.stop(timeout=1)
            httpd.server_close()

    def test_network_binding_generates_a_token(self, tmp_path):
        """Sobald der Server im Netz haengt, darf er nicht offen sein."""
        httpd, _, watcher = build_server(
            host="0.0.0.0", port=0, state=WatchState(tmp_path / "b.json"),
            settings=Settings(), use_telegram=False,
        )
        try:
            assert len(httpd.auth_token) >= 16
        finally:
            watcher.stop(timeout=1)
            httpd.server_close()


class TestClientDisconnect:
    """Ein Browser, der die Verbindung abbricht, darf keinen Stacktrace
    erzeugen. Das passiert im Alltag staendig: eine Pruefung dauert lange,
    die Seite wird neu geladen - und die Konsole sah aus wie ein Absturz."""

    def _server_with_spy(self, monkeypatch):
        """QuietHTTPServer ohne echten Socket, dessen Elternaufruf mitgezaehlt wird."""
        from http.server import ThreadingHTTPServer

        from xeno.server import QuietHTTPServer

        passed_through = []
        monkeypatch.setattr(
            ThreadingHTTPServer,
            "handle_error",
            lambda self, request, addr: passed_through.append(True),
        )
        return QuietHTTPServer.__new__(QuietHTTPServer), passed_through

    def test_connection_error_is_not_passed_on(self, monkeypatch):
        server, passed_through = self._server_with_spy(monkeypatch)
        try:
            raise ConnectionAbortedError(10053, "abgebrochen")
        except ConnectionAbortedError:
            server.handle_error(None, ("127.0.0.1", 1234))
        assert passed_through == []

    def test_timeout_is_not_passed_on(self, monkeypatch):
        server, passed_through = self._server_with_spy(monkeypatch)
        try:
            raise TimeoutError("zu langsam")
        except TimeoutError:
            server.handle_error(None, ("127.0.0.1", 1234))
        assert passed_through == []

    def test_real_errors_still_surface(self, monkeypatch):
        """Echte Programmfehler duerfen nicht mitverschluckt werden - sonst
        verschwinden auch die Meldungen, die man wirklich sehen will."""
        server, passed_through = self._server_with_spy(monkeypatch)
        try:
            raise ValueError("echter Fehler")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 1234))
        assert passed_through == [True]

    def test_send_survives_closed_socket(self, server):
        """Bricht der Client waehrend der Antwort ab, faellt _send still durch."""
        import socket
        import urllib.request

        base, _, _ = server
        host, port = base.replace("http://", "").split(":")

        # Anfrage schicken und die Verbindung sofort kappen, ohne zu lesen.
        raw = socket.create_connection((host, int(port)), timeout=5)
        raw.sendall(b"GET /api/tokens HTTP/1.1\r\nHost: x\r\n\r\n")
        raw.close()

        # Der Server muss danach noch normal antworten.
        with urllib.request.urlopen(f"{base}/api/status", timeout=10) as response:
            assert response.status == 200
