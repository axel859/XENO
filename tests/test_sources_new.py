"""Tests der beiden neuen Abrufe - Kerzen und Wallet-Herkunft.

Beide gehen im Betrieb ans Netz, hier nicht: die HTTP-Schicht wird durch ein
Doppel ersetzt, das aufgezeichnete Antworten zurueckgibt. Getestet wird
damit genau das, was schiefgehen kann - das Umsetzen der Antwort und das
Verhalten bei Ausfaellen.

Die Antwortformen stammen aus echten Abrufen gegen beide APIs.
"""

from __future__ import annotations

import pytest

from xeno.net import HttpError
from xeno.sources.geckoterminal import GeckoTerminal
from xeno.sources.helius import WALLET_PAGE, Helius


class FakeHttp:
    """Gibt vorbereitete Antworten zurueck und merkt sich die Aufrufe."""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        if self.error is not None:
            raise self.error
        return self.payload


def ohlcv(rows) -> dict:
    return {"data": {"attributes": {"ohlcv_list": rows}}}


class TestCandles:
    #: So liefert die API: neueste Kerze zuerst, [zeit, o, h, l, c, volumen].
    ROWS = [
        [1_700_000_600, 3.0, 3.5, 2.9, 3.4, 120.0],
        [1_700_000_300, 2.0, 2.6, 1.9, 3.0, 90.0],
        [1_700_000_000, 1.0, 1.4, 0.9, 2.0, 50.0],
    ]

    def test_returns_oldest_first(self):
        """Die API liefert rueckwaerts - jede Auswertung braucht vorwaerts."""
        gecko = GeckoTerminal(http=FakeHttp(ohlcv(self.ROWS)))
        candles = gecko.candles("pool")
        assert [c.time for c in candles] == [
            1_700_000_000,
            1_700_000_300,
            1_700_000_600,
        ]

    def test_maps_the_columns(self):
        gecko = GeckoTerminal(http=FakeHttp(ohlcv(self.ROWS)))
        first = gecko.candles("pool")[0]
        assert (first.open, first.high, first.low, first.close) == (1.0, 1.4, 0.9, 2.0)
        assert first.volume == 50.0

    def test_body_ignores_the_wick(self):
        gecko = GeckoTerminal(http=FakeHttp(ohlcv(self.ROWS)))
        first = gecko.candles("pool")[0]
        assert first.body_high == 2.0  # nicht 1.4 aus dem Docht
        assert first.body_low == 1.0

    def test_broken_rows_are_skipped(self):
        rows = [["kaputt"], [1, 2, 3], list(self.ROWS[0])]
        gecko = GeckoTerminal(http=FakeHttp(ohlcv(rows)))
        assert len(gecko.candles("pool")) == 1

    def test_empty_answer(self):
        assert GeckoTerminal(http=FakeHttp({})).candles("pool") == []
        assert GeckoTerminal(http=FakeHttp(None)).candles("pool") == []

    def test_passes_timeframe_through(self):
        http = FakeHttp(ohlcv([]))
        GeckoTerminal(http=http).candles("meinpool", timeframe="hour", aggregate=4, limit=50)
        url, params = http.calls[0]
        assert url.endswith("/pools/meinpool/ohlcv/hour")
        assert params == {"aggregate": 4, "limit": 50}


def tx(ts: int, sender: str, to: str, amount: int) -> dict:
    return {
        "timestamp": ts,
        "nativeTransfers": [
            {"fromUserAccount": sender, "toUserAccount": to, "amount": amount}
        ],
    }


class TestOrigin:
    def test_finds_the_funder(self):
        page = [tx(200, "spaeter", "meine", 9_000_000), tx(100, "quelle", "meine", 9_000_000)]
        result = Helius(api_key="k", http=FakeHttp(page)).origin("meine")
        assert result.funder == "quelle"
        assert result.funded_at == 100
        assert result.established is False

    def test_a_full_page_means_established(self):
        """Wer eine volle Seite Transaktionen hat, wurde nicht fuer diesen
        einen Token angelegt. Weiterblaettern kostete Anfragen fuer eine
        Antwort, die damit schon feststeht."""
        page = [tx(i, "x", "meine", 9_000_000) for i in range(WALLET_PAGE)]
        result = Helius(api_key="k", http=FakeHttp(page)).origin("meine")
        assert result.established is True
        assert result.funder == ""

    def test_only_one_request_per_wallet(self):
        http = FakeHttp([tx(100, "quelle", "meine", 9_000_000)])
        Helius(api_key="k", http=http).origin("meine")
        assert len(http.calls) == 1

    def test_without_a_key_nothing_is_fetched(self):
        http = FakeHttp([])
        assert Helius(api_key="", http=http).origin("meine") is None
        assert http.calls == []

    def test_a_failed_request_is_a_gap_not_an_all_clear(self):
        helius = Helius(api_key="k", http=FakeHttp(error=HttpError("kaputt")))
        assert helius.origin("meine") is None

    def test_unexpected_payload(self):
        assert Helius(api_key="k", http=FakeHttp({"fehler": 1})).origin("w") is None


class TestOriginBudget:
    def test_stops_at_the_budget(self):
        http = FakeHttp([tx(100, "quelle", "w", 9_000_000)])
        helius = Helius(api_key="k", http=http)
        helius.origins([f"w{i}" for i in range(50)], budget=4)
        assert len(http.calls) == 4

    def test_failures_are_left_out_without_stopping(self, monkeypatch):
        helius = Helius(api_key="k", http=FakeHttp([]))
        calls = {"n": 0}

        def flaky(wallet):
            calls["n"] += 1
            from xeno.sources.helius import Origin

            return None if calls["n"] == 2 else Origin(wallet=wallet, funder="q")

        monkeypatch.setattr(helius, "origin", flaky)
        result = helius.origins(["a", "b", "c"], budget=10)
        assert [o.wallet for o in result] == ["a", "c"]


@pytest.mark.parametrize("payload", [None, {}, [], "text"])
def test_candles_never_raise_on_junk(payload):
    assert GeckoTerminal(http=FakeHttp(payload)).candles("pool") == []
