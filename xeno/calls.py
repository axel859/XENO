"""Calls - wann XENO von sich aus etwas vorschlaegt.

Alle uebrigen Pruefungen beantworten eine Frage: **ist das eine Falle?** Ein
Call ist etwas anderes. Er behauptet, dass gerade etwas losgeht - und das
ist eine Aussage ueber die Zukunft, keine Feststellung ueber den Ist-Zustand.

Deshalb wird er nicht aus einem Punktestand abgeleitet. Ein hoher Score
heisst nur "nichts Schlimmes gefunden", und genau das war der Grund, warum
saubere Token ohne jede Bewegung dasselbe gute Urteil bekamen wie welche,
die tatsaechlich liefen.

**Konfluenz statt Punktzahl.** Die Regel stammt aus der Chartlehre und ist
die einzige, die hier passt:

    ein Signal    -> moegliche Reaktion
    zwei Signale  -> handelbare Lage
    drei und mehr -> Ansage

Gezaehlt werden dabei nur Signale, die **voneinander unabhaengig** sind. Ein
Aufwaertstrend und ein Bruch nach oben sind dasselbe Signal in zwei
Formulierungen; sie zaehlen einmal.

**Ohne Bilanz kein Call.** Jeder Vorschlag wird mitgemessen, und die
Trefferquote steht daneben. Ein Call ohne diese Zahl ist eine Vermutung mit
selbstbewusstem Etikett - und daran ist hier schon einmal jemand
haengengeblieben.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import RiskReport, Severity, Verdict

#: So viele unabhaengige Signale muessen zusammenkommen.
MIN_SIGNALS = 3

#: Befunde, die einen Call ausschliessen - unabhaengig davon, wie viele
#: Signale sonst zusammenkommen. Kein Gegengewicht hebt sie auf.
BLOCKERS = frozenset(
    {
        "downtrend",
        "structure_break_down",
        "past_its_peak",
        "uniform_trade_sizes",
        "machine_trading",
        "wash_trading",
        "single_wallet_trading",
        "shared_funding_source",
        "insider_network",
        "insider_supply",
        "creator_rug_history",
        "liquidity_dust",
        "no_sell_route",
        "supply_barely_trades",
    }
)


@dataclass
class Signal:
    """Ein einzelnes positives Merkmal."""

    name: str
    met: bool
    detail: str = ""


@dataclass
class Call:
    """Ein Vorschlag mitsamt seiner Begruendung."""

    mint: str
    symbol: str = ""
    price_usd: float | None = None
    mcap_usd: float | None = None
    signals: list[Signal] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def met(self) -> list[Signal]:
        return [s for s in self.signals if s.met]

    @property
    def strength(self) -> int:
        return len(self.met)

    @property
    def qualifies(self) -> bool:
        return not self.blocked_by and self.strength >= MIN_SIGNALS

    @property
    def reasons(self) -> list[str]:
        return [s.detail or s.name for s in self.met]

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "price_usd": self.price_usd,
            "mcap_usd": self.mcap_usd,
            "strength": self.strength,
            "signals": [s.name for s in self.met],
            "reasons": self.reasons,
            "blocked_by": list(self.blocked_by),
        }


def _codes(report: RiskReport) -> set[str]:
    return {f.code for f in report.findings}


def evaluate(report: RiskReport) -> Call:
    """Prueft einen fertigen Bericht auf die Call-Bedingungen.

    Gibt immer einen ``Call`` zurueck - ob er zaehlt, sagt ``qualifies``.
    Das ist Absicht: auch ein knapp verfehlter Vorschlag ist eine
    Information, und die Begruendung soll nachvollziehbar bleiben.
    """
    codes = _codes(report)
    candidate = report.candidate
    structure = report.structure

    call = Call(
        mint=report.mint,
        symbol=report.symbol,
        price_usd=candidate.price_usd if candidate else None,
        mcap_usd=candidate.mcap_usd if candidate else None,
        blocked_by=sorted(codes & BLOCKERS),
    )

    # Grundbedingung: nichts Schweres gefunden, und keine offenen Fragen.
    # Ein Token, ueber den etwas Wesentliches unbekannt ist, wird nicht
    # vorgeschlagen - Unwissen ist kein Argument fuer einen Kauf.
    heavy = any(
        f.severity in (Severity.CRITICAL, Severity.HIGH) for f in report.findings
    )
    if heavy or report.data_gaps or report.verdict is not Verdict.OK:
        call.blocked_by = sorted(set(call.blocked_by) | {"grundbedingung"})

    # -- Die einzelnen Signale ------------------------------------------

    # Richtung. Aufwaertstrend und Bruch nach oben sind dasselbe Signal in
    # zwei Formulierungen und zaehlen deshalb einmal.
    rising = "uptrend" in codes or "structure_break_up" in codes
    trend_detail = ""
    if structure is not None and structure.change_pct is not None:
        trend_detail = f"Kurs steigt ({structure.change_pct:+.0f}% im Verlauf)"
    call.signals.append(
        Signal("aufwaerts", rising, trend_detail or "Aufwaertsstruktur")
    )

    # Beteiligung: viele verschiedene Kaeufer, und der Handel sieht
    # menschlich aus. Das eine ohne das andere sagt wenig.
    buyers = (candidate.buyers_h1 or 0) if candidate else 0
    organic = "trading_looks_organic" in codes or "trade_sizes_look_human" in codes
    call.signals.append(
        Signal(
            "beteiligung",
            buyers >= 25 and organic,
            f"{buyers} verschiedene Kaeufer, Handel wirkt echt",
        )
    )

    # Herkunft der Gelder nachweislich unabhaengig - das staerkste Signal,
    # weil es als einziges auf einem Nachweis beruht statt auf Indizien.
    call.signals.append(
        Signal(
            "herkunft",
            "funding_looks_independent" in codes,
            "groesste Halter unabhaengig finanziert",
        )
    )

    # Handelbarkeit ohne Auffaelligkeit beim Rueckverkauf.
    call.signals.append(
        Signal(
            "handelbar",
            "round_trip_ok" in codes,
            "Kauf und Rueckverkauf ohne Auffaelligkeit",
        )
    )

    # Verteilung: keine Wallet, die den Kurs allein kippen kann.
    call.signals.append(
        Signal(
            "verteilung",
            "distribution_ok" in codes and "no_bundling_signal" in codes,
            "Supply breit verteilt, kein Buendel erkennbar",
        )
    )

    # Aussenauftritt. Bewusst das schwaechste Signal - Links kann jeder
    # eintragen. Es zaehlt nur als Ergaenzung, nie allein.
    call.signals.append(
        Signal(
            "auftritt",
            "has_public_presence" in codes,
            "Website oder Social-Links hinterlegt",
        )
    )

    return call


def worth_calling(report: RiskReport) -> Call | None:
    """Bequemer Zugang: der Call, falls er die Bedingungen erfuellt."""
    call = evaluate(report)
    return call if call.qualifies else None
