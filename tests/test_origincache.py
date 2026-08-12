"""Tests des Wallet-Gedaechtnisses.

Der Anlass war ein Fehler mit Rechnung: die Herkunftspruefung fragte bei
jeder Wiederholung dieselbe unveraenderliche Tatsache neu ab. Ein Token wird
in der ersten Stunde alle fuenf Minuten geprueft - zwoelf Mal zehn Anfragen
fuer ein Ergebnis, das sich nicht aendern kann. Das hat ein Monatskontingent
in wenigen Tagen aufgebraucht.

Geprueft wird deshalb vor allem eines: dass beim zweiten Mal **keine**
Anfrage mehr rausgeht.
"""

from __future__ import annotations

import json

import pytest

from xeno.origincache import CACHE_FILE_NAME, MAX_ENTRIES, OriginCache
from xeno.sources.helius import Helius, Origin


class CountingHttp:
    """Zaehlt Anfragen und liefert immer dieselbe kurze Historie."""

    def __init__(self, pages=None) -> None:
        self.calls = 0
        self.pages = pages if pages is not None else [
            {
                "timestamp": 100,
                "nativeTransfers": [
                    {
                        "fromUserAccount": "geldgeber",
                        "toUserAccount": "w",
                        "amount": 9_000_000,
                    }
                ],
            }
        ]

    def get(self, url, params=None, **kwargs):
        self.calls += 1
        return self.pages


@pytest.fixture
def cache(tmp_path):
    return OriginCache(tmp_path / CACHE_FILE_NAME)


class TestCaching:
    def test_a_repeated_lookup_costs_nothing(self, cache):
        """Der eigentliche Zweck: die Wiederholungspruefung darf nichts kosten."""
        http = CountingHttp()
        helius = Helius(api_key="k", http=http)

        helius.origins(["w1", "w2", "w3"], cache=cache)
        assert http.calls == 3

        helius.origins(["w1", "w2", "w3"], cache=cache)
        assert http.calls == 3  # keine einzige weitere Anfrage

    def test_known_wallets_do_not_use_up_the_budget(self, cache):
        """Das Budget begrenzt Anfragen, nicht Ergebnisse - sonst blieben
        bekannte Wallets bei knappem Budget grundlos aussen vor."""
        http = CountingHttp()
        helius = Helius(api_key="k", http=http)

        for name in ("w1", "w2", "w3"):
            cache.put(Origin(wallet=name, funder="geldgeber", tx_count=5))

        result = helius.origins(["w1", "w2", "w3", "w4", "w5"], budget=2, cache=cache)
        assert http.calls == 2            # nur die beiden unbekannten
        assert len(result) == 5           # trotzdem alle fuenf Ergebnisse

    def test_without_a_cache_nothing_changes(self):
        http = CountingHttp()
        helius = Helius(api_key="k", http=http)
        helius.origins(["w1", "w2"], cache=None)
        helius.origins(["w1", "w2"], cache=None)
        assert http.calls == 4

    def test_the_budget_still_caps_fresh_lookups(self, cache):
        http = CountingHttp()
        helius = Helius(api_key="k", http=http)
        helius.origins([f"w{i}" for i in range(30)], budget=4, cache=cache)
        assert http.calls == 4

    def test_a_failed_lookup_is_not_remembered(self, cache):
        """Sonst gaelte ein Netzwerkausfall fuer immer als Ergebnis."""
        class Broken:
            calls = 0

            def get(self, *a, **kw):
                Broken.calls += 1
                from xeno.net import HttpError

                raise HttpError("weg")

        helius = Helius(api_key="k", http=Broken())
        helius.origins(["w1"], cache=cache)
        assert len(cache) == 0


class TestPersistence:
    def test_survives_a_restart(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        first = OriginCache(path)
        first.put(Origin(wallet="w1", funder="quelle", funded_at=100, tx_count=7))
        first.save(force=True)

        second = OriginCache(path)
        found = second.get("w1")
        assert found is not None
        assert found.funder == "quelle"
        assert found.tx_count == 7

    def test_established_flag_survives(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        first = OriginCache(path)
        first.put(Origin(wallet="w1", established=True, tx_count=100))
        first.save(force=True)
        assert OriginCache(path).get("w1").established is True

    def test_a_broken_file_does_not_stop_the_start(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        path.write_text("{kaputt", encoding="utf-8")
        assert len(OriginCache(path)) == 0

    def test_writing_is_throttled(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        cache = OriginCache(path)
        cache.put(Origin(wallet="w1", funder="q"))
        cache.save(force=True)

        cache.put(Origin(wallet="w2", funder="q"))
        cache.save()  # zu frueh - wird uebersprungen
        assert "w2" not in json.loads(path.read_text())["wallets"]

        cache.save(force=True)
        assert "w2" in json.loads(path.read_text())["wallets"]

    def test_nothing_to_save_writes_nothing(self, tmp_path):
        path = tmp_path / CACHE_FILE_NAME
        OriginCache(path).save(force=True)
        assert not path.exists()

    def test_an_unwritable_place_does_not_raise(self, tmp_path, monkeypatch):
        cache = OriginCache(tmp_path / CACHE_FILE_NAME)
        cache.put(Origin(wallet="w1", funder="q"))

        def boom(*args, **kwargs):
            raise OSError("Platte voll")

        monkeypatch.setattr("tempfile.mkstemp", boom)
        cache.save(force=True)  # darf nicht werfen


class TestPruning:
    def test_oldest_entries_go_first(self, tmp_path):
        cache = OriginCache(tmp_path / CACHE_FILE_NAME)
        for i in range(MAX_ENTRIES + 100):
            cache.put(Origin(wallet=f"w{i}", funder="q"))
        cache.save(force=True)
        assert len(cache) <= MAX_ENTRIES
        # Die zuletzt eingetragenen ueberleben.
        assert cache.get(f"w{MAX_ENTRIES + 99}") is not None


def test_hit_rate_is_reported(cache):
    cache.put(Origin(wallet="w1", funder="q"))
    cache.get("w1")
    cache.get("unbekannt")
    assert cache.hit_rate == 50.0
