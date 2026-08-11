"""Bundling und Insider-Cluster.

"Gebundelt" heisst: der Ersteller hat beim Launch mit vielen eigenen Wallets
gleichzeitig gekauft und haelt dadurch verdeckt einen Grossteil der Supply.
Auf den ersten Blick sieht die Verteilung breit aus - tatsaechlich gehoert
sie einer Person, die jederzeit geschlossen verkaufen kann.

Zwei Wege, das zu erkennen:

1. **Insider-Markierungen von RugCheck.** Deren Graph-Analyse verfolgt, aus
   welchen Quellen die Wallets ihr SOL bekommen haben. Das ist die
   belastbarere Variante.
2. **Cluster-Heuristik auf den Betraegen.** Bundler kaufen typischerweise mit
   gleichen oder sehr aehnlichen Betraegen pro Wallet. Mehrere private
   Wallets mit nahezu identischem Anteil sind deshalb verdaechtig.

Punkt 2 ist ausdruecklich eine Heuristik, kein Beweis: gleich grosse
Bestaende koennen auch zufaellig entstehen, und wer die Betraege streut,
faellt hier nicht auf. Die Befunde sind entsprechend gewichtet.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Holder, Severity
from .base import TokenData, finding

CHECK = "bundling"

#: Relative Abweichung, bis zu der zwei Bestaende als "gleich gross" gelten.
CLUSTER_TOLERANCE = 0.06
#: Ab so vielen aehnlich grossen Wallets wird von einem Cluster gesprochen.
MIN_CLUSTER_SIZE = 3
#: Sehr kleine Bestaende sind Rauschen und werden nicht gruppiert.
MIN_CLUSTER_PCT = 0.4


def find_balance_clusters(holders: list[Holder]) -> list[list[Holder]]:
    """Gruppiert private Wallets mit nahezu identischem Supply-Anteil.

    Arbeitet auf der absteigend sortierten Liste und fasst benachbarte
    Eintraege zusammen, solange sie innerhalb der Toleranz zum Startwert der
    Gruppe liegen.
    """
    candidates = sorted(
        (h for h in holders if not h.is_excluded and h.pct >= MIN_CLUSTER_PCT),
        key=lambda h: h.pct,
        reverse=True,
    )

    clusters: list[list[Holder]] = []
    current: list[Holder] = []
    for holder in candidates:
        if current and abs(current[0].pct - holder.pct) <= current[0].pct * CLUSTER_TOLERANCE:
            current.append(holder)
            continue
        if len(current) >= MIN_CLUSTER_SIZE:
            clusters.append(current)
        current = [holder]
    if len(current) >= MIN_CLUSTER_SIZE:
        clusters.append(current)
    return clusters


def check_bundling(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    findings: list[Finding] = []
    rc = data.rc

    # 1. Insider-Netzwerke aus der Graph-Analyse
    networks = rc.insider_networks
    if networks:
        total_wallets = sum(int(n.get("size") or 0) for n in networks)
        findings.append(
            finding(
                CHECK,
                "insider_network",
                Severity.HIGH if total_wallets >= 10 else Severity.MEDIUM,
                f"{len(networks)} verbundene Wallet-Netzwerke erkannt "
                f"({total_wallets} Wallets)",
                networks=len(networks),
                wallets=total_wallets,
            )
        )

    insider_pct = rc.insider_pct
    if insider_pct > 0:
        severity = Severity.CRITICAL if insider_pct >= 40 else (
            Severity.HIGH if insider_pct >= thresholds.max_bundle_pct else Severity.MEDIUM
        )
        findings.append(
            finding(
                CHECK,
                "insider_supply",
                severity,
                f"Als Insider markierte Wallets halten {insider_pct:.1f}% der Supply",
                pct=round(insider_pct, 2),
            )
        )

    # 2. Cluster-Heuristik auf den Bestandsgroessen
    if data.distribution and data.distribution.holders:
        clusters = find_balance_clusters(data.distribution.holders)
        if clusters:
            biggest = max(clusters, key=lambda c: sum(h.pct for h in c))
            cluster_pct = sum(h.pct for h in biggest)
            severity = (
                Severity.HIGH
                if cluster_pct >= thresholds.max_bundle_pct
                else Severity.MEDIUM
                if cluster_pct >= 8
                else Severity.LOW
            )
            findings.append(
                finding(
                    CHECK,
                    "balance_cluster",
                    severity,
                    f"{len(biggest)} Wallets mit nahezu gleichem Bestand halten zusammen "
                    f"{cluster_pct:.1f}% - moegliches Bundling",
                    wallets=len(biggest),
                    pct=round(cluster_pct, 2),
                    heuristic=True,
                )
            )

    if not findings:
        # Aussagekraeftig nur, wenn ueberhaupt Holder-Daten vorlagen.
        if data.distribution and data.distribution.holders:
            findings.append(
                finding(
                    CHECK,
                    "no_bundling_signal",
                    Severity.INFO,
                    "Keine Hinweise auf Bundling in den geprueften Wallets",
                )
            )
        else:
            findings.append(
                finding(
                    CHECK,
                    "bundling_unchecked",
                    Severity.MEDIUM,
                    "Bundling konnte mangels Holder-Daten nicht geprueft werden",
                )
            )
    return findings
