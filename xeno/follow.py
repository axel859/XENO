"""Nachverfolgung: was ist aus den geprueften Token geworden?

Ein Risikourteil ohne Rueckmeldung bleibt eine Behauptung. Diese Schicht
notiert zu jedem geprueften Token den Ausgangskurs und schaut spaeter nach,
was daraus wurde - damit sich die Frage "taugen die Urteile eigentlich?" mit
Zahlen statt mit Eindruecken beantworten laesst.

Wichtig fuer die Deutung: gemessen wird gegen das **erste** Urteil, nicht
gegen das aktuelle. Sonst wanderte der Bezugspunkt mit und man bekaeme eine
Statistik, die im Nachhinein immer recht hat.

Der Abruf ist bewusst billig gehalten: DexScreener liefert bis zu 30 Token in
einem einzigen Request, es fallen also keine RPC-Kosten an. Die Nachverfolgung
konkurriert damit nicht mit dem Pruefbudget.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sources import DexScreener
from .watchstate import TokenState, WatchState

#: Zeitpunkte, zu denen nachgeschaut wird, in Sekunden nach dem ersten Check.
HORIZONS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
}

#: Ab wann ein Token als praktisch wertlos gilt (Anteil vom Ausgangskurs).
DEAD_THRESHOLD = 0.1

#: Wie weit eine Messung hinter ihrem Zeitpunkt liegen darf.
#:
#: Wichtig, wenn der Bot nicht durchgehend laeuft: war er zwoelf Stunden aus,
#: waeren beim Neustart die Zeitpunkte 15min, 1h und 6h alle "faellig" - und
#: bekaemen denselben aktuellen Kurs eingetragen. Die Statistik behauptete
#: dann, ein Token habe sich nach 15 Minuten verzehnfacht, obwohl der Wert in
#: Wahrheit zwoelf Stunden spaeter gemessen wurde. Verpasste Zeitpunkte werden
#: deshalb als solche vermerkt statt geraten.
LATE_TOLERANCE = 0.5
MIN_TOLERANCE_SECONDS = 5 * 60


def deadline(horizon_seconds: int) -> float:
    """Spaetester Zeitpunkt, zu dem eine Messung noch gueltig ist."""
    return horizon_seconds + max(
        MIN_TOLERANCE_SECONDS, horizon_seconds * LATE_TOLERANCE
    )

#: So viele Mints passen in einen DexScreener-Request.
BATCH_SIZE = 30


@dataclass
class FollowStats:
    measured: int = 0
    missing: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def due_measurements(
    state: WatchState, now: float
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Welche Messungen jetzt anstehen und welche endgueltig verpasst sind.

    Rueckgabe ist ``(faellig, verpasst)``, beides als ``{"1h": [mint, ...]}``.

    Die Trennung ist der Kern: lief der Bot zwischendurch nicht, sind mehrere
    Zeitpunkte gleichzeitig "ueberfaellig". Sie alle mit dem aktuellen Kurs zu
    befuellen waere bequem und falsch - der Wert gehoerte dann zu einem ganz
    anderen Zeitpunkt als seiner Beschriftung.
    """
    pending: dict[str, list[str]] = {}
    missed: dict[str, list[str]] = {}

    for entry in state.snapshot():
        baseline_at = entry.get("baseline_at") or 0
        if not baseline_at or not entry.get("baseline_price_usd"):
            continue
        age = now - baseline_at
        recorded = entry.get("outcomes") or {}
        for label, seconds in HORIZONS.items():
            if label in recorded or age < seconds:
                continue
            target = pending if age <= deadline(seconds) else missed
            target.setdefault(label, []).append(entry["mint"])

    return pending, missed


class OutcomeTracker:
    def __init__(
        self, state: WatchState, dexscreener: DexScreener | None = None
    ) -> None:
        self.state = state
        self.dexscreener = dexscreener or DexScreener()

    def run(self, now: float, on_error=None) -> FollowStats:
        """Misst alle faelligen Zeitpunkte und traegt sie im Zustand ein."""
        stats = FollowStats()
        pending, missed = due_measurements(self.state, now)

        # Verpasste Zeitpunkte als solche festhalten, damit sie nicht bei
        # jedem Durchlauf erneut anstehen - und damit sie in der Auswertung
        # als Luecke erscheinen statt als erfundener Wert.
        for label, mints in missed.items():
            for mint in mints:
                entry = self.state.get(mint)
                if entry is not None:
                    entry.outcomes[label] = None  # type: ignore[assignment]
                    stats.missing += 1

        if not pending:
            return stats

        # Ein Token kann fuer mehrere Zeitpunkte faellig sein - der Kurs wird
        # trotzdem nur einmal geholt.
        wanted = sorted({mint for mints in pending.values() for mint in mints})
        try:
            prices = self.dexscreener.prices(wanted)
        except Exception as exc:  # noqa: BLE001
            message = f"Kursabruf fehlgeschlagen: {exc}"
            stats.errors.append(message)
            if on_error:
                on_error(message)
            return stats

        for label, mints in pending.items():
            for mint in mints:
                entry = self.state.get(mint)
                if entry is None or not entry.baseline_price_usd:
                    continue
                price = prices.get(mint)
                if price is None:
                    # Kein Paar mehr auffindbar - der Markt ist weg. Das ist
                    # keine fehlende Messung, sondern das Ergebnis "wertlos".
                    entry.outcomes[label] = 0.0
                    stats.measured += 1
                    continue
                entry.outcomes[label] = price / entry.baseline_price_usd
                stats.measured += 1

        return stats


def is_dead(multiple: float) -> bool:
    return multiple <= DEAD_THRESHOLD


def summarise(states: list[TokenState]) -> dict[str, dict[str, dict]]:
    """Fasst die Ergebnisse je Erst-Urteil zusammen.

    Der Median wird bewusst dem Mittelwert vorgezogen: ein einzelner Token,
    der sich verhundertfacht, wuerde einen Durchschnitt so verzerren, dass
    zwanzig Totalverluste daneben nicht mehr auffallen.
    """
    from statistics import median

    grouped: dict[str, list[TokenState]] = {}
    for entry in states:
        if not entry.first_verdict or not entry.outcomes:
            continue
        grouped.setdefault(entry.first_verdict, []).append(entry)

    summary: dict[str, dict[str, dict]] = {}
    for verdict, entries in grouped.items():
        per_horizon: dict[str, dict] = {}
        for label in HORIZONS:
            # None steht fuer "Zeitpunkt verpasst, weil der Bot aus war" -
            # das ist keine Null, sondern gar keine Messung.
            values = [
                e.outcomes[label]
                for e in entries
                if e.outcomes.get(label) is not None
            ]
            if not values:
                continue
            per_horizon[label] = {
                "count": len(values),
                "median": median(values),
                "best": max(values),
                "dead_pct": 100.0 * sum(1 for v in values if is_dead(v)) / len(values),
                "winners_pct": 100.0 * sum(1 for v in values if v >= 2.0) / len(values),
            }
        if per_horizon:
            summary[verdict] = per_horizon
    return summary
