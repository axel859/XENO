"""Tests der Suche - Adresse, Ticker oder Name.

Der wichtigste Test steht in ``TestRanking``. Eine Suche nach einem Namen
liefert bei Memecoins fast immer mehrere Token mit demselben Kuerzel, und
die Reihenfolge entscheidet, welchen davon jemand anschliessend kauft. Wer
nach ausgewiesener Liquiditaet sortiert, stellt die Faelschungen nach oben:
eine grosse Zahl im Pool kostet nichts, Umsatz dagegen setzt voraus, dass
tatsaechlich jemand handelt.

Die Zahlen in ``FAKE_TOP`` sind nicht erfunden - so sah die Antwort auf die
Suche nach "bonk" am 13.08.2026 aus.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from xeno.config import Settings
from xeno.known import looks_like_mint
from xeno.net import HttpError
from xeno.report import format_search_hit
from xeno.server import build_server
from xeno.sources.dexscreener import DexScreener
from xeno.watchstate import WatchState

REAL = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
FAKE = "GcbBU9phXen93bdFGVdujTprrKLvszcbfLDmq9TDEBMB"
OTHER = "BonkUg9t46HnxF4erZzmnB4Ae6oEcDS4V66zqUoUhwWY"


class FakeHttp:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        if self.error is not None:
            raise self.error
        return self.payload


def pair(
    mint: str,
    liquidity: float,
    volume: float,
    symbol: str = "Bonk",
    chain: str = "solana",
    name: str = "",
) -> dict:
    return {
        "chainId": chain,
        "pairAddress": f"pool-{mint[:6]}-{int(liquidity)}",
        "dexId": "raydium",
        "baseToken": {"address": mint, "symbol": symbol, "name": name or symbol},
        "priceUsd": "0.000021",
        "liquidity": {"usd": liquidity},
        "volume": {"h1": volume / 24, "h24": volume},
        "txns": {"h1": {"buys": 3, "sells": 2}},
        "marketCap": 204_000_000,
    }


def search(pairs: list[dict], query: str = "bonk", limit: int = 8):
    http = FakeHttp({"pairs": pairs})
    return DexScreener(http=http).search(query, limit=limit), http


class TestRanking:
    #: Echt gehandelt, aber je Pool nur wenig Liquiditaet.
    REAL_PAIRS = [
        pair(REAL, 123_951, 95_475),
        pair(REAL, 94_579, 763),
        pair(REAL, 64_179, 42_343),
    ]
    #: 142 Mio. ausgewiesene Liquiditaet, vier Dollar Umsatz am Tag.
    FAKE_TOP = pair(FAKE, 142_013_620, 3.99, symbol="BONK")

    def test_the_fake_with_the_big_pool_does_not_win(self):
        found, _ = search(self.REAL_PAIRS + [self.FAKE_TOP])
        assert found[0].mint == REAL

    def test_the_fake_is_still_listed(self):
        """Ausblenden waere eine Bewertung - und die trifft die Pruefung."""
        found, _ = search(self.REAL_PAIRS + [self.FAKE_TOP])
        assert FAKE in {c.mint for c in found}

    def test_volume_is_summed_over_all_pools(self):
        """Ein Token mit drei mittleren Pools schlaegt einen mit einem grossen."""
        found, _ = search(
            [pair(REAL, 1_000, 40_000), pair(REAL, 1_000, 40_000)]
            + [pair(OTHER, 900_000, 70_000)]
        )
        assert found[0].mint == REAL

    def test_liquidity_decides_when_nothing_trades(self):
        found, _ = search([pair(REAL, 5_000, 0.0), pair(OTHER, 900_000, 0.0)])
        assert found[0].mint == OTHER


class TestSearch:
    def test_every_mint_appears_once(self):
        found, _ = search(TestRanking.REAL_PAIRS)
        assert [c.mint for c in found] == [REAL]

    def test_the_deepest_pool_supplies_the_numbers(self):
        """Angezeigt wird der belastbarste Kurs, nicht irgendeiner."""
        found, _ = search(TestRanking.REAL_PAIRS)
        assert found[0].liquidity_usd == 123_951

    def test_other_chains_are_dropped(self):
        found, _ = search([pair(REAL, 5_000, 900, chain="ethereum")])
        assert found == []

    def test_pairs_without_address_are_skipped(self):
        http = FakeHttp({"pairs": [{"chainId": "solana", "baseToken": {}}]})
        assert DexScreener(http=http).search("bonk") == []

    def test_limit_is_respected(self):
        pairs = [pair(f"{i}" * 40, 1_000, 100 * i) for i in range(1, 6)]
        found, _ = search(pairs, limit=2)
        assert len(found) == 2

    def test_the_query_is_passed_on(self):
        _, http = search([], query="wif")
        assert http.calls[0][1] == {"q": "wif"}

    def test_empty_query_asks_nothing(self):
        """Sonst kostet jeder Tastendruck ins leere Feld eine Anfrage."""
        http = FakeHttp({"pairs": []})
        assert DexScreener(http=http).search("   ") == []
        assert http.calls == []

    def test_a_broken_answer_is_no_crash(self):
        """Eine Fehlerseite ist kein JSON-Objekt mit ``pairs`` darin."""
        assert DexScreener(http=FakeHttp(None)).search("bonk") == []
        assert DexScreener(http=FakeHttp({})).search("bonk") == []

    def test_source_is_marked(self):
        found, _ = search([pair(REAL, 5_000, 900)])
        assert found[0].source == "dexscreener:search"


class TestMintForm:
    def test_accepts_a_real_mint(self):
        assert looks_like_mint(REAL)

    def test_rejects_a_name(self):
        assert not looks_like_mint("bonk")

    def test_rejects_the_characters_base58_does_not_have(self):
        # 0, O, I und l - genau die, die man beim Abtippen verwechselt.
        assert not looks_like_mint("0OIl" + "1" * 36)

    def test_rejects_empty(self):
        assert not looks_like_mint("")


class TestHitLine:
    def test_shows_ticker_and_address(self):
        found, _ = search([pair(REAL, 5_000, 900, symbol="BONK", name="Bonk")])
        line = format_search_hit(found[0])
        assert "BONK" in line
        assert REAL in line

    def test_the_name_is_left_out_when_it_repeats_the_ticker(self):
        found, _ = search([pair(REAL, 5_000, 900, symbol="Bonk", name="Bonk")])
        assert format_search_hit(found[0]).count("Bonk") == 1

    def test_missing_numbers_do_not_break_the_line(self):
        found, _ = search([pair(REAL, 0, 0)])
        assert "n/a" in format_search_hit(found[0])


# -- Die Suche ueber die API -------------------------------------------------


@pytest.fixture
def api(tmp_path):
    """Server mit einer Suchquelle, die nur vorbereitete Antworten kennt.

    Kein Test geht ins Netz: der Watcher wird nicht gestartet, und die
    einzige Quelle, die ``/api/search`` benutzt, ist ein Doppel.
    """
    state = WatchState(tmp_path / "state.json")
    httpd, app, watcher_thread = build_server(
        host="127.0.0.1", port=0, settings=Settings(), state=state, use_telegram=False
    )
    analyzer = watcher_thread.watcher.analyzer
    http = FakeHttp({"pairs": []})
    analyzer.dexscreener = DexScreener(http=http)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", http, analyzer, app
    finally:
        watcher_thread.stop(timeout=2)
        httpd.shutdown()
        httpd.server_close()


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class TestSearchApi:
    def test_finds_by_name(self, api):
        url, http, _analyzer, _app = api
        http.payload = {"pairs": [pair(REAL, 5_000, 900)]}
        status, data = get(f"{url}/api/search?q=bonk")
        assert status == 200
        assert [m["mint"] for m in data["matches"]] == [REAL]

    def test_an_address_is_looked_up_directly(self, api):
        """Nachschlagen ist genauer als suchen - und findet auch, was in
        keinem Suchindex steht."""
        url, http, _analyzer, _app = api
        http.payload = {"pairs": [pair(REAL, 5_000, 900)]}
        status, data = get(f"{url}/api/search?q={REAL}")
        assert status == 200
        assert data["matches"][0]["mint"] == REAL
        assert "tokens" in http.calls[0][0]  # nicht /search

    def test_an_address_without_a_pool_is_still_a_hit(self, api):
        """Kein Handelspaar heisst nicht, dass es den Token nicht gibt -
        die Pruefung liest den Mint direkt von der Chain."""
        url, http, _analyzer, _app = api
        http.payload = {"pairs": []}
        status, data = get(f"{url}/api/search?q={REAL}")
        assert status == 200
        assert data["matches"][0]["mint"] == REAL

    def test_market_data_comes_along(self, api):
        url, http, _analyzer, _app = api
        http.payload = {"pairs": [pair(REAL, 5_000, 900)]}
        _status, data = get(f"{url}/api/search?q=bonk")
        assert data["matches"][0]["market"]["liquidity_usd"] == 5_000

    def test_an_empty_query_asks_nothing(self, api):
        url, http, _analyzer, _app = api
        status, data = get(f"{url}/api/search?q=")
        assert status == 200
        assert data["matches"] == []
        assert http.calls == []

    def test_already_checked_tokens_are_marked(self, api):
        """Sonst gibt jemand Credits aus, nur um denselben Bericht nochmal
        zu sehen."""
        url, http, _analyzer, app = api
        from conftest import MINT, make_mint_info

        from xeno.models import RiskReport

        app.add_report(RiskReport(mint=MINT, symbol="T", mint_info=make_mint_info()))
        http.payload = {"pairs": [pair(MINT, 5_000, 900), pair(REAL, 4_000, 800)]}
        _status, data = get(f"{url}/api/search?q=bonk")
        marked = {m["mint"]: m["checked"] for m in data["matches"]}
        assert marked[MINT] is True
        assert marked[REAL] is False

    def test_a_failing_source_is_reported_not_swallowed(self, api):
        """Eine leere Liste wuerde heissen 'gibt es nicht' - das waere
        etwas anderes als 'konnte nicht nachsehen'."""
        url, http, _analyzer, _app = api
        http.error = HttpError("DexScreener antwortet nicht")
        status, data = get(f"{url}/api/search?q=bonk")
        assert status == 502
        assert "antwortet nicht" in data["error"]

    def test_without_a_source_it_says_so(self, api):
        url, _http, analyzer, _app = api
        analyzer.dexscreener = None
        status, data = get(f"{url}/api/search?q=bonk")
        assert status == 503
        assert "error" in data


class TestAusblendenApi:
    def test_one_token_disappears_from_the_list(self, api):
        url, _http, _analyzer, app = api
        from conftest import MINT, make_mint_info

        from xeno.models import RiskReport

        app.state.record(RiskReport(mint=MINT, symbol="T", mint_info=make_mint_info()))

        request = urllib.request.Request(
            f"{url}/api/hide", method="POST",
            data=json.dumps({"mint": MINT}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        assert data["ok"] is True
        assert next(t for t in data["tokens"] if t["mint"] == MINT)["hidden"] is True

    def test_hiding_everything_at_once(self, api):
        url, _http, _analyzer, app = api
        from conftest import make_mint_info

        from xeno.models import RiskReport

        for i in range(4):
            app.state.record(
                RiskReport(mint=f"{i}" * 40, symbol="T", mint_info=make_mint_info())
            )
        request = urllib.request.Request(
            f"{url}/api/hide", method="POST", data=json.dumps({"all": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        assert data["hidden"] == 4

    def test_bringing_one_back(self, api):
        url, _http, _analyzer, app = api
        from conftest import MINT, make_mint_info

        from xeno.models import RiskReport

        app.state.record(RiskReport(mint=MINT, symbol="T", mint_info=make_mint_info()))
        app.state.set_hidden(MINT, True)

        request = urllib.request.Request(
            f"{url}/api/hide/{MINT}", method="DELETE",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        assert data["ok"] is True
        assert next(t for t in data["tokens"] if t["mint"] == MINT)["hidden"] is False

    def test_a_bad_address_is_rejected(self, api):
        url, _http, _analyzer, _app = api
        request = urllib.request.Request(
            f"{url}/api/hide", method="POST", data=json.dumps({"mint": "quatsch"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("ungueltige Adresse wurde angenommen")
