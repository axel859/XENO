"""Papierhandel - der Auto-Trader ohne Geld.

Dies ist bewusst kein Simulator neben dem eigentlichen Handel, sondern
dieselbe Entscheidungskette: Signal rein, Einstieg, Ausstiegsregel,
Position schliessen, Bilanz. Wer spaeter echtes Geld einsetzen will,
schaltet an genau dieser Stelle um - es entsteht keine zweite Software,
deren Verhalten von der getesteten abweicht.

**Warum das vor dem echten Handel kommt.** XENO hat bisher nie belegt, dass
seine Urteile in die richtige Richtung zeigen; die einzige Beobachtung war,
dass die abgelehnten Token besser liefen als die empfohlenen. Wer das
automatisiert, verliert nicht langsamer, sondern schneller und rund um die
Uhr. Eine Woche Papierhandel kostet nichts und beantwortet die Frage.

**Die Ausstiegsregel ist der eigentliche Inhalt.** Ein Einstieg ohne
geplanten Ausstieg ist kein Handel, sondern eine Hoffnung. Sie steht
deshalb fest, bevor eine Position eroeffnet wird, und sie ist stumpf:
Ziel, Verlustgrenze, Zeitlimit. Ein Programm hat gegenueber einem Menschen
an dieser Stelle genau einen Vorteil - es wird nicht gierig.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median

BOOK_FILE_NAME = "paper-trades.json"

#: Einsatz je Position. Nur eine Rechengroesse - es fliesst kein Geld.
DEFAULT_SIZE_USD = 100.0

#: Ausstiegsregel. Bewusst grob: die Frage ist erst einmal, ob die Signale
#: ueberhaupt taugen, nicht ob 2.1x besser waere als 2.0x.
TAKE_PROFIT = 2.0      # verdoppelt -> raus
STOP_LOSS = 0.6        # 40 Prozent im Minus -> raus
MAX_HOLD_SECONDS = 24 * 3600

#: Was vom Einsatz nach Hin- und Rueckweg uebrig bleibt, wenn zu einem Token
#: keine eigene Messung vorliegt.
#:
#: Gemessen ueber Jupiter an neun Token des early-Profils bei 100 USD
#: Ordergroesse: Verlust 0.8% bis 5.6%, Median 4.4%. Die Spanne haengt fast
#: nur an der Pooltiefe - der Token mit 70.000 USD Liquiditaet kostete 0.8%,
#: die mit 3.000 USD ueber 5%.
#:
#: Gebraucht wird der Wert vor allem fuer die Vergleichsgruppe: die
#: abgelehnten Token werden nie tief geprueft, es gibt zu ihnen also keinen
#: gemessenen Rueckweg. Sie mit 0% zu rechnen waere die schlechteste
#: Variante - dann saehe ausgerechnet der Massstab guenstiger aus als das,
#: was er messen soll. Der Standardwert ist der Median genau der Messungen,
#: die die geprueften Token liefern - damit unterscheiden sich beide Gruppen
#: im Mittel nicht durch die Kostenannahme, sondern nur durch die Streuung.
DEFAULT_RETENTION = 0.956

#: Was das Durchbringen einer Transaktion kostet, fuer Kauf und Verkauf
#: zusammen.
#:
#: Auf Solana zahlt man ueber die Grundgebuehr hinaus eine Prioritaetsgebuehr,
#: sonst bleibt die Transaktion bei Andrang liegen - und genau bei frischen
#: Memecoins ist Andrang. Angesetzt sind rund 0.001 SOL je Transaktion bei
#: etwa 200 USD je SOL, also 0.20 USD, zweimal.
#:
#: Das ist eine Annahme, keine Messung: die tatsaechliche Gebuehr haengt an
#: der Auslastung und an der eigenen Einstellung. Sie steht hier trotzdem,
#: weil null anzusetzen die groessere Luege waere. Ueber ``XENO_FEE_USD``
#: laesst sie sich anpassen.
DEFAULT_FEE_USD = 0.40


def _fee_usd() -> float:
    raw = os.environ.get("XENO_FEE_USD", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_FEE_USD


def measured_retention(round_trip) -> float | None:
    """Der gemessene Rueckweg eines Berichts - oder ``None``.

    **Ein Wert ab 1.0 wird verworfen.** Er hiesse, dass Kauf und sofortiger
    Verkauf mehr einbringen als sie kosten; das gaebe es nur als risikolose
    Arbitrage. Bei frischen Bonding-Curve-Pools liefert Jupiter genau solche
    Werte, weil beide Seiten gegen unterschiedlich aktuelle Poolstaende
    gerechnet werden. Ihn zu uebernehmen hiesse, eine Position mit Gewinn zu
    eroeffnen, bevor sich der Kurs bewegt hat.

    **Ein schlechter Wert wird dagegen uebernommen, so schlecht er ist.**
    Wenn Jupiter sagt, vom Einsatz kaeme die Haelfte zurueck, dann ist das
    das Ergebnis und nicht ein Ausreisser. Genau diese Token sind der Grund,
    warum die Bilanz Kosten braucht.
    """
    if round_trip is None:
        return None
    value = getattr(round_trip, "retention", None)
    if value is None or getattr(round_trip, "unreliable", False):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value < 1.0:
        return None
    return value


#: Ab hier war nicht der Kurs so hoch, sondern der Einstiegswert kaputt.
#:
#: Abgeleitet aus der Ausstiegsregel statt frei gegriffen: sie loest bei 2x
#: aus und schaut alle 25 Sekunden nach. Damit eine geschlossene Position
#: auf das Zwanzigfache kommt, muesste der Kurs in einem einzigen Fenster
#: von unter 2x auf ueber 20x springen. Das gibt es nicht - wohl aber
#: Kursquellen, die fuer sekundenalte Pools fast null liefern.
MAX_CREDIBLE_MULTIPLE = TAKE_PROFIT * 10


@dataclass
class Position:
    """Eine offene oder abgeschlossene Papierposition."""

    mint: str
    symbol: str = ""
    opened_at: float = 0.0
    entry_price: float = 0.0
    size_usd: float = DEFAULT_SIZE_USD
    strength: int = 0
    reasons: list[str] = field(default_factory=list)

    #: Urteil beim Einstieg - OK, CAUTION, RISKY, AVOID, UNKNOWN oder
    #: CONTROL fuer die im Vorfilter abgelehnten. Bewusst festgehalten und
    #: nie nachtraeglich geaendert: gemessen wird, was XENO beim ersten
    #: Hinsehen gesagt hat, nicht was er spaeter besser wusste.
    group: str = "UNKNOWN"
    #: Ob dieser Einstieg zusaetzlich die Call-Bedingungen erfuellte. Ein
    #: Call ist eine Teilmenge von OK, keine eigene Kategorie - so laesst
    #: sich beides vergleichen, ohne die Positionen doppelt zu fuehren.
    is_call: bool = False

    #: Bewertung beim Einstieg. Die interessantere Zahl als der Kurs, weil
    #: sie sich zwischen Token vergleichen laesst.
    entry_mcap_usd: float | None = None

    #: Anteil des Einsatzes, der einen Hin- und Rueckweg ueberlebt - beim
    #: Einstieg von Jupiter gemessen, sonst ``DEFAULT_RETENTION``. Darin
    #: stecken Swap-Gebuehr und Preiseinfluss, also alles, was zwischen dem
    #: Kurs auf dem Chart und dem Kontostand steht.
    #:
    #: Zwei Einschraenkungen gehoeren dazu, damit die Zahl nicht genauer
    #: aussieht als sie ist. Gemessen wird mit 0.1 SOL, gehandelt werden
    #: 100 USD, also rund 0.5 SOL - der Preiseinfluss der echten Order ist
    #: damit eher hoeher. Und gemessen wird beim **Einstieg**; ob der Pool
    #: beim Ausstieg noch so tief ist, weiss beim Kauf niemand. Bei einem
    #: Ausstieg an der Verlustgrenze ist er es regelmaessig nicht.
    retention: float = DEFAULT_RETENTION
    #: Ob der Wert gemessen wurde oder der Standardwert ist. Gehoert
    #: festgehalten: eine geschaetzte Zahl darf sich nicht als gemessene
    #: ausgeben.
    retention_measured: bool = False
    #: Prioritaetsgebuehren fuer Kauf und Verkauf zusammen.
    fee_usd: float = DEFAULT_FEE_USD

    closed_at: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    #: Hoechster Kurs seit Einstieg - zeigt, was ein besserer Ausstieg
    #: gebracht haette.
    peak_price: float = 0.0
    #: Zuletzt gesehener Kurs. Ohne ihn haetten die Positionen der
    #: Vergleichsgruppe nie einen aktuellen Wert: sie werden nie tief
    #: geprueft, also liegt zu ihnen auch nie ein Bericht vor, aus dem sich
    #: einer ablesen liesse.
    last_price: float = 0.0
    last_price_at: float = 0.0

    @property
    def open(self) -> bool:
        return not self.closed_at

    def mcap_at(self, price: float | None) -> float | None:
        """Bewertung bei einem gegebenen Kurs.

        Hochgerechnet statt abgefragt: die Supply eines Memecoins liegt fest,
        also bewegt sich die Bewertung genau wie der Kurs. Das erspart eine
        zweite Abfrage je Position - und die Kurse holen wir ohnehin.
        """
        if not self.entry_mcap_usd or not self.entry_price or not price:
            return None
        return self.entry_mcap_usd * (price / self.entry_price)

    @property
    def exit_mcap_usd(self) -> float | None:
        return self.mcap_at(self.exit_price) if self.closed_at else None

    @property
    def multiple(self) -> float | None:
        """Vielfaches des Einstiegskurses.

        Bei einer geschlossenen Position ist ein Kurs von null **kein**
        fehlender Wert, sondern das Ergebnis: der Markt ist verschwunden.
        Ihn als "keine Angabe" zu behandeln wuerde Totalverluste aus jeder
        Auswertung herausfallen lassen - und die Bilanz schoenrechnen.
        """
        if not self.entry_price:
            return None
        if self.closed_at:
            return self.exit_price / self.entry_price
        return self.peak_price / self.entry_price if self.peak_price else None

    @property
    def credited_multiple(self) -> float | None:
        """Das Vielfache, mit dem gerechnet werden darf.

        **Nach oben gedeckelt, nach unten nicht.** Der Grund steht in einer
        echten Bilanz: 69 abgeschlossene Trades zu 100 USD, Ausstieg bei 2x -
        rechnerisch also hoechstens +6.900 USD moeglich. In der Tabelle
        standen +8.789 USD, bei 41% Treffern und einem Median von 0.52x.

        Die Erklaerung liegt in ``decide``: die Regel loest bei 2x aus, und
        notiert wird der Kurs, der beim Nachsehen dasteht. Springt er
        zwischen zwei Abfragen weit darueber, schreibt sich die Simulation
        den ganzen Sprung gut. Nur bekaeme den niemand: XENO sucht Token mit
        wenigen tausend Dollar Liquiditaet, und wer dort in eine Spitze
        verkauft, bekommt den Buchkurs nicht, sondern was das Orderbuch
        hergibt. Eine Simulation, die sich Ausfuehrungen gutschreibt, die es
        nicht gibt, ist als Massstab wertlos.

        Nach unten wird bewusst **nicht** gedeckelt: dort ist derselbe
        Effekt real. Wer unter die Verlustgrenze rutscht, verkauft
        tatsaechlich schlechter als geplant. Die Unsymmetrie ist keine
        Nachlaessigkeit, sondern genau das, was am Markt passiert.
        """
        value = self.multiple
        if value is None:
            return None
        return min(value, TAKE_PROFIT) * self.retention

    @property
    def broken(self) -> bool:
        """Ob der Einstiegskurs unbrauchbar war.

        Aufgefallen an einer echten Bilanz: 20.800 USD Umsatz, angeblich
        20.595.246 USD Gewinn. Ein Vielfaches von zweihunderttausend ist bei
        einer Ausstiegsregel, die bei 2x verkauft, rechnerisch unmoeglich -
        da war nicht der Kurs so hoch, sondern der Einstiegswert nahe null.
        Das passiert bei sekundenalten Pools, wo die Kursquelle noch keinen
        belastbaren Wert hat.

        Solche Positionen fliegen aus der Bilanz und werden gezaehlt. Sie
        stehen zu lassen waere schlimmer als sie wegzulassen: eine einzige
        davon macht jede Gruppenauswertung daneben unlesbar.
        """
        value = self.multiple
        return value is not None and value >= MAX_CREDIBLE_MULTIPLE

    def _capped_ratio(self, price: float | None) -> float | None:
        """Das gedeckelte Kursverhaeltnis, mit dem gerechnet werden darf."""
        if not self.entry_price:
            return None
        if self.closed_at:
            ratio = self.exit_price / self.entry_price
        elif price:
            ratio = price / self.entry_price
        else:
            # Offene Position ohne aktuellen Kurs: hier ist tatsaechlich
            # nichts bekannt.
            return None
        return min(ratio, TAKE_PROFIT)

    def gross_result_usd(self, price: float | None = None) -> float | None:
        """Was der **Kurs** hergegeben haette, ohne Kosten.

        Steht nicht als Ergebnis in der Bilanz, sondern daneben: erst der
        Abstand zwischen dieser Zahl und ``result_usd`` zeigt, wieviel vom
        Chart auf dem Weg zum Kontostand verlorengeht. Bei einer Strategie,
        die auf 2x zielt, ist das keine Nebensache - die Kosten fressen
        einen zweistelligen Prozentsatz des Ziels.
        """
        ratio = self._capped_ratio(price)
        if ratio is None:
            return None
        return self.size_usd * (ratio - 1.0)

    def cost_usd(self, price: float | None = None) -> float | None:
        """Was Handel und Ausfuehrung kosten - Swap-Weg plus Gebuehren.

        Der Swap-Anteil wird auf den **Ausstiegswert** gerechnet, nicht auf
        den Einsatz: wer mit 100 USD einsteigt und bei 2x aussteigt, bewegt
        auf dem Rueckweg 200 USD, und der Preiseinfluss haengt an dem, was
        durch den Pool geht. Ein Gewinn kostet dadurch mehr als ein Verlust -
        was unangenehm klingt, aber genau so passiert.
        """
        ratio = self._capped_ratio(price)
        if ratio is None:
            return None
        return self.size_usd * ratio * (1.0 - self.retention) + self.fee_usd

    def result_usd(self, price: float | None = None) -> float | None:
        """Gewinn oder Verlust in Dollar, nach allen Kosten.

        Gerechnet wird mit dem gedeckelten Vielfachen - siehe
        ``credited_multiple``. Ohne den Deckel schrieb sich die Simulation
        Ausfuehrungen gut, die es bei diesen Liquiditaeten nicht gibt.

        Bei einer **offenen** Position stecken die Kosten beider Wege schon
        drin, obwohl erst einer gegangen wurde. Das ist die ehrlichere
        Anzeige: der Betrag beantwortet die Frage "was bekaeme ich, wenn ich
        jetzt verkaufe", und dieses Verkaufen kostet eben noch etwas.
        """
        ratio = self._capped_ratio(price)
        if ratio is None:
            return None
        return self.size_usd * (ratio * self.retention - 1.0) - self.fee_usd

    def decide(self, price: float, now: float) -> str:
        """Ob und warum diese Position jetzt geschlossen wird."""
        if not self.entry_price or price <= 0:
            return ""
        ratio = price / self.entry_price
        if ratio >= TAKE_PROFIT:
            return "ziel"
        if ratio <= STOP_LOSS:
            return "verlustgrenze"
        if now - self.opened_at >= MAX_HOLD_SECONDS:
            return "zeitlimit"
        return ""


def book_path_for(state_path) -> Path:
    """Wo das Papierbuch zu einer Zustandsdatei liegt.

    Beim Standardpfad bleibt es, wo es immer lag - sonst waere ein
    bestehendes Buch nach einem Update verwaist.

    Bei einer **eigenen** Zustandsdatei bekommt es einen eigenen Namen
    daneben. Genau dafuer gibt es ``--state-file``: um zwei Einstellungen
    getrennt zu messen. Liefe der Papierhandel weiter in dasselbe Buch,
    mischten sich beide Versuchsreihen, und die Trades-Tabelle - die
    eigentliche Antwort auf "taugt es etwas" - waere ein Brei aus beidem.
    """
    from .paths import STATE_FILE_NAME, data_dir, target_path

    path = Path(state_path)
    if path == target_path(STATE_FILE_NAME):
        return data_dir() / BOOK_FILE_NAME
    return path.with_name(f"{path.stem}-paper.json")


class PaperBook:
    """Alle Papierpositionen, dauerhaft gespeichert."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .paths import data_dir

            path = data_dir() / BOOK_FILE_NAME
        self.path = Path(path)
        self.positions: list[Position] = []
        self._lock = threading.RLock()
        self._dirty = False
        self.load()

    # -- Persistenz -------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        known = set(Position.__dataclass_fields__)
        with self._lock:
            for raw in payload.get("positions") or []:
                if isinstance(raw, dict):
                    self.positions.append(
                        Position(**{k: v for k, v in raw.items() if k in known})
                    )

    def save(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "positions": [asdict(p) for p in self.positions],
            }
            self._dirty = False

        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".paper-", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    # -- Handel -----------------------------------------------------------

    @property
    def open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self.positions if p.open]

    @property
    def closed_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self.positions if not p.open]

    def holds(self, mint: str) -> bool:
        return any(p.mint == mint and p.open for p in self.positions)

    def enter(
        self,
        mint: str,
        price: float | None,
        *,
        symbol: str = "",
        group: str = "UNKNOWN",
        is_call: bool = False,
        mcap: float | None = None,
        strength: int = 0,
        reasons: list[str] | None = None,
        round_trip=None,
        now: float | None = None,
    ) -> Position | None:
        """Eroeffnet eine Position.

        Simuliert wird **jedes** geprueft Urteil, nicht nur die Calls. Sonst
        gaebe es am Ende zwar eine Zahl fuer die Vorschlaege, aber keine
        Vergleichszahl - und ob die strengen Bedingungen ueberhaupt etwas
        bringen, bliebe offen.

        Kein Nachkaufen: ein zweiter Einstieg in denselben Token waehrend die
        Position laeuft wuerde die Auswertung verfaelschen, weil derselbe
        Kursverlauf doppelt zaehlte.

        Die Kosten werden **beim Einstieg** festgeschrieben, nicht beim
        Ausstieg berechnet. Das ist Absicht: der Kauf-Verkauf-Test von
        Jupiter laeuft ohnehin in der Tiefpruefung, und zwar genau in dem
        Moment, in dem ein echter Kauf stattfaende. Spaeter nachzumessen
        haette ihn zu einem anderen Zeitpunkt gemessen als dem, den er
        beschreiben soll.
        """
        if not price or self.holds(mint):
            return None
        now = now or time.time()
        measured = measured_retention(round_trip)
        position = Position(
            mint=mint,
            symbol=symbol,
            opened_at=now,
            entry_price=price,
            peak_price=price,
            group=group,
            is_call=is_call,
            entry_mcap_usd=mcap,
            strength=strength,
            reasons=list(reasons or []),
            retention=DEFAULT_RETENTION if measured is None else measured,
            retention_measured=measured is not None,
            fee_usd=_fee_usd(),
        )
        with self._lock:
            self.positions.append(position)
            self._dirty = True
        return position

    def enter_report(self, report, call=None, now: float | None = None) -> Position | None:
        """Bequemer Weg aus einem fertigen Bericht."""
        candidate = report.candidate
        return self.enter(
            report.mint,
            candidate.price_usd if candidate else None,
            symbol=report.symbol,
            group=report.verdict.value,
            is_call=call is not None,
            mcap=candidate.mcap_usd if candidate else None,
            strength=call.strength if call else 0,
            reasons=list(call.reasons) if call else [],
            round_trip=getattr(report, "round_trip", None),
            now=now,
        )

    def update(self, prices: dict[str, float], now: float | None = None) -> list[Position]:
        """Traegt neue Kurse ein und schliesst, was faellig ist.

        Ein Token, fuer den es keinen Kurs mehr gibt, ist nicht "unbekannt" -
        sein Markt ist verschwunden. Das ist das Ergebnis "wertlos", und es
        wird auch so verbucht.
        """
        now = now or time.time()
        closed: list[Position] = []
        with self._lock:
            for position in self.positions:
                if not position.open:
                    continue
                # Ein Kurs von null oder darunter ist keine Angabe, sondern
                # eine kaputte. Er wird wie ein fehlender behandelt: sonst
                # bliebe die Position ewig offen, weil die Ausstiegsregel mit
                # einem solchen Wert nichts anfangen kann - nicht einmal das
                # Zeitlimit griffe.
                price = prices.get(position.mint) or None
                if price is None or price <= 0:
                    if now - position.opened_at >= MAX_HOLD_SECONDS:
                        position.closed_at = now
                        position.exit_price = 0.0
                        position.exit_reason = "markt_weg"
                        closed.append(position)
                        self._dirty = True
                    continue

                position.last_price = price
                position.last_price_at = now
                if price > position.peak_price:
                    position.peak_price = price
                reason = position.decide(price, now)
                if reason:
                    position.closed_at = now
                    position.exit_price = price
                    position.exit_reason = reason
                    closed.append(position)
                self._dirty = True
        return closed

    # -- Auswertung -------------------------------------------------------

    def summary(self, prices: dict[str, float] | None = None) -> dict:
        """Die Zahl, um die es geht: was waere herausgekommen."""
        with self._lock:
            positions = list(self.positions)
        return _stats(positions, prices or {})

    def by_group(self, prices: dict[str, float] | None = None) -> dict[str, dict]:
        """Bilanz je Urteilsgruppe - und die Calls als eigene Zeile.

        Das ist die Auswertung, um die es eigentlich geht: liefen die
        empfohlenen Token besser als die abgelehnten? Wenn nicht, zeigt der
        Filter in die falsche Richtung, und keine noch so gute
        Einzelpruefung aendert daran etwas.

        Die Calls stehen zusaetzlich zu ihrer Urteilsgruppe. Sie sind eine
        Teilmenge von OK - erst der Vergleich beider Zeilen zeigt, ob die
        strengen Bedingungen ihr Geld wert sind oder nur dafuer sorgen, dass
        seltener gehandelt wird.
        """
        with self._lock:
            positions = list(self.positions)

        buckets: dict[str, list[Position]] = {}
        for position in positions:
            buckets.setdefault(position.group, []).append(position)
            if position.is_call:
                buckets.setdefault("CALL", []).append(position)

        return {name: _stats(group, prices or {}) for name, group in buckets.items()}

    def hit_rate_text(self) -> str:
        """Die Bilanz in einem Satz - gehoert an jeden Call.

        Ohne diese Zahl ist ein Call eine Vermutung mit selbstbewusstem
        Etikett.
        """
        closed = [p for p in self.closed_positions if not p.broken]
        if not closed:
            return "noch keine abgeschlossenen Calls"
        wins = sum(1 for p in closed if (p.result_usd() or 0) > 0)
        return f"von {len(closed)} abgeschlossenen Calls waren {wins} im Plus"


#: Abstand zwischen zwei Kursabfragen fuer offene Positionen.
#:
#: Bewusst entkoppelt vom Pruefzyklus. Der laeuft je nach Kontingent alle
#: ein bis fuenf Minuten - viel zu grob fuer eine Ausstiegsregel: ein Token,
#: der zwischen zwei Messungen auf 2.5x schiesst und auf 1.1x zurueckfaellt,
#: hat sein Ziel erreicht, ohne dass es jemand gesehen haette. Die Bilanz
#: faellt dadurch schlechter aus als die Strategie wirklich ist.
#:
#: Kostet nichts: die Kurse kommen von DexScreener, dreissig Token je
#: Anfrage, ohne Kontingent. Teuer sind nur die Pruefungen.
TRACK_INTERVAL = 25.0


class PositionTracker:
    """Verfolgt offene Positionen unabhaengig vom Pruefzyklus.

    Das ist zugleich die Schleife, die ein echter Auto-Trader spaeter
    braucht: beim Ausstieg entscheidet die Reaktionszeit mit ueber das
    Ergebnis. Sie jetzt richtig zu bauen erspart es, sie spaeter
    nachzuruesten - und die Papierbilanz misst dann dasselbe Verhalten, das
    mit echtem Geld liefe.
    """

    def __init__(
        self,
        book: "PaperBook",
        dexscreener,
        interval: float = TRACK_INTERVAL,
        log=None,
    ) -> None:
        self.book = book
        self.dexscreener = dexscreener
        self.interval = max(5.0, interval)
        self.log = log or (lambda message: None)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.ticks = 0
        self.errors = 0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def tick(self, now: float | None = None) -> list[Position]:
        """Ein Durchgang: Kurse holen, faellige Positionen schliessen.

        Getrennt vom Thread, damit sich das Verhalten ohne Warten und ohne
        Nebenlaeufigkeit pruefen laesst.
        """
        open_positions = self.book.open_positions
        if not open_positions:
            return []
        try:
            prices = self.dexscreener.prices([p.mint for p in open_positions])
        except Exception as exc:  # noqa: BLE001
            # Eine ausgefallene Kursquelle darf die Schleife nie beenden -
            # sonst stuenden die Positionen still, ohne dass es auffiele.
            self.errors += 1
            self.log(f"Positionen: Kurse nicht abrufbar: {exc}")
            return []

        self.ticks += 1
        closed = self.book.update(prices, now=now)
        for position in closed:
            result = position.result_usd() or 0.0
            self.log(
                f"Position geschlossen: {position.symbol or position.mint[:8]} "
                f"({position.group}) zu {position.exit_reason}, {result:+.2f} USD"
            )
        self.book.save()
        return closed

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self.log(f"Positionsverfolgung gestolpert: {exc}")
            self.stop_event.wait(self.interval)

    def start(self) -> bool:
        if self.running:
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="xeno-positions"
        )
        self.thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        if not self.running:
            return False
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        return True


def _stats(positions: list[Position], prices: dict[str, float]) -> dict:
    """Kennzahlen einer Gruppe von Positionen.

    Positionen mit kaputtem Einstiegskurs fliegen vorher raus - siehe
    ``Position.broken``. Eine einzige davon hat die Gesamtbilanz einmal auf
    20,5 Millionen Dollar gehoben.
    """
    broken = [p for p in positions if p.broken]
    positions = [p for p in positions if not p.broken]
    closed = [p for p in positions if not p.open]
    still_open = [p for p in positions if p.open]

    realised = [r for r in (p.result_usd() for p in closed) if r is not None]
    wins = [r for r in realised if r > 0]
    multiples = [m for m in (p.multiple for p in closed) if m is not None]

    # Kosten der abgeschlossenen Positionen, getrennt ausgewiesen. Sie
    # stecken bereits in ``result_usd`` - hier stehen sie noch einmal
    # einzeln, weil sonst niemand sehen kann, wieviel von der Bilanz an der
    # Strategie liegt und wieviel an der Ausfuehrung.
    gross = [r for r in (p.gross_result_usd() for p in closed) if r is not None]
    costs = [c for c in (p.cost_usd() for p in closed) if c is not None]
    fees = sum(p.fee_usd for p in closed if p.result_usd() is not None)

    unrealised = 0.0
    for position in still_open:
        value = position.result_usd(prices.get(position.mint) or position.last_price)
        if value is not None:
            unrealised += value

    measured = [p for p in positions if p.retention_measured]

    return {
        "open": len(still_open),
        "closed": len(closed),
        "result_usd": round(sum(realised), 2),
        "unrealised_usd": round(unrealised, 2),
        "wins": len(wins),
        "win_rate": round(100.0 * len(wins) / len(realised), 1) if realised else 0.0,
        "median_multiple": round(median(multiples), 2) if multiples else None,
        "best_multiple": round(max(multiples), 2) if multiples else None,
        # Gesamteinsatz - der "Umsatz" der Simulation.
        "invested_usd": round(sum(p.size_usd for p in closed), 2),
        "turnover_usd": round(sum(p.size_usd for p in positions), 2),
        #: Was ohne Handelskosten dagestanden haette. Die Differenz zu
        #: ``result_usd`` ist ``costs_usd``.
        "gross_result_usd": round(sum(gross), 2),
        "costs_usd": round(sum(costs), 2),
        #: Nur der Gebuehrenanteil. Der Rest der Kosten ist Swap-Weg.
        "fees_usd": round(fees, 2),
        #: Wie oft der Rueckweg wirklich gemessen wurde statt geschaetzt.
        #: Ohne diese Zahl liesse sich nicht sagen, wie belastbar die
        #: Kostenseite ist.
        "measured": len(measured),
        "avg_retention": (
            round(sum(p.retention for p in positions) / len(positions), 4)
            if positions
            else None
        ),
        #: Aussortiert wegen unbrauchbarem Einstiegskurs - benannt statt
        #: verschwiegen, sonst fehlt in der Bilanz still etwas.
        "broken": len(broken),
        "reasons": _reason_counts(closed),
    }


def _reason_counts(positions: list[Position]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for position in positions:
        if position.exit_reason:
            counts[position.exit_reason] = counts.get(position.exit_reason, 0) + 1
    return counts
