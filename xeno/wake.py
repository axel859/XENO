"""Aufwach-Erkennung - die zweite Chance fuer abgehakte Token.

Der Anlass ist ein Fall, den XENO bisher nicht sehen konnte: ein Coin wird
geprueft, es passiert nichts, er bekommt ein mittelmaessiges Urteil - und
zwei Tage spaeter schreibt jemand mit Reichweite darueber, und er laeuft.
Ab dem Moment war XENO blind. Nachverfolgt wurden Kurse nur bis 24 Stunden
nach dem ersten Check, erneut geprueft wurde ab Tag zwei nur alle vier
Stunden, und Token mit kritischem Befund gar nicht mehr.

**Vorhergesagt wird hier nichts.** Der Ausloeser - ein Tweet, ein Listing,
eine Gruppe - ist nicht vorhersagbar, auch nicht mit einem Twitter-Tracker:
der sieht den Tweet ebenfalls erst, wenn er da ist. Gemessen wird deshalb
nicht der Ausloeser, sondern der Einschlag. Das hat drei Vorteile: es ist
eindeutig (ein Tweet ueber "doge" passt auf vierhundert Token, ein
Volumensprung auf genau einen Mint), es kostet keine RPC-Credits, und es
findet **jeden** Ausloeser statt nur den einen, auf den man horcht.

Der Preis dafuer ist Ehrlichkeit wert: wer eine direkte Leitung zur Kette
hat, ist frueher drin. Aber ein echter Ausloeser ist nicht nach dreissig
Sekunden vorbei - er baut ueber Stunden Struktur auf, und genau die misst
``structure.py`` bereits.

Das Mass
--------

Verglichen wird ein Token **mit sich selbst**, nicht mit anderen::

    burst = Volumen der letzten Stunde / (Tagesvolumen / 24)

Ein Token im Normalzustand liegt bei ungefaehr 1. Eine feste Schwelle in
Dollar waere hier falsch: sie fande immer nur, was ohnehin am groessten
ist, nie den kleinen Coin, bei dem gerade etwas anfaengt.

Beide Zahlen stehen in derselben DexScreener-Antwort, die der Bot fuer die
Kursnachverfolgung ohnehin holt. Es braucht also weder eine eigene
Historie noch eine einzige zusaetzliche Anfrage.

Die Schwellen stammen aus einer Messung an 53 aktiv gehandelten Token
(13.08.2026, GeckoTerminal-Trending, alle aelter als ein Tag):

===========  =====
Median        0.61
75%           1.16
90%           1.98
95%           4.00
98%           6.43
Maximum       6.5
===========  =====

``MIN_BURST = 6.0`` liegt damit oberhalb von 98% dessen, was selbst unter
bereits *laufenden* Token normal ist. Bei ruhenden Token - und nur um die
geht es hier - ist es entsprechend seltener.

Zwei Fallstricke, die beim Messen aufgefallen sind
--------------------------------------------------

**Unter 24 Stunden ist der Faktor ein Rechenartefakt.** Bei einem frisch
gestarteten Token ist das Stundenvolumen zwangslaeufig gleich dem
Tagesvolumen, der Faktor also exakt 24 - immer, bei jedem. In der ersten
Messung lagen deshalb saemtliche Neuzugaenge auf dem Maximalwert. Ein
Wiederaufwachen setzt ein vorheriges Leben voraus, darum ``MIN_AGE``.

**Ohne Volumenuntergrenze ist der Faktor Rauschen.** In den Daten stand
ein Token mit 421 USD Tagesumsatz und "+499465% in einer Stunde" - ein
einziger Kauf in einen toten Pool. Verhaeltniszahlen auf winzigen Absolut-
werten bedeuten nichts, deshalb dieselbe Untergrenze, die auch der
Vorfilter benutzt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import TokenCandidate
from .sources import DexScreener
from .watchstate import TokenState, WatchState

#: Stundenvolumen im Verhaeltnis zum eigenen Tagesschnitt. Siehe Messung oben.
MIN_BURST = 6.0

#: Der Kurs muss mitgehen. Volumen bei fallendem Kurs ist ein Ausverkauf,
#: kein Aufwachen - da verlaesst gerade jemand die Position.
MIN_PRICE_CHANGE_PCT = 10.0

#: Untergrenze in Dollar, damit das Verhaeltnis ueberhaupt etwas bedeutet.
#: Bewusst derselbe Wert wie ``ScreenThresholds.min_volume_h1_usd`` - was
#: fuer einen neuen Token zu wenig Handel ist, ist es fuer einen alten auch.
MIN_VOLUME_H1_USD = 2_000.0

#: Vorher ist der Faktor ein Rechenartefakt, kein Signal.
MIN_AGE_MINUTES = 24 * 60.0

#: Sperrfrist je Token. Ein Lauf ueber mehrere Stunden ist ein Ereignis,
#: nicht zwanzig - ohne die Frist meldete jeder Durchlauf denselben Token
#: erneut, solange die Bewegung anhaelt.
COOLDOWN_SECONDS = 6 * 3600.0

#: Abstand zwischen zwei Herzschlaegen. Der Abruf ist kostenlos, aber nicht
#: gratis: er belegt das Anfragekontingent von DexScreener, das sich der
#: Puls mit Kursnachverfolgung und Papierhandel teilt. Fuenf Minuten reichen
#: fuer ein Ereignis, das sich ueber Stunden entwickelt.
PULSE_INTERVAL = 300.0

#: Wie viele Aufwacher je Durchlauf tief geprueft werden. Die Erkennung
#: selbst kostet nichts, die Pruefung danach schon - und bei einem Ereignis,
#: das den ganzen Markt bewegt, wachen viele Token gleichzeitig auf.
MAX_PER_CYCLE = 3

#: Obergrenze des Faktors. Mehr als das gesamte Tagesvolumen in einer Stunde
#: ist rechnerisch nicht moeglich; groessere Werte waeren Datenfehler.
MAX_BURST = 24.0

#: Ab hier ist die Prozentzahl keine Aussage mehr, sondern ein Symptom.
#: Gemessen wurde ein Token mit 365.000 USD Stundenumsatz und "+497937%":
#: der Pool lag so lange still, dass der Vergleichskurs praktisch null war.
#: Das Ereignis ist echt - die Zahl in der Meldung waere nur Rauschen.
IMPLAUSIBLE_CHANGE_PCT = 1_000.0


@dataclass(frozen=True)
class Wake:
    """Ein Token, der nach laengerer Ruhe wieder gehandelt wird."""

    mint: str
    symbol: str
    burst: float
    volume_h1_usd: float
    price_change_h1_pct: float
    #: Urteil, mit dem XENO ihn damals abgelegt hat.
    previous_verdict: str = ""
    #: Wie lange XENO ihn schon kennt.
    known_days: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "burst": round(self.burst, 1),
            "volume_h1_usd": self.volume_h1_usd,
            "price_change_h1_pct": self.price_change_h1_pct,
            "previous_verdict": self.previous_verdict,
            "known_days": round(self.known_days, 1),
            "reasons": list(self.reasons),
        }


def burst_factor(volume_h1: float | None, volume_h24: float | None) -> float | None:
    """Stundenvolumen im Verhaeltnis zum eigenen Tagesschnitt.

    ``None`` heisst "keine Aussage moeglich" - nicht "unauffaellig". Ohne
    Tagesvolumen gibt es keinen Bezugspunkt, und ein fehlender Bezugspunkt
    darf nie als Entwarnung durchgehen.
    """
    if volume_h1 is None or volume_h24 is None:
        return None
    if volume_h24 <= 0 or volume_h1 < 0:
        return None
    factor = volume_h1 / (volume_h24 / 24.0)
    return min(factor, MAX_BURST)


def detect(
    candidate: TokenCandidate,
    state: TokenState | None,
    now: float | None = None,
) -> Wake | None:
    """Prueft einen einzelnen Token auf Wiederaufnahme des Handels.

    Alle Bedingungen muessen zutreffen. Sie sind bewusst als Konjunktion
    gebaut und nicht als Punktzahl: jede einzelne schliesst einen Fall aus,
    in dem die Zahl zwar hoch, die Aussage aber leer waere.
    """
    if state is None:
        return None
    now = now or time.time()

    age = candidate.age_minutes
    if age is None or age < MIN_AGE_MINUTES:
        return None

    volume = candidate.volume_h1_usd
    if volume is None or volume < MIN_VOLUME_H1_USD:
        return None

    burst = burst_factor(volume, candidate.volume_h24_usd)
    if burst is None or burst < MIN_BURST:
        return None

    change = candidate.price_change_h1_pct
    if change is None or change < MIN_PRICE_CHANGE_PCT:
        return None

    if state.woke_at and now - state.woke_at < COOLDOWN_SECONDS:
        return None

    known_days = (now - state.first_seen) / 86400.0 if state.first_seen else 0.0
    reasons = [
        f"Volumen {burst:.0f}x ueber dem eigenen Tagesschnitt",
        (
            "Kurs aus dem Stand hoch - der Pool lag vorher praktisch still"
            if change >= IMPLAUSIBLE_CHANGE_PCT
            else f"Kurs +{change:.0f}% in einer Stunde"
        ),
        f"${volume:,.0f} Handel in der letzten Stunde",
    ]
    if state.verdict:
        reasons.append(f"lag seit {known_days:.1f} Tagen als {state.verdict} ab")

    return Wake(
        mint=candidate.mint,
        symbol=candidate.symbol or state.symbol,
        burst=burst,
        volume_h1_usd=volume,
        price_change_h1_pct=change,
        previous_verdict=state.verdict,
        known_days=known_days,
        reasons=reasons,
    )


class WakeWatcher:
    """Der Herzschlag ueber alles, was XENO je gesehen hat.

    Holt in einem Rutsch Marktdaten fuer alle bekannten Mints - DexScreener
    nimmt 30 Adressen je Anfrage - und meldet die, die aufgewacht sind. Das
    kostet keine RPC-Credits und konkurriert deshalb nicht mit dem
    Pruefbudget. Teuer wird erst die Tiefpruefung danach, und die trifft nur
    noch Token, bei denen tatsaechlich etwas passiert.
    """

    def __init__(
        self,
        dexscreener: DexScreener | None = None,
        interval: float = PULSE_INTERVAL,
        max_per_cycle: int = MAX_PER_CYCLE,
    ) -> None:
        self.dexscreener = dexscreener or DexScreener()
        self.interval = interval
        self.max_per_cycle = max_per_cycle
        self.last_pulse = 0.0
        #: Laufende Zaehler fuer die Anzeige.
        self.pulses = 0
        self.observed = 0
        #: Es wurde gefragt, aber nichts kam zurueck. Siehe ``scan``.
        self.blind = False

    def due(self, now: float | None = None) -> bool:
        now = now or time.time()
        return now - self.last_pulse >= self.interval

    def candidates(self, state: WatchState, now: float | None = None) -> list[str]:
        """Welche Mints der Puls ueberhaupt abfragt.

        Token unter einem Tag bleiben draussen: fuer sie ist der Faktor
        nicht berechenbar, und sie werden ohnehin engmaschig geprueft. Das
        haelt die Abfrage nebenbei klein - in einem frischen Zustand sind
        die meisten Eintraege jung.
        """
        now = now or time.time()
        wanted = []
        for entry in state.snapshot():
            first_seen = entry.get("first_seen") or 0
            if not first_seen:
                continue
            if (now - first_seen) / 60.0 < MIN_AGE_MINUTES:
                continue
            wanted.append(entry["mint"])
        return wanted

    def scan(self, state: WatchState, now: float | None = None) -> list[Wake]:
        """Ein Herzschlag: Marktdaten holen, Aufwacher zurueckgeben.

        Fehler werden nach oben durchgereicht - ein stiller Puls waere
        schlimmer als gar keiner: man verliesse sich auf eine Erkennung, die
        seit Tagen nichts mehr abfragt.

        Dasselbe gilt fuer den leisen Fall: ``enrich_many`` faengt Ausfaelle
        je Block selbst ab und liefert dann einfach weniger zurueck. Kam auf
        hundert Fragen keine einzige Antwort, ist das kein "nichts ist
        passiert", sondern "es konnte niemand nachsehen" - und das gehoert
        gesagt, nicht verschwiegen.
        """
        now = now or time.time()
        self.last_pulse = now
        self.pulses += 1
        self.blind = False

        mints = self.candidates(state, now)
        if not mints:
            return []

        enriched = self.dexscreener.enrich_many(
            [TokenCandidate(mint=mint) for mint in mints]
        )
        self.observed = len(enriched)
        if not enriched:
            self.blind = True
            return []

        found = []
        for candidate in enriched:
            wake = detect(candidate, state.get(candidate.mint), now)
            if wake is not None:
                found.append(wake)

        # Der staerkste zuerst - wenn das Budget nicht fuer alle reicht,
        # soll es an den gehen, bei dem am meisten passiert.
        found.sort(key=lambda w: w.burst, reverse=True)
        return found[: self.max_per_cycle]
