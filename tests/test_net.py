"""Tests fuer Rate-Limiting, Retry und den RPC-Client.

Ohne Netzwerk: ``urllib.request.urlopen`` wird ersetzt. Diese Schicht ist
kritisch, weil die kostenlosen Endpunkte durchweg drosseln - ein stiller
Fehler hier liefert leere Analysen statt einer Fehlermeldung.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from xeno.net import HttpClient, HttpError, RateLimiter, RpcError, SolanaRpc, redact_url


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://test", code, "err", {}, None)


@pytest.fixture
def patch_urlopen(monkeypatch):
    """Ersetzt urlopen durch eine Liste vorgegebener Antworten/Fehler."""

    def install(responses):
        calls = {"count": 0, "payloads": []}

        def fake(req, timeout=None):
            index = calls["count"]
            calls["count"] += 1
            if req.data:
                calls["payloads"].append(json.loads(req.data))
            item = responses[min(index, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return FakeResponse(json.dumps(item).encode())

        monkeypatch.setattr("urllib.request.urlopen", fake)
        return calls

    return install


class TestRedaction:
    """Zugangsdaten stehen bei Helius und Telegram in der URL - sie duerfen
    nicht in Fehlermeldungen, Logs oder Screenshots landen."""

    def test_removes_telegram_bot_token(self):
        result = redact_url("https://api.telegram.org/bot7891234:AAF-geheim_xyz/getUpdates")
        assert "geheim" not in result
        assert result == "https://api.telegram.org/bot***/getUpdates"

    def test_removes_rpc_api_key(self):
        result = redact_url("https://mainnet.helius-rpc.com/?api-key=abc123-secret")
        assert "abc123" not in result

    def test_keeps_harmless_parameters(self):
        url = "https://api.example.com/x?limit=5&amount=100"
        assert redact_url(url) == url

    def test_redacts_only_the_secret_parameter(self):
        result = redact_url("https://x.test/?limit=5&api-key=geheim&page=2")
        assert "limit=5" in result and "page=2" in result and "geheim" not in result

    def test_url_without_query_is_unchanged(self):
        assert redact_url("https://x.test/a/b") == "https://x.test/a/b"

    def test_secret_never_reaches_the_error_message(self, patch_urlopen, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        patch_urlopen([http_error(401)])
        with pytest.raises(HttpError) as exc:
            HttpClient(rate_limit=0, max_retries=1).get(
                "https://mainnet.helius-rpc.com/?api-key=streng-geheim"
            )
        assert "streng-geheim" not in str(exc.value)


class TestRateLimiter:
    def test_enforces_minimum_spacing(self):
        limiter = RateLimiter(calls_per_second=20.0)  # 50ms Abstand
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire()
        assert time.monotonic() - start >= 0.09

    def test_zero_disables_limiting(self):
        limiter = RateLimiter(calls_per_second=0)
        start = time.monotonic()
        for _ in range(50):
            limiter.acquire()
        assert time.monotonic() - start < 0.05


class TestHttpClient:
    def test_returns_parsed_json(self, patch_urlopen):
        patch_urlopen([{"ok": True}])
        assert HttpClient(rate_limit=0).get("http://test") == {"ok": True}

    def test_retries_on_429_then_succeeds(self, patch_urlopen, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        calls = patch_urlopen([http_error(429), http_error(429), {"ok": True}])
        client = HttpClient(rate_limit=0, max_retries=4)
        assert client.get("http://test") == {"ok": True}
        assert calls["count"] == 3

    def test_gives_up_after_max_retries(self, patch_urlopen, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        patch_urlopen([http_error(503)])
        with pytest.raises(HttpError) as exc:
            HttpClient(rate_limit=0, max_retries=2).get("http://test")
        assert exc.value.status == 503

    def test_does_not_retry_client_errors(self, patch_urlopen):
        calls = patch_urlopen([http_error(404)])
        with pytest.raises(HttpError):
            HttpClient(rate_limit=0, max_retries=4).get("http://test")
        assert calls["count"] == 1

    def test_builds_query_string_and_drops_none(self, patch_urlopen, monkeypatch):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            return FakeResponse(b"{}")

        monkeypatch.setattr("urllib.request.urlopen", fake)
        HttpClient(rate_limit=0).get("http://test", params={"a": 1, "b": None})
        assert "a=1" in seen["url"]
        assert "b=" not in seen["url"]


class TestSolanaRpc:
    def test_unwraps_result(self, patch_urlopen):
        patch_urlopen([{"jsonrpc": "2.0", "id": 1, "result": {"value": 42}}])
        rpc = SolanaRpc("http://rpc", HttpClient(rate_limit=0))
        assert rpc.call("getX") == {"value": 42}

    def test_raises_on_rpc_error(self, patch_urlopen):
        patch_urlopen([{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}])
        rpc = SolanaRpc("http://rpc", HttpClient(rate_limit=0))
        with pytest.raises(RpcError) as exc:
            rpc.call("getX")
        assert exc.value.code == -32601

    def test_batch_preserves_request_order(self, patch_urlopen):
        # Antworten absichtlich vertauscht - die Zuordnung laeuft ueber die id.
        patch_urlopen(
            [[{"id": 2, "result": "second"}, {"id": 1, "result": "first"}]]
        )
        rpc = SolanaRpc("http://rpc", HttpClient(rate_limit=0))
        assert rpc.batch([("a", []), ("b", [])]) == ["first", "second"]

    def test_batch_reports_partial_failure_as_none(self, patch_urlopen):
        patch_urlopen(
            [[{"id": 1, "result": "ok"}, {"id": 2, "error": {"message": "bad"}}]]
        )
        rpc = SolanaRpc("http://rpc", HttpClient(rate_limit=0))
        assert rpc.batch([("a", []), ("b", [])]) == ["ok", None]

    def test_empty_batch_skips_request(self, patch_urlopen):
        calls = patch_urlopen([{}])
        rpc = SolanaRpc("http://rpc", HttpClient(rate_limit=0))
        assert rpc.batch([]) == []
        assert calls["count"] == 0

    def test_get_multiple_accounts_chunks_above_100(self, patch_urlopen):
        calls = patch_urlopen([{"jsonrpc": "2.0", "id": 1, "result": {"value": [None] * 100}}])
        rpc = SolanaRpc("http://rpc", HttpClient(rate_limit=0))
        rpc.get_multiple_accounts([f"addr{i}" for i in range(250)])
        assert calls["count"] == 3
