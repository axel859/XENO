"""Handelsmuster in den echten Transaktionen.

Die Pruefung in ``botting.py`` arbeitet mit zusammengefassten Marktdaten und
erkennt daran, ob wenige Wallets sehr viel handeln. Hier geht es eine Ebene
tiefer: auf die einzelnen Trades.

Das entscheidende Merkmal ist die **Gleichfoermigkeit der Betraege**. Ein
Programm kauft mit fest eingestelltem Einsatz - fuenfzig Mal exakt 0,05 SOL.
Menschen tun das nicht; sie kaufen 0,13, dann 2,40, dann 0,07.

Gemessen an echten Token: bei unauffaelligem Handel haben 2 bis 10 Prozent
der Trades exakt denselben Betrag. Bei einem Token, den die Marktdaten
bereits als maschinell auffaellig zeigten, waren es 53 Prozent.

**Zur Belastbarkeit:** die Grenzen stammen aus sechs ausgewerteten Token.
Das reicht, um die Groessenordnung zu treffen, nicht fuer Feinjustierung -
deshalb sind sie mit Abstand zum beobachteten Normalbereich gesetzt, und die
schwaecheren Stufen melden nur einen Hinweis.

Nicht umgesetzt: die Regelmaessigkeit der Zeitabstaende. Solana-Zeitstempel
haben Sekundenaufloesung, bei 400-Millisekunden-Bloecken landen zu viele
Trades im selben Zeitpunkt - daraus laesst sich nichts ablesen.
"""

from __future__ import annotations

from collections import Counter

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "tradepattern"

#: Unter so vielen Trades ist der Anteil gleicher Betraege reines Rauschen -
#: bei zehn Trades sind zwei gleiche schon 20 Prozent.
MIN_TRADES = 25

#: Auf so viele Nachkommastellen werden Betraege verglichen. Feiner waere
#: nutzlos, weil Gebuehren die letzten Stellen ohnehin verwackeln.
AMOUNT_PRECISION = 4

#: Stufen fuer den Anteil identischer Betraege. Beobachteter Normalbereich
#: liegt bei 2 bis 10 Prozent, ein maschineller Fall bei 53.
UNIFORMITY_LEVELS: list[tuple[float, Severity, str]] = [
    (40.0, Severity.HIGH, "Betraege sind fast alle identisch"),
    (25.0, Severity.MEDIUM, "auffaellig viele identische Betraege"),
    (15.0, Severity.LOW, "leicht erhoehte Gleichfoermigkeit der Betraege"),
]


def uniformity(trades) -> tuple[float, float, float]:
    """Wie gleichfoermig die Handelsgroessen sind.

    Rueckgabe: (Anteil des haeufigsten Betrags, Anteil der drei haeufigsten,
    haeufigster Betrag in SOL) - jeweils in Prozent.
    """
    if not trades:
        return 0.0, 0.0, 0.0
    counts = Counter(round(trade.sol, AMOUNT_PRECISION) for trade in trades)
    total = len(trades)
    amount, top = counts.most_common(1)[0]
    top3 = sum(count for _, count in counts.most_common(3))
    return 100.0 * top / total, 100.0 * top3 / total, amount


def check_trade_pattern(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    trades = data.trades
    if trades is None:
        # Nicht abgerufen - kein Befund, aber auch keine Entwarnung.
        return []

    if len(trades) < MIN_TRADES:
        return [
            finding(
                CHECK,
                "too_few_trades_for_pattern",
                Severity.INFO,
                f"Nur {len(trades)} Handelsvorgaenge - zu wenig fuer eine Musteraussage",
                trades=len(trades),
            )
        ]

    top_share, top3_share, common_amount = uniformity(trades)
    wallets = len({trade.wallet for trade in trades})
    per_wallet = len(trades) / max(wallets, 1)

    findings: list[Finding] = []

    for limit, severity, text in UNIFORMITY_LEVELS:
        if top_share >= limit:
            findings.append(
                finding(
                    CHECK,
                    "uniform_trade_sizes",
                    severity,
                    f"{text}: {top_share:.0f}% aller Trades sind exakt "
                    f"{common_amount:.4f} SOL",
                    identical_pct=round(top_share, 1),
                    top3_pct=round(top3_share, 1),
                    amount_sol=common_amount,
                    trades=len(trades),
                )
            )
            break

    # Aus den echten Transaktionen gerechnet, deshalb belastbarer als der
    # gleichnamige Wert aus den Marktdaten.
    if per_wallet >= 5.0:
        findings.append(
            finding(
                CHECK,
                "few_wallets_many_trades",
                Severity.MEDIUM if per_wallet >= 8 else Severity.LOW,
                f"{len(trades)} Handelsvorgaenge von nur {wallets} Wallets "
                f"({per_wallet:.1f} je Wallet, aus den Transaktionen gerechnet)",
                trades=len(trades),
                wallets=wallets,
                per_wallet=round(per_wallet, 1),
            )
        )

    if not findings:
        findings.append(
            finding(
                CHECK,
                "trade_sizes_look_human",
                Severity.INFO,
                f"Handelsgroessen breit gestreut ({top_share:.0f}% identisch bei "
                f"{len(trades)} Trades von {wallets} Wallets)",
                identical_pct=round(top_share, 1),
                wallets=wallets,
            )
        )

    return findings
