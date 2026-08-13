"""Tests der Call-Regel.

Ein Call ist die einzige Stelle, an der XENO von sich aus etwas vorschlaegt.
Alle uebrigen Meldungen sagen nur, dass sich etwas geaendert hat.

Der Fehler, der hier lauert, ist derselbe wie schon einmal: aus "nichts
Schlimmes gefunden" wird stillschweigend "sieht gut aus". Deshalb pruefen
die Tests vor allem die Grenzen - was einen Call **verhindert**.
"""

from __future__ import annotations

import pytest
from conftest import MINT, make_candidate, make_mint_info

from xeno.calls import BLOCKERS, MIN_SIGNALS, evaluate, worth_calling
from xeno.models import Finding, RiskReport, Severity


def finding(code: str, severity: Severity = Severity.INFO) -> Finding:
    return Finding(check="test", code=code, severity=severity, message=code)


#: Die Befunde eines Tokens, bei dem alles zusammenkommt.
PERFECT = [
    "uptrend",
    "trading_looks_organic",
    "trade_sizes_look_human",
    "funding_looks_independent",
    "round_trip_ok",
    "distribution_ok",
    "no_bundling_signal",
    "has_public_presence",
    "authorities_revoked",
    "lp_locked",
]


def report(codes=None, *, buyers=60, severity=Severity.INFO, **kwargs) -> RiskReport:
    codes = PERFECT if codes is None else codes
    return RiskReport(
        mint=MINT,
        symbol="TEST",
        candidate=make_candidate(buyers_h1=buyers, price_usd=0.001, **kwargs),
        # Ohne Mint-Daten lautet das Urteil UNKNOWN - dann blockiert schon
        # die Grundbedingung, und der Test pruefte nichts von dem, was er soll.
        mint_info=make_mint_info(),
        findings=[finding(c, severity) for c in codes],
    )


class TestQualifying:
    def test_everything_together_is_a_call(self):
        call = evaluate(report())
        assert call.qualifies
        assert call.strength >= MIN_SIGNALS

    def test_the_reasons_are_named(self):
        call = evaluate(report())
        assert call.reasons
        assert all(isinstance(r, str) and r for r in call.reasons)

    def test_too_few_signals_is_not_a_call(self):
        call = evaluate(report(["round_trip_ok", "authorities_revoked", "lp_locked"]))
        assert call.strength < MIN_SIGNALS
        assert not call.qualifies

    def test_worth_calling_returns_none_below_the_bar(self):
        assert worth_calling(report(["lp_locked", "authorities_revoked"])) is None


class TestBlockers:
    @pytest.mark.parametrize("blocker", sorted(BLOCKERS))
    def test_a_single_blocker_kills_the_call(self, blocker):
        """Kein Gegengewicht hebt sie auf - auch nicht sechs gute Signale."""
        call = evaluate(report(PERFECT + [blocker]))
        assert not call.qualifies
        assert blocker in call.blocked_by

    def test_a_downtrend_blocks_despite_everything_else(self):
        call = evaluate(report(PERFECT + ["downtrend"]))
        assert call.strength >= MIN_SIGNALS  # die Signale sind da
        assert not call.qualifies            # und zaehlen trotzdem nicht

    def test_past_its_peak_blocks(self):
        """Der Fall, der die ganze Strukturanalyse ausgeloest hat: sauberer
        Token, dessen Lauf laengst stattgefunden hat."""
        assert not evaluate(report(PERFECT + ["past_its_peak"])).qualifies


class TestPreconditions:
    def test_a_heavy_finding_blocks(self):
        call = evaluate(report(PERFECT, severity=Severity.HIGH))
        assert not call.qualifies

    def test_a_knowledge_gap_blocks(self):
        """Unwissen ist kein Argument fuer einen Kauf."""
        call = evaluate(report(PERFECT + ["holders_unavailable"]))
        assert "grundbedingung" in call.blocked_by
        assert not call.qualifies

    def test_a_verdict_below_ok_blocks(self):
        call = evaluate(report(PERFECT + ["lp_status_unknown"]))
        assert not call.qualifies


class TestSignals:
    def test_uptrend_and_break_count_once(self):
        """Dasselbe Signal in zwei Formulierungen darf nicht doppelt zaehlen."""
        one = evaluate(report(PERFECT))
        both = evaluate(report(PERFECT + ["structure_break_up"]))
        assert one.strength == both.strength

    def test_few_buyers_drop_the_participation_signal(self):
        many = evaluate(report(buyers=60))
        few = evaluate(report(buyers=3))
        assert few.strength == many.strength - 1

    def test_participation_needs_organic_trading_too(self):
        """Viele Kaeufer allein reichen nicht - die koennen ein Skript sein."""
        codes = [c for c in PERFECT if c not in
                 ("trading_looks_organic", "trade_sizes_look_human")]
        assert "beteiligung" not in {s.name for s in evaluate(report(codes)).met}

    def test_presence_alone_is_never_enough(self):
        """Links kann jeder eintragen - das darf nie allein tragen."""
        call = evaluate(report(["has_public_presence"]))
        assert not call.qualifies


class TestSerialisation:
    def test_to_dict_carries_the_reasoning(self):
        data = evaluate(report()).to_dict()
        assert data["mint"] == MINT
        assert data["strength"] >= MIN_SIGNALS
        assert data["reasons"]

    def test_a_blocked_call_still_explains_itself(self):
        data = evaluate(report(PERFECT + ["downtrend"])).to_dict()
        assert "downtrend" in data["blocked_by"]


def test_an_empty_report_is_not_a_call():
    assert not evaluate(RiskReport(mint=MINT)).qualifies
