"""Tests des Verbrauchszaehlers.

Der Anlass war ein Totalausfall: XENO gab pro Token rund 600 Credits aus und
raeumte ein Monatskontingent in einer Nacht leer, ohne dass das irgendwo
sichtbar gewesen waere. Der Fehler dahinter war ein Denkfehler - ich hatte
**Anfragen** gezaehlt, abgerechnet wird aber in **Credits**, und ein Aufruf
der geparsten Transaktionen kostet so viel wie hundert gewoehnliche.

Geprueft wird deshalb beides: dass richtig gerechnet wird, und dass der
Deckel tatsaechlich haelt.
"""

from __future__ import annotations

import json

import pytest

from xeno.credits import (
    ENHANCED,
    RPC,
    RPC_LARGE,
    CreditMeter,
    days_left_in_month,
    provider_of,
)


@pytest.fixture
def meter(tmp_path):
    return CreditMeter(
        "https://mainnet.helius-rpc.com/?api-key=x",
        path=tmp_path / "credits.json",
        monthly_cap=30_000,
    )


class TestProviderDetection:
    def test_helius(self):
        assert provider_of("https://mainnet.helius-rpc.com/?api-key=abc") == "helius"

    def test_quicknode(self):
        assert provider_of("https://x.solana-mainnet.quiknode.pro/abc/") == "quicknode"

    def test_public(self):
        assert provider_of("https://api.mainnet-beta.solana.com") == "public"
        assert provider_of("") == "public"

    def test_unknown_provider_is_priced_carefully(self, tmp_path):
        """Lieber zu frueh bremsen als noch einmal in eine Sperre laufen."""
        unknown = CreditMeter("https://irgendein-rpc.example/x", path=tmp_path / "c.json")
        assert unknown.cost(RPC) >= 30


class TestPricing:
    def test_helius_prices(self, meter):
        assert meter.cost(RPC) == 1
        assert meter.cost(ENHANCED) == 100

    def test_quicknode_prices(self, tmp_path):
        qn = CreditMeter("https://x.quiknode.pro/y/", path=tmp_path / "c.json")
        assert qn.cost(RPC) == 30
        assert qn.cost(RPC_LARGE) == 120

    def test_the_mistake_that_caused_all_this(self, meter):
        """Eine geparste Transaktion kostet so viel wie hundert normale
        Aufrufe. Wer Anfragen zaehlt, rechnet um Faktor hundert falsch."""
        assert meter.cost(ENHANCED) == meter.cost(RPC, 100)

    def test_counts_multiply(self, meter):
        assert meter.cost(ENHANCED, 5) == 500


class TestBudget:
    def test_public_rpc_has_no_cap(self, tmp_path):
        free = CreditMeter("https://api.mainnet-beta.solana.com", path=tmp_path / "c.json")
        assert free.capped is False
        assert free.can_afford(ENHANCED, 10_000) is True
        assert free.spend(ENHANCED, 10_000) == 0

    def test_the_daily_share_is_the_month_over_remaining_days(self, meter):
        assert meter.daily_allowance == 30_000 // days_left_in_month()

    def test_spending_reduces_what_is_left(self, meter):
        allowance = meter.daily_allowance
        meter.spend(ENHANCED, 3)
        assert meter.remaining_today == allowance - 300

    def test_the_cap_actually_holds(self, meter):
        meter.spend(ENHANCED, meter.daily_allowance // 100 + 1)
        assert meter.can_afford(ENHANCED) is False

    def test_spending_it_all_leaves_nothing(self, tmp_path):
        small = CreditMeter("https://helius", path=tmp_path / "a.json", monthly_cap=30_000)
        allowance = small.daily_allowance
        small.spend(RPC, allowance)
        assert small.remaining_today == 0
        assert small.remaining_month == 30_000 - allowance

    def test_todays_spending_does_not_enlarge_todays_budget(self, meter):
        """Der Fehler, den der Test gefunden hat: zaehlte der heutige Verbrauch
        in die Verteilung mit hinein, wuechse das Tagesbudget mit jeder
        Ausgabe - und der Deckel griffe nie."""
        before = meter.daily_allowance
        meter.spend(ENHANCED, 3)
        assert meter.daily_allowance == before

    def test_a_frugal_month_leaves_a_larger_daily_share(self, tmp_path):
        sparsam = CreditMeter("https://helius", path=tmp_path / "a.json", monthly_cap=30_000)
        verschwenderisch = CreditMeter(
            "https://helius", path=tmp_path / "b.json", monthly_cap=30_000
        )
        verschwenderisch.month_spent = 20_000
        assert sparsam.daily_allowance > verschwenderisch.daily_allowance


class TestAffordable:
    def test_returns_how_many_fit(self, meter):
        # Tagesbudget bei 30.000/Monat liegt bei rund 1.000 Credits.
        assert meter.affordable(ENHANCED, 100) == meter.daily_allowance // 100

    def test_never_more_than_wanted(self, meter):
        assert meter.affordable(RPC, 5) == 5

    def test_zero_when_exhausted(self, meter):
        meter.spend(RPC, meter.daily_allowance)
        assert meter.affordable(ENHANCED, 10) == 0

    def test_uncapped_gives_everything(self, tmp_path):
        free = CreditMeter("", path=tmp_path / "c.json")
        assert free.affordable(ENHANCED, 42) == 42


class TestPersistence:
    def test_survives_a_restart(self, tmp_path):
        path = tmp_path / "credits.json"
        first = CreditMeter("https://helius", path=path, monthly_cap=30_000)
        first.spend(ENHANCED, 4)
        first.save(force=True)

        second = CreditMeter("https://helius", path=path, monthly_cap=30_000)
        assert second.day_spent == 400
        assert second.month_spent == 400

    def test_counters_from_another_day_are_ignored(self, tmp_path):
        """Sonst gaelte das Budget von gestern heute als verbraucht."""
        path = tmp_path / "credits.json"
        path.write_text(
            json.dumps(
                {
                    "month": "1999-01",
                    "month_spent": 999_999,
                    "day": "1999-01-01",
                    "day_spent": 999_999,
                }
            ),
            encoding="utf-8",
        )
        meter = CreditMeter("https://helius", path=path, monthly_cap=30_000)
        assert meter.day_spent == 0
        assert meter.month_spent == 0

    def test_a_broken_file_does_not_stop_the_start(self, tmp_path):
        path = tmp_path / "credits.json"
        path.write_text("{kaputt", encoding="utf-8")
        assert CreditMeter("https://helius", path=path).day_spent == 0

    def test_an_unwritable_place_does_not_raise(self, tmp_path, monkeypatch):
        meter = CreditMeter("https://helius", path=tmp_path / "c.json", monthly_cap=100)
        meter.spend(RPC)

        def boom(*args, **kwargs):
            raise OSError("Platte voll")

        monkeypatch.setattr("tempfile.mkstemp", boom)
        meter.save(force=True)


class TestOverride:
    def test_environment_sets_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XENO_CREDIT_CAP", "5000")
        meter = CreditMeter("https://helius", path=tmp_path / "c.json")
        assert meter.monthly_cap == 5000

    def test_junk_falls_back_to_the_free_tier(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XENO_CREDIT_CAP", "keine-zahl")
        meter = CreditMeter("https://helius", path=tmp_path / "c.json")
        assert meter.monthly_cap > 0


def test_denials_are_counted(meter):
    meter.deny()
    meter.deny()
    assert meter.snapshot()["denied_today"] == 2


def test_summary_is_readable(meter):
    assert "helius" in meter.summary()
