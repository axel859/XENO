"""Tests der Einzelpruefungen."""

from __future__ import annotations

from conftest import (
    make_candidate,
    make_data,
    make_distribution,
    make_mint_info,
    rugcheck,
)

from xeno.checks.authority import check_authorities
from xeno.checks.bundling import check_bundling, find_balance_clusters
from xeno.checks.creator import check_creator
from xeno.checks.extensions import check_extensions
from xeno.checks.holders import check_holders
from xeno.checks.liquidity import check_liquidity
from xeno.checks.tradability import check_tradability
from xeno.known import TOKEN_2022_PROGRAM
from xeno.models import Severity
from xeno.sources.jupiter import RoundTrip


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def worst(findings) -> Severity:
    order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    return max((f.severity for f in findings), key=order.index, default=Severity.INFO)


class TestAuthorities:
    def test_revoked_authorities_are_clean(self, risk):
        result = check_authorities(make_data(), risk)
        assert codes(result) == {"authorities_revoked"}

    def test_active_mint_authority_is_critical(self, risk):
        data = make_data(mint_info=make_mint_info(mint_authority="Attacker1111"))
        result = check_authorities(data, risk)
        assert "mint_authority_active" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_active_freeze_authority_is_critical(self, risk):
        data = make_data(mint_info=make_mint_info(freeze_authority="Attacker1111"))
        result = check_authorities(data, risk)
        assert "freeze_authority_active" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_falls_back_to_rugcheck_without_rpc(self, risk):
        data = make_data(
            mint_info=None, rugcheck=rugcheck(mintAuthority="Attacker1111", tokenMeta={})
        )
        result = check_authorities(data, risk)
        assert "mint_authority_active" in codes(result)

    def test_reports_gap_when_nothing_known(self, risk):
        data = make_data(mint_info=None, rugcheck=rugcheck())
        assert "authority_unknown" in codes(check_authorities(data, risk))


class TestToken2022Extensions:
    def _data(self, extension: str, state: dict):
        return make_data(
            mint_info=make_mint_info(
                program=TOKEN_2022_PROGRAM,
                extensions=[{"extension": extension, "state": state}],
            )
        )

    def test_permanent_delegate_is_critical(self, risk):
        result = check_extensions(self._data("permanentDelegate", {"delegate": "Bad11"}), risk)
        assert "permanent_delegate" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_transfer_hook_is_critical(self, risk):
        result = check_extensions(self._data("transferHook", {"programId": "Hook11"}), risk)
        assert "transfer_hook" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_default_frozen_is_critical(self, risk):
        result = check_extensions(self._data("defaultAccountState", {"accountState": "frozen"}), risk)
        assert "default_frozen" in codes(result)

    def test_default_initialized_is_fine(self, risk):
        data = self._data("defaultAccountState", {"accountState": "initialized"})
        assert "default_frozen" not in codes(check_extensions(data, risk))

    def test_transfer_fee_scales_with_size(self, risk):
        small = self._data(
            "transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 100}}
        )
        large = self._data(
            "transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 2000}}
        )
        assert worst(check_extensions(small, risk)) is Severity.MEDIUM
        assert worst(check_extensions(large, risk)) is Severity.CRITICAL

    def test_fee_authority_flagged_even_at_zero_fee(self, risk):
        data = self._data(
            "transferFeeConfig",
            {
                "newerTransferFee": {"transferFeeBasisPoints": 0},
                "transferFeeConfigAuthority": "Boss11",
            },
        )
        result = check_extensions(data, risk)
        assert "transfer_fee_authority" in codes(result)

    def test_metadata_extensions_are_benign(self, risk):
        data = self._data("tokenMetadata", {"name": "Test"})
        assert codes(check_extensions(data, risk)) == {"token2022_clean"}

    def test_classic_spl_token_is_skipped(self, risk):
        assert check_extensions(make_data(), risk) == []


class TestHolders:
    def test_flags_dominant_wallet(self, risk):
        data = make_data(distribution=make_distribution([35.0, 2.0]))
        result = check_holders(data, risk)
        assert "single_holder_dominant" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_flags_top10_concentration(self, risk):
        data = make_data(distribution=make_distribution([9.0] * 6))
        assert "top10_concentration" in codes(check_holders(data, risk))

    def test_pools_and_burns_are_excluded(self, risk):
        """Ein Pool mit 95% darf keine Konzentrationswarnung ausloesen."""
        data = make_data(
            distribution=make_distribution(
                [95.0, 2.0, 1.0], tags=["Pool: Raydium AMM v4", "", ""]
            )
        )
        result = check_holders(data, risk)
        assert "single_holder_dominant" not in codes(result)
        assert "distribution_ok" in codes(result)

    def test_missing_data_is_a_gap_not_a_pass(self, risk):
        data = make_data(distribution=None)
        result = check_holders(data, risk)
        assert "holders_unavailable" in codes(result)
        assert worst(result) is Severity.HIGH

    def test_unknown_holder_count_is_not_penalised(self, risk):
        data = make_data(distribution=make_distribution([5.0, 3.0], holder_count=None))
        assert "almost_no_holders" not in codes(check_holders(data, risk))


class TestLiquidity:
    def test_unlocked_lp_is_critical(self, risk):
        data = make_data(rugcheck=rugcheck(markets=[{"lp": {"lpLockedPct": 0}}]))
        result = check_liquidity(data, risk)
        assert "lp_unlocked" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_locked_lp_passes(self, risk):
        data = make_data(
            rugcheck=rugcheck(markets=[{"lp": {"lpLockedPct": 100}}]),
            candidate=make_candidate(),
        )
        assert "lp_locked" in codes(check_liquidity(data, risk))

    def test_rugged_flag_is_critical(self, risk):
        data = make_data(rugcheck=rugcheck(rugged=True, markets=[{"lp": {"lpLockedPct": 100}}]))
        result = check_liquidity(data, risk)
        assert "already_rugged" in codes(result)

    def test_dust_liquidity_flagged(self, risk):
        data = make_data(
            rugcheck=rugcheck(markets=[{"lp": {"lpLockedPct": 100}}]),
            candidate=make_candidate(liquidity_usd=500.0),
        )
        assert "liquidity_dust" in codes(check_liquidity(data, risk))

    def test_highest_lock_across_markets_wins(self, risk):
        data = make_data(
            rugcheck=rugcheck(
                markets=[{"lp": {"lpLockedPct": 10}}, {"lp": {"lpLockedPct": 100}}]
            )
        )
        assert "lp_locked" in codes(check_liquidity(data, risk))


class TestBundling:
    def test_detects_equal_sized_wallets(self):
        distribution = make_distribution([5.0, 5.0, 4.9, 1.0])
        clusters = find_balance_clusters(distribution.holders)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_ignores_dust_wallets(self):
        distribution = make_distribution([0.1, 0.1, 0.1])
        assert find_balance_clusters(distribution.holders) == []

    def test_excluded_holders_not_clustered(self):
        distribution = make_distribution(
            [5.0, 5.0, 5.0], tags=["Pool: Raydium AMM v4", "Burn", "Lock: Streamflow"]
        )
        assert find_balance_clusters(distribution.holders) == []

    def test_insider_supply_is_flagged(self, risk):
        data = make_data(
            rugcheck=rugcheck(
                topHolders=[
                    {"address": "a", "owner": "o1", "pct": 25.0, "insider": True},
                    {"address": "b", "owner": "o2", "pct": 20.0, "insider": True},
                ]
            )
        )
        result = check_bundling(data, risk)
        assert "insider_supply" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_missing_holders_reported_as_unchecked(self, risk):
        data = make_data(distribution=None)
        assert "bundling_unchecked" in codes(check_bundling(data, risk))


class TestCreator:
    def test_rug_history_is_critical(self, risk):
        data = make_data(
            rugcheck=rugcheck(
                creator="Dev111",
                risks=[
                    {
                        "name": "Creator history of rugged tokens",
                        "level": "danger",
                        "description": "Creator has a history of rugging tokens.",
                    }
                ],
            )
        )
        result = check_creator(data, risk)
        assert "creator_rug_history" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_serial_deployer_flagged(self, risk):
        data = make_data(
            rugcheck=rugcheck(creator="Dev111", creatorTokens=[{}] * 25)
        )
        assert "serial_deployer" in codes(check_creator(data, risk))

    def test_few_launches_are_medium(self, risk):
        data = make_data(rugcheck=rugcheck(creator="Dev111", creatorTokens=[{}] * 4))
        result = check_creator(data, risk)
        assert "repeat_deployer" in codes(result)
        assert worst(result) is Severity.MEDIUM

    def test_no_rugcheck_data_yields_nothing(self, risk):
        assert check_creator(make_data(rugcheck=rugcheck()), risk) == []


class TestTradability:
    def test_healthy_round_trip_passes(self, risk):
        trip = RoundTrip(
            buy_ok=True, sell_ok=True, lamports_in=100_000_000, lamports_out=96_000_000
        )
        data = make_data(round_trip=trip)
        assert "round_trip_ok" in codes(check_tradability(data, risk))

    def test_missing_sell_route_on_old_pool_is_honeypot(self, risk):
        trip = RoundTrip(buy_ok=True, sell_ok=False, lamports_in=100_000_000)
        data = make_data(round_trip=trip, candidate=make_candidate(age_minutes=600))
        result = check_tradability(data, risk)
        assert "no_sell_route" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_missing_sell_route_on_fresh_pool_is_downgraded(self, risk):
        """Frische Pools sind oft nur noch nicht indexiert - kein Honeypot-Urteil."""
        trip = RoundTrip(buy_ok=True, sell_ok=False, lamports_in=100_000_000)
        data = make_data(round_trip=trip, candidate=make_candidate(age_minutes=5))
        result = check_tradability(data, risk)
        assert "sell_route_missing_young" in codes(result)
        assert worst(result) is Severity.MEDIUM

    def test_impossible_profit_is_treated_as_unreliable(self, risk):
        """Mehr zurueck als eingesetzt kann nicht stimmen - nicht als Signal werten."""
        trip = RoundTrip(
            buy_ok=True, sell_ok=True, lamports_in=100_000_000, lamports_out=190_000_000
        )
        assert trip.unreliable is True
        assert trip.loss_pct is None
        data = make_data(round_trip=trip)
        result = check_tradability(data, risk)
        assert "round_trip_unreliable" in codes(result)
        assert worst(result) is Severity.INFO

    def test_heavy_loss_is_critical(self, risk):
        trip = RoundTrip(
            buy_ok=True, sell_ok=True, lamports_in=100_000_000, lamports_out=30_000_000
        )
        data = make_data(round_trip=trip)
        result = check_tradability(data, risk)
        assert "round_trip_lossy" in codes(result)
        assert worst(result) is Severity.CRITICAL

    def test_skipped_when_not_run(self, risk):
        assert check_tradability(make_data(round_trip=None), risk) == []
