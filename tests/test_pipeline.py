"""Tests fuer Vorfilter, Bewertung und Chain-Parsing."""

from __future__ import annotations

from conftest import MINT, make_candidate, make_data, rugcheck

from xeno.analyzer import build_report
from xeno.chain import build_distribution, distribution_from_rugcheck, parse_mint_account
from xeno.config import Settings
from xeno.discovery import merge_candidates
from xeno.known import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from xeno.models import Finding, RiskReport, Severity, Verdict
from xeno.screen import screen
from xeno.sources.rugcheck import RugCheckReport


class TestScreen:
    def test_healthy_candidate_passes(self, screen_thresholds):
        assert screen(make_candidate(), screen_thresholds).passed

    def test_rejects_thin_liquidity(self, screen_thresholds):
        result = screen(make_candidate(liquidity_usd=100.0), screen_thresholds)
        assert not result.passed
        assert any("Liquiditaet" in r for r in result.reasons)

    def test_rejects_too_young(self, screen_thresholds):
        result = screen(make_candidate(age_minutes=1), screen_thresholds)
        assert not result.passed
        assert any("frisch" in r for r in result.reasons)

    def test_rejects_too_old(self, screen_thresholds):
        result = screen(make_candidate(age_minutes=100 * 60), screen_thresholds)
        assert not result.passed
        assert any("alt" in r for r in result.reasons)

    def test_rejects_sell_pressure(self, screen_thresholds):
        result = screen(make_candidate(buys_h1=10, sells_h1=100), screen_thresholds)
        assert not result.passed
        assert any("Verkaufsdruck" in r for r in result.reasons)

    def test_rejects_wash_trading_pattern(self, screen_thresholds):
        """Sehr hohes Volumen bei duenner Liquiditaet deutet auf Kreisgeschaefte."""
        result = screen(
            make_candidate(liquidity_usd=6_000.0, volume_h1_usd=2_000_000.0),
            screen_thresholds,
        )
        assert not result.passed
        assert any("Volumen/Liquiditaet" in r for r in result.reasons)

    def test_collects_all_reasons_not_just_first(self, screen_thresholds):
        result = screen(
            make_candidate(liquidity_usd=10.0, volume_h1_usd=1.0, buyers_h1=1),
            screen_thresholds,
        )
        assert len(result.reasons) >= 3

    def test_missing_data_does_not_pass_silently(self, screen_thresholds):
        from xeno.models import TokenCandidate

        result = screen(TokenCandidate(mint=MINT), screen_thresholds)
        assert not result.passed


class TestMerge:
    def test_keeps_the_richer_record(self):
        sparse = make_candidate(liquidity_usd=None, volume_h1_usd=None, buyers_h1=None)
        rich = make_candidate()
        merged = merge_candidates([[sparse], [rich]])
        assert len(merged) == 1
        assert merged[0].liquidity_usd == rich.liquidity_usd

    def test_deduplicates_by_mint(self):
        assert len(merge_candidates([[make_candidate()], [make_candidate()]])) == 1


class TestMintParsing:
    def _account(self, program: str, info: dict) -> dict:
        return {"owner": program, "data": {"parsed": {"type": "mint", "info": info}}}

    def test_parses_classic_mint(self):
        info = parse_mint_account(
            MINT,
            self._account(
                TOKEN_PROGRAM,
                {"decimals": 6, "supply": "1000000000000000", "mintAuthority": None},
            ),
        )
        assert info is not None
        assert info.decimals == 6
        assert info.supply == 1_000_000_000
        assert info.is_token_2022 is False

    def test_parses_token2022_with_metadata(self):
        info = parse_mint_account(
            MINT,
            self._account(
                TOKEN_2022_PROGRAM,
                {
                    "decimals": 6,
                    "supply": "1000000000000000",
                    "extensions": [
                        {
                            "extension": "tokenMetadata",
                            "state": {"name": "Momota", "symbol": "MOMOTA"},
                        }
                    ],
                },
            ),
        )
        assert info is not None
        assert info.is_token_2022 is True
        assert info.symbol == "MOMOTA"

    def test_rejects_non_token_account(self):
        assert parse_mint_account(MINT, {"owner": "SomeOtherProgram", "data": {}}) is None

    def test_handles_missing_account(self):
        assert parse_mint_account(MINT, None) is None


class TestDistribution:
    def test_pool_owner_is_tagged_and_excluded(self):
        """Der entscheidende Fall: der Pool haelt fast alles und darf nicht zaehlen."""
        largest = [
            {"address": "vault", "amount": "950", "uiAmount": 950.0},
            {"address": "whale", "amount": "30", "uiAmount": 30.0},
        ]
        owners = ["675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "private1"]
        distribution = build_distribution(largest, owners, total_supply=1000.0)

        assert distribution.excluded_pct == 95.0
        assert distribution.largest_pct == 3.0
        assert distribution.top10_pct == 3.0

    def test_extra_pool_addresses_are_honoured(self):
        largest = [{"address": "lpvault", "amount": "800", "uiAmount": 800.0}]
        distribution = build_distribution(
            largest, ["unknown"], total_supply=1000.0, extra_pool_addresses={"lpvault"}
        )
        assert distribution.excluded_pct == 80.0
        assert distribution.largest_pct == 0.0

    def test_rugcheck_distribution_excludes_pools(self):
        report = rugcheck(
            topHolders=[
                {"address": "a", "owner": "poolowner", "pct": 90.0, "uiAmount": 900.0},
                {"address": "b", "owner": "private", "pct": 4.0, "uiAmount": 40.0},
            ],
            markets=[{"pubkey": "poolowner"}],
        )
        distribution = distribution_from_rugcheck(
            report.top_holders, 1000.0, pool_addresses=report.pool_addresses
        )
        assert distribution.largest_pct == 4.0
        assert distribution.excluded_pct == 90.0


class TestRugCheckReport:
    def test_zero_holders_with_top_holders_is_treated_as_unknown(self):
        """Widerspruch in den Daten - der Zaehler ist noch nicht befuellt."""
        report = rugcheck(totalHolders=0, topHolders=[{"address": "a", "pct": 5.0}])
        assert report.total_holders is None

    def test_real_zero_is_kept(self):
        assert rugcheck(totalHolders=0, topHolders=[]).total_holders == 0

    def test_insider_pct_sums_flagged_wallets(self):
        report = rugcheck(
            topHolders=[
                {"address": "a", "pct": 10.0, "insider": True},
                {"address": "b", "pct": 5.0, "insider": True},
                {"address": "c", "pct": 20.0, "insider": False},
            ]
        )
        assert report.insider_pct == 15.0

    def test_empty_report_is_safe_to_query(self):
        empty = RugCheckReport(mint=MINT, raw={})
        assert empty.available is False
        assert empty.top_holders == []
        assert empty.lp_locked_pct is None
        assert empty.total_holders is None


class TestVerdict:
    def _report(self, findings: list[Finding], mint_info=True) -> RiskReport:
        from conftest import make_mint_info

        report = RiskReport(mint=MINT, mint_info=make_mint_info() if mint_info else None)
        report.findings = findings
        return report

    def _finding(self, severity: Severity, code: str = "x") -> Finding:
        return Finding(check="t", code=code, severity=severity, message="m")

    def test_critical_always_means_avoid(self):
        report = self._report([self._finding(Severity.CRITICAL)])
        assert report.verdict is Verdict.AVOID

    def test_clean_report_is_ok(self):
        report = self._report([self._finding(Severity.INFO)])
        assert report.score == 100
        assert report.verdict is Verdict.OK

    def test_missing_mint_info_is_unknown_not_ok(self):
        report = self._report([self._finding(Severity.INFO)], mint_info=False)
        assert report.verdict is Verdict.UNKNOWN

    def test_data_gap_prevents_ok(self):
        """Fehlende Daten duerfen nicht wie ein bestandener Test wirken."""
        report = self._report([self._finding(Severity.INFO, code="holders_unavailable")])
        assert report.score == 100
        assert report.verdict is Verdict.CAUTION

    def test_truncated_distribution_is_not_a_gap(self):
        """Dieser Hinweis erscheint immer - sonst waere OK nie erreichbar."""
        report = self._report([self._finding(Severity.INFO, code="distribution_truncated")])
        assert report.verdict is Verdict.OK

    def test_score_floors_at_zero(self):
        report = self._report([self._finding(Severity.CRITICAL)] * 3)
        assert report.score == 0

    def test_severities_accumulate(self):
        report = self._report(
            [self._finding(Severity.MEDIUM), self._finding(Severity.HIGH)]
        )
        assert report.score == 100 - 15 - 30


class TestBuildReport:
    def test_produces_findings_without_network(self):
        data = make_data(
            candidate=make_candidate(),
            rugcheck=rugcheck(markets=[{"lp": {"lpLockedPct": 100}}], tokenMeta={"symbol": "TT"}),
        )
        report = build_report(data, Settings())
        assert report.findings
        assert report.verdict in set(Verdict)

    def test_errors_are_carried_through(self):
        data = make_data(errors=["RPC weg"])
        assert "RPC weg" in build_report(data, Settings()).errors


class TestColorHandling:
    """Unter Windows muessen ANSI-Codes freigeschaltet werden - sonst stehen
    Zeichenfolgen wie ``←[91m`` mitten in der Ausgabe."""

    def _stream(self, tty: bool):
        class Stream:
            def isatty(self):
                return tty

        return Stream()

    def test_no_color_when_output_is_redirected(self, monkeypatch):
        import xeno.report as report

        monkeypatch.setattr(report, "_ansi_ready", None)
        assert report.use_color(self._stream(False)) is False

    def test_no_color_env_wins(self, monkeypatch):
        import xeno.report as report

        monkeypatch.setenv("NO_COLOR", "1")
        assert report.use_color(self._stream(True)) is False

    def test_windows_without_console_support_disables_color(self, monkeypatch):
        import sys

        import xeno.report as report

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(report, "_ansi_ready", None)
        monkeypatch.delenv("NO_COLOR", raising=False)
        # ctypes.windll gibt es hier nicht - der Zugriff scheitert und
        # genau dann darf keine Farbe ausgegeben werden.
        assert report.use_color(self._stream(True)) is False

    def test_result_is_cached(self, monkeypatch):
        import xeno.report as report

        monkeypatch.setattr(report, "_ansi_ready", True)
        assert report.enable_ansi() is True
