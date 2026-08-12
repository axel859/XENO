"""Herkunft der Gelder der groessten Halter.

Die Bundle-Erkennung in ``bundling.py`` arbeitet mit Aehnlichkeiten: gleich
grosse Bestaende, benachbarte Konten, auffaellige Muster. Das sind Indizien.

Hier geht es um einen Nachweis. Auf Solana kann eine Wallet ohne SOL nichts
tun - nicht einmal die Gebuehr fuer den ersten Kauf bezahlen. Jede Wallet hat
deshalb einen Moment, in dem ihr jemand das erste Geld geschickt hat. Wenn
fuenf der groessten Halter dieses Geld von **derselben** Adresse bekamen,
kurz bevor der Token startete, dann sind das nicht fuenf Kaeufer. Das ist
einer.

Das ist keine Heuristik mehr, sondern steht so in der Blockchain.

**Zwei Einschraenkungen, die der Befund selbst nennt:**

Eine gemeinsame Herkunft kann harmlos sein. Boersen zahlen an tausende
Wallets aus; wer ueber dieselbe Boerse einsteigt, teilt den Absender, ohne
sich zu kennen. Bekannte Boersen- und Programmadressen werden deshalb nicht
gezaehlt.

Und Wallets mit langer Historie werden gar nicht erst untersucht. Wer
hunderte Transaktionen hat, wurde nicht fuer diesen Token angelegt - dessen
erste Einzahlung liegt Monate zurueck und sagt ueber diesen Token nichts.
"""

from __future__ import annotations

from collections import defaultdict

from ..config import RiskThresholds
from ..known import classify_owner
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "funding"

#: Ab so vielen Wallets mit demselben Geldgeber gilt es als Buendel. Der
#: Wert folgt der gaengigen Faustregel "mehr als drei bis fuenf ist schlecht"
#: und ist am unteren Ende angesetzt, weil hier nicht geraten wird.
CLUSTER_MIN = 3
CLUSTER_HIGH = 5

#: So viele Wallets muessen ueberhaupt auswertbar sein, damit ein Anteil
#: etwas bedeutet. Bei zwei untersuchten Wallets ist "beide vom selben
#: Absender" ein Zufall, kein Muster.
MIN_EXAMINED = 4


def clusters(origins) -> dict[str, list[str]]:
    """Gruppiert Wallets nach ihrem Geldgeber.

    Ausgelassen werden Wallets ohne erkennbare Herkunft, etablierte Wallets
    und alle Geldgeber, die als Boerse oder Programm bekannt sind.
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in origins:
        if entry.established or not entry.known:
            continue
        # Boersen und Programme sind keine Personen - eine geteilte Boerse
        # verbindet zwei Wallets nicht.
        if classify_owner(entry.funder):
            continue
        grouped[entry.funder].append(entry.wallet)
    return dict(grouped)


def check_funding(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    origins = data.origins
    if origins is None:
        # Nicht abgerufen - kein Befund, aber auch keine Entwarnung.
        return []

    examined = [entry for entry in origins if not entry.established]
    established = len(origins) - len(examined)

    if len(origins) < MIN_EXAMINED:
        return [
            finding(
                CHECK,
                "too_few_wallets_examined",
                Severity.INFO,
                f"Nur {len(origins)} Wallets untersucht - zu wenig fuer eine "
                "Aussage zur Herkunft",
                examined=len(origins),
            )
        ]

    findings: list[Finding] = []
    grouped = clusters(origins)
    biggest = max(grouped.items(), key=lambda kv: len(kv[1]), default=("", []))
    funder, members = biggest

    if len(members) >= CLUSTER_MIN:
        severity = Severity.HIGH if len(members) >= CLUSTER_HIGH else Severity.MEDIUM
        findings.append(
            finding(
                CHECK,
                "shared_funding_source",
                severity,
                f"{len(members)} der groessten Halter wurden von derselben "
                f"Adresse finanziert ({funder[:8]}...) - das ist ein Halter, "
                "nicht mehrere",
                funder=funder,
                wallets=len(members),
                examined=len(origins),
            )
        )

    # Frische Wallets: keine Historie ausser diesem einen Token. Einzeln
    # unauffaellig, gehaeuft das klassische Bild eines Starts aus einer Hand.
    fresh = [entry for entry in examined if entry.tx_count <= 3]
    if len(fresh) >= CLUSTER_MIN:
        findings.append(
            finding(
                CHECK,
                "many_fresh_wallets",
                Severity.MEDIUM if len(fresh) >= CLUSTER_HIGH else Severity.LOW,
                f"{len(fresh)} der groessten Halter sind brandneue Wallets "
                "ohne jede sonstige Historie",
                fresh=len(fresh),
                examined=len(origins),
            )
        )

    if not findings:
        findings.append(
            finding(
                CHECK,
                "funding_looks_independent",
                Severity.INFO,
                f"Groesste Halter unabhaengig finanziert ({len(origins)} geprueft, "
                f"davon {established} mit eigener Handelshistorie)",
                examined=len(origins),
                established=established,
            )
        )

    return findings
