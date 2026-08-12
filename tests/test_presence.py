"""Tests fuer Aussenauftritt und Umschlag.

Beide Werte stammen aus Daten, die XENO ohnehin abruft - sie wurden bisher
nur nicht gelesen. Entsprechend billig sind sie, und entsprechend
zurueckhaltend faellt ihre Gewichtung aus: hinterlegte Links kann jeder
eintragen.
"""

from __future__ import annotations

from conftest import MINT, make_candidate

from xeno.checks.base import TokenData
from xeno.checks.presence import MIN_MCAP_FOR_TURNOVER, check_presence
from xeno.config import RiskThresholds
from xeno.models import Severity, TokenCandidate
from xeno.sources.dexscreener import DexScreener, _links

RISK = RiskThresholds()


def run(**kwargs) -> list:
    candidate = make_candidate(**kwargs)
    return check_presence(TokenData(mint=MINT, candidate=candidate), RISK)


def codes(findings) -> set[str]:
    return {f.code for f in findings}


class TestLinkParsing:
    def test_reads_socials_and_websites(self):
        info = {
            "socials": [
                {"type": "twitter", "url": "https://x.com/coin"},
                {"type": "telegram", "url": "https://t.me/coin"},
            ],
            "websites": [{"url": "https://coin.org", "label": "Website"}],
        }
        socials, websites = _links(info)
        assert socials == {
            "twitter": "https://x.com/coin",
            "telegram": "https://t.me/coin",
        }
        assert websites == ["https://coin.org"]

    def test_missing_block_is_empty(self):
        assert _links(None) == ({}, [])
        assert _links({}) == ({}, [])

    def test_duplicates_keep_the_first(self):
        info = {"socials": [
            {"type": "twitter", "url": "eins"},
            {"type": "twitter", "url": "zwei"},
        ]}
        assert _links(info)[0] == {"twitter": "eins"}

    def test_junk_entries_are_skipped(self):
        info = {"socials": ["kaputt", {"type": "", "url": "x"}], "websites": [{}]}
        assert _links(info) == ({}, [])

    def test_a_real_pair_carries_the_links_through(self):
        pair = {
            "baseToken": {"address": MINT, "symbol": "WIF", "name": "dogwifhat"},
            "marketCap": 140_000_000,
            "fdv": 141_000_000,
            "info": {"socials": [{"type": "twitter", "url": "https://x.com/dogwifcoin"}]},
        }
        candidate = DexScreener.as_candidate(DexScreener(http=None), pair)
        assert candidate.socials["twitter"] == "https://x.com/dogwifcoin"
        # Die umlaufende Bewertung gewinnt gegen die FDV.
        assert candidate.mcap_usd == 140_000_000


class TestMarketCap:
    def test_falls_back_to_fdv(self):
        assert TokenCandidate(mint=MINT, fdv_usd=50_000).mcap_usd == 50_000

    def test_prefers_the_circulating_value(self):
        candidate = TokenCandidate(mint=MINT, fdv_usd=90_000, market_cap_usd=30_000)
        assert candidate.mcap_usd == 30_000

    def test_turnover_needs_both_numbers(self):
        assert TokenCandidate(mint=MINT, volume_h1_usd=500).volume_to_mcap is None
        assert TokenCandidate(mint=MINT, fdv_usd=1000).volume_to_mcap is None

    def test_turnover_is_volume_over_valuation(self):
        candidate = TokenCandidate(mint=MINT, fdv_usd=100_000, volume_h1_usd=5_000)
        assert candidate.volume_to_mcap == 0.05


class TestPresenceCheck:
    def test_missing_links_are_a_weak_signal(self):
        result = run(socials={}, websites=[])
        found = next(f for f in result if f.code == "no_public_presence")
        assert found.severity is Severity.LOW

    def test_links_are_listed(self):
        result = run(socials={"twitter": "https://x.com/c"}, websites=["https://c.io"])
        assert "has_public_presence" in codes(result)

    def test_no_candidate_yields_nothing(self):
        assert check_presence(TokenData(mint=MINT), RISK) == []


class TestTurnover:
    def test_frozen_supply_is_flagged(self):
        result = run(market_cap_usd=500_000, volume_h1_usd=2_000)
        assert "supply_barely_trades" in codes(result)

    def test_healthy_turnover_passes(self):
        result = run(market_cap_usd=100_000, volume_h1_usd=40_000)
        assert "supply_barely_trades" not in codes(result)
        assert "low_turnover" not in codes(result)

    def test_tiny_tokens_are_not_judged_on_turnover(self):
        """Bei ein paar tausend Dollar Bewertung ist jedes Verhaeltnis Zufall."""
        result = run(market_cap_usd=MIN_MCAP_FOR_TURNOVER - 1, volume_h1_usd=1)
        assert "supply_barely_trades" not in codes(result)
        assert "low_turnover" not in codes(result)
