"""Tests der Herkunftsanalyse.

Anders als die uebrige Bundle-Erkennung arbeitet diese Schicht nicht mit
Aehnlichkeiten, sondern mit einem Nachweis: auf Solana kann keine Wallet
handeln, bevor ihr jemand SOL geschickt hat. Wer diese erste Einzahlung
geschickt hat, steht in der Blockchain.

Die beiden Faelle, in denen dieser Nachweis nichts wert waere, werden hier
ausdruecklich geprueft: eine geteilte Boerse verbindet niemanden, und die
erste Einzahlung einer jahrealten Wallet sagt ueber diesen Token nichts.
"""

from __future__ import annotations

from conftest import MINT, make_candidate

from xeno.checks.base import TokenData
from xeno.checks.funding import CLUSTER_MIN, check_funding, clusters
from xeno.config import RiskThresholds
from xeno.models import Severity
from xeno.sources.helius import (
    MIN_FUNDING_LAMPORTS,
    WALLET_PAGE,
    Origin,
    first_funder,
)

RISK = RiskThresholds()

#: Eine bekannte Boersenadresse aus known.py - Coinbase.
EXCHANGE = "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS"


def origins(count: int, funder: str = "geldgeber", txs: int = 5) -> list[Origin]:
    return [
        Origin(wallet=f"w{i}", funder=funder, funded_at=1700, tx_count=txs)
        for i in range(count)
    ]


def run(entries) -> list:
    data = TokenData(mint=MINT, candidate=make_candidate(), origins=entries)
    return check_funding(data, RISK)


def codes(findings) -> set[str]:
    return {f.code for f in findings}


class TestFirstFunder:
    def _tx(self, ts: int, transfers: list[tuple[str, str, int]]) -> dict:
        return {
            "timestamp": ts,
            "nativeTransfers": [
                {"fromUserAccount": f, "toUserAccount": t, "amount": a}
                for f, t, a in transfers
            ],
        }

    def test_finds_the_earliest_incoming_transfer(self):
        txs = [
            self._tx(300, [("spaeter", "meine", 5_000_000)]),
            self._tx(100, [("zuerst", "meine", 5_000_000)]),
            self._tx(200, [("dazwischen", "meine", 5_000_000)]),
        ]
        assert first_funder(txs, "meine") == ("zuerst", 100)

    def test_outgoing_transfers_are_ignored(self):
        txs = [self._tx(100, [("meine", "jemand", 9_000_000)])]
        assert first_funder(txs, "meine") == ("", 0)

    def test_dust_is_not_funding(self):
        """Winzige Betraege an fremde Wallets sind eine bekannte Masche -
        wer so angeschrieben wird, ist damit nicht finanziert."""
        txs = [self._tx(100, [("werber", "meine", MIN_FUNDING_LAMPORTS - 1)])]
        assert first_funder(txs, "meine") == ("", 0)

    def test_self_transfer_is_not_funding(self):
        txs = [self._tx(100, [("meine", "meine", 9_000_000)])]
        assert first_funder(txs, "meine") == ("", 0)

    def test_no_transactions(self):
        assert first_funder([], "meine") == ("", 0)


class TestClusters:
    def test_groups_by_funder(self):
        entries = origins(3, "A") + origins(2, "B")
        entries[3].wallet, entries[4].wallet = "x", "y"
        grouped = clusters(entries)
        assert len(grouped["A"]) == 3
        assert len(grouped["B"]) == 2

    def test_established_wallets_are_left_out(self):
        entries = origins(4, "A")
        entries[0].established = True
        assert len(clusters(entries)["A"]) == 3

    def test_a_shared_exchange_links_nobody(self):
        """Boersen zahlen an tausende Wallets aus. Wer ueber dieselbe Boerse
        einsteigt, teilt den Absender, ohne sich zu kennen."""
        assert clusters(origins(5, EXCHANGE)) == {}

    def test_unknown_origin_is_not_a_group(self):
        assert clusters([Origin(wallet="w1", funder="")]) == {}


class TestCheck:
    def test_not_fetched_yields_nothing(self):
        assert check_funding(TokenData(mint=MINT), RISK) == []

    def test_too_few_wallets_is_not_an_all_clear(self):
        result = run(origins(2, "A"))
        assert "too_few_wallets_examined" in codes(result)
        assert "funding_looks_independent" not in codes(result)

    def test_shared_source_is_reported(self):
        entries = origins(CLUSTER_MIN, "A") + origins(3, "B")
        for i, entry in enumerate(entries):
            entry.wallet = f"wallet{i}"
        result = run(entries)
        assert "shared_funding_source" in codes(result)

    def test_a_large_cluster_weighs_more(self):
        entries = origins(6, "A")
        for i, entry in enumerate(entries):
            entry.wallet = f"wallet{i}"
        result = run(entries)
        found = next(f for f in result if f.code == "shared_funding_source")
        assert found.severity is Severity.HIGH

    def test_independent_wallets_pass(self):
        entries = [
            Origin(wallet=f"w{i}", funder=f"quelle{i}", tx_count=20) for i in range(6)
        ]
        result = run(entries)
        assert "funding_looks_independent" in codes(result)
        assert all(f.severity is Severity.INFO for f in result)

    def test_established_wallets_do_not_trigger_anything(self):
        entries = [
            Origin(wallet=f"w{i}", established=True, tx_count=WALLET_PAGE)
            for i in range(8)
        ]
        result = run(entries)
        assert "shared_funding_source" not in codes(result)
        assert "many_fresh_wallets" not in codes(result)

    def test_fresh_wallets_are_counted(self):
        entries = [
            Origin(wallet=f"w{i}", funder=f"quelle{i}", tx_count=1) for i in range(5)
        ]
        result = run(entries)
        assert "many_fresh_wallets" in codes(result)
