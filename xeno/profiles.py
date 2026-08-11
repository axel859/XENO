"""Suchprofile.

Ein einzelner Satz Schwellwerte kann nicht beides: fruehe Token finden *und*
nur sicher handelbare zeigen. Wer bei einem vier Minuten alten Pool 5.000 $
Liquiditaet und 25 Kaeufer verlangt, bekommt ausschliesslich Token, deren
Bewegung schon gelaufen ist.

Die Werte hier sind nicht geschaetzt, sondern aus 200 tatsaechlich frisch
erstellten Pools abgeleitet. Deren Wirklichkeit sah so aus:

    Alter        Median   3 Minuten
    Liquiditaet  Median   1.746 $     (25%-Quantil 1.665 $)
    Volumen 1h   Median     793 $     (25%-Quantil   176 $)
    Kaeufer 1h   Median       4       (75%-Quantil    18)

Gegen die urspruenglichen Vorgaben kam davon **einer von hundert** durch.
Nicht weil die Token schlecht waren, sondern weil die Messlatte fuer ihr
Alter unerreichbar war.

Entscheidend ist deshalb der Wechsel des Massstabs: bei einem vier Minuten
alten Token sagt die absolute Groesse nichts, wohl aber die *Beteiligung*.
Achtzig Kaeufer in vier Minuten sind ein Signal - 14.000 $ Liquiditaet sind
es nicht, die kann eine einzelne Wallet stellen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import ScreenThresholds


@dataclass(frozen=True)
class Profile:
    """Eine vollstaendige Suchstrategie."""

    name: str
    summary: str
    screen: ScreenThresholds
    #: Seiten je Discovery-Lauf bei einem einmaligen Scan. Eine Seite sind
    #: 20 Pools.
    pages: int
    include_new: bool
    include_trending: bool
    #: Seiten im Dauerbetrieb. Deutlich weniger als bei einem einmaligen
    #: Scan, und zwar aus zwei Gruenden: die kostenlose GeckoTerminal-API
    #: sperrt bei zu vielen Anfragen (nachgemessen: 10 Seiten im Minutentakt
    #: fuehren ab dem dritten Durchlauf zu HTTP 429), und tiefe Seiten sind
    #: im Dauerbetrieb ohnehin unnoetig - dort stehen Pools, die beim
    #: vorherigen Durchlauf schon auf Seite 1 waren.
    watch_pages: int = 3
    #: Nach welchem Merkmal das knappe Pruefbudget verteilt wird.
    #: "traction" = Kaeufer und Kaufdruck, "liquidity" = Groesse.
    rank_by: str = "liquidity"


EARLY = Profile(
    name="early",
    summary="Frische Pools mit erster echter Beteiligung - vor dem Anstieg",
    pages=10,
    watch_pages=3,
    # Trending ist per Definition zu spaet: was dort auftaucht, laeuft bereits.
    include_new=True,
    include_trending=False,
    rank_by="traction",
    screen=ScreenThresholds(
        # Unter zwei Minuten liegen noch nicht genug Daten vor, um irgendetwas
        # zu beurteilen - der Pool existiert, mehr weiss man nicht.
        min_age_minutes=2.0,
        # Sechs Stunden statt drei Tage: danach ist "frueh" vorbei.
        max_age_hours=6.0,
        # Knapp unter dem 25%-Quantil frischer Pools.
        min_liquidity_usd=1_200.0,
        max_liquidity_usd=500_000.0,
        min_volume_h1_usd=500.0,
        # Doppelter Median - waehlt die Token mit ueberdurchschnittlichem
        # Zulauf aus, ohne die Latte unerreichbar zu legen.
        min_unique_buyers_h1=8,
        # In dieser Phase sind Verkaeufe fast immer Erstkaeufer, die sofort
        # wieder aussteigen. Klarer Kaufueberhang ist das eigentliche Signal.
        min_buy_sell_ratio=1.5,
        max_volume_to_liquidity=80.0,
    ),
)

BALANCED = Profile(
    name="balanced",
    summary="Bereits handelbare Token mit Substanz",
    pages=3,
    watch_pages=2,
    include_new=True,
    include_trending=True,
    rank_by="liquidity",
    screen=ScreenThresholds(),
)

ESTABLISHED = Profile(
    name="established",
    summary="Groessere, laufende Token - spaet, aber belastbar bewertbar",
    pages=2,
    watch_pages=2,
    include_new=False,
    include_trending=True,
    rank_by="liquidity",
    screen=ScreenThresholds(
        min_age_minutes=60.0,
        max_age_hours=24 * 30,
        min_liquidity_usd=50_000.0,
        max_liquidity_usd=50_000_000.0,
        min_volume_h1_usd=25_000.0,
        min_unique_buyers_h1=50,
        min_buy_sell_ratio=0.8,
        max_volume_to_liquidity=40.0,
    ),
)

PROFILES: dict[str, Profile] = {p.name: p for p in (EARLY, BALANCED, ESTABLISHED)}

DEFAULT_PROFILE = "early"


def get_profile(name: str | None = None) -> Profile:
    """Liefert ein Profil nach Namen, sonst das aus der Umgebung, sonst früh."""
    key = (name or os.environ.get("XENO_PROFILE") or DEFAULT_PROFILE).strip().lower()
    return PROFILES.get(key, PROFILES[DEFAULT_PROFILE])


def profile_names() -> list[str]:
    return list(PROFILES)
