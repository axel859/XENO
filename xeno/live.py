"""Live-Strom neu erstellter Token.

Statt alle 60 Sekunden zu fragen, ob es etwas Neues gibt, meldet der
Solana-Knoten es sofort. Das Abo laeuft auf die Programm-Logs von pump.fun;
jede Token-Erstellung erscheint dort binnen einer Sekunde.

**Der Mint steht bereits im Log.** pump.fun schreibt bei der Erstellung ein
Ereignis mit Name, Symbol, Mint und Ersteller in die Logs. Das heisst: der
gesamte Strom kostet keine einzige zusaetzliche Anfrage - egal wie viele
Token entstehen.

Zur Groessenordnung, gemessen: rund 170 Ereignisse pro Sekunde ueber alle
pump.fun-Aktivitaeten, davon etwa 40 Token-Erstellungen pro Minute. Gefiltert
wird deshalb im Client, bevor irgendetwas nachgeladen wird.

Was der Live-Strom bringt und was nicht: er liefert **Vollstaendigkeit und
den exakten Geburtszeitpunkt** - jeder Token, nicht nur die, die ein
Datendienst zufaellig schon erfasst hat. Beurteilen laesst sich ein Token in
Sekunde eins trotzdem nicht, denn dann hat noch niemand gehandelt. Der Strom
sammelt deshalb, und geprueft wird, sobald genug Handel stattgefunden hat.
"""

from __future__ import annotations

import base64
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .ws import WebSocket, WebSocketError

#: pump.fun-Programm. Auf dessen Logs wird gelauscht.
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

#: Erste acht Bytes des Erstellungs-Ereignisses. Andere Ereignisse (Kauf,
#: Verkauf) tragen andere Kennungen und werden so aussortiert, ohne dass ihr
#: Inhalt ueberhaupt gelesen werden muss.
CREATE_DISCRIMINATOR = bytes.fromhex("1b72a94ddeeb6376")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Obergrenze fuer wartende Token. Bei rund 40 Erstellungen pro Minute
#: waeren es sonst nach einem Tag ueber 50.000.
MAX_PENDING = 2_000


def b58encode(data: bytes) -> str:
    """Base58 wie bei Solana ueblich - fuehrende Nullbytes werden zu '1'."""
    number = int.from_bytes(data, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = _B58_ALPHABET[remainder] + encoded
    leading_zeros = len(data) - len(data.lstrip(b"\0"))
    return "1" * leading_zeros + encoded


@dataclass
class NewToken:
    """Ein gerade erstellter Token, direkt aus dem Log gelesen."""

    mint: str
    name: str = ""
    symbol: str = ""
    creator: str = ""
    signature: str = ""
    #: Zeitpunkt des Empfangs - praktisch der Geburtszeitpunkt.
    seen_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.seen_at


def parse_create_event(blob: bytes) -> NewToken | None:
    """Liest ein Erstellungs-Ereignis.

    Aufbau: acht Byte Kennung, dann drei laengen-praefixierte Zeichenketten
    (Name, Symbol, URI), danach 32-Byte-Adressen - zuerst der Mint.

    Gibt None zurueck, wenn es kein Erstellungs-Ereignis ist oder die Daten
    nicht zum erwarteten Aufbau passen. Ein veraendertes Format soll den
    Strom nicht abreissen lassen.
    """
    if len(blob) < 8 or blob[:8] != CREATE_DISCRIMINATOR:
        return None

    position = 8
    fields: list[str] = []
    try:
        for _ in range(3):
            (length,) = struct.unpack_from("<I", blob, position)
            position += 4
            if length > 1024 or position + length > len(blob):
                return None
            fields.append(blob[position : position + length].decode("utf-8", "replace"))
            position += length

        if position + 32 > len(blob):
            return None
        mint = b58encode(blob[position : position + 32])
        # Danach folgen bonding curve und deren Token-Konto, dann der Ersteller.
        creator_at = position + 96
        creator = (
            b58encode(blob[creator_at : creator_at + 32])
            if creator_at + 32 <= len(blob)
            else ""
        )
    except (struct.error, IndexError):
        return None

    name, symbol, _uri = fields
    return NewToken(mint=mint, name=name, symbol=symbol, creator=creator)


def extract_new_tokens(logs: list[str], signature: str = "") -> list[NewToken]:
    """Sucht Erstellungs-Ereignisse in den Logs einer Transaktion."""
    found: list[NewToken] = []
    for line in logs:
        if not line.startswith("Program data:"):
            continue
        payload = line.split("Program data:", 1)[1].strip()
        try:
            blob = base64.b64decode(payload)
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            continue
        token = parse_create_event(blob)
        if token is not None:
            token.signature = signature
            found.append(token)
    return found


class PumpFunStream:
    """Abonniert die Erstellungen und liefert sie als Strom."""

    def __init__(self, ws_url: str, program: str = PUMP_FUN_PROGRAM) -> None:
        self.ws_url = ws_url
        self.program = program

    def subscribe(self, socket: WebSocket) -> None:
        socket.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [self.program]},
                    # "processed" ist die frueheste Stufe - hier zaehlt Tempo,
                    # und eine Fehlmeldung kostet nur eine spaetere Pruefung.
                    {"commitment": "processed"},
                ],
            }
        )

    def listen(self, stop_event=None, on_error: Callable[[str], None] | None = None) -> Iterator[NewToken]:
        """Verbindet sich und liefert neue Token, bis gestoppt wird.

        Bricht die Verbindung ab, wird mit wachsendem Abstand neu verbunden -
        ein Aussetzer beim Anbieter darf den Strom nicht dauerhaft beenden.
        """
        delay = 1.0
        while stop_event is None or not stop_event.is_set():
            socket = WebSocket(self.ws_url)
            try:
                socket.connect()
                self.subscribe(socket)
                socket.settimeout(60.0)
                delay = 1.0  # Verbindung steht - Wartezeit zuruecksetzen

                while stop_event is None or not stop_event.is_set():
                    message = socket.recv_json()
                    if message is None:
                        break
                    value = (
                        (message.get("params") or {}).get("result", {}).get("value") or {}
                    )
                    logs = value.get("logs") or []
                    if not logs:
                        continue
                    for token in extract_new_tokens(logs, value.get("signature", "")):
                        yield token

            except (WebSocketError, OSError, ValueError) as exc:
                if on_error is not None:
                    on_error(f"Live-Strom unterbrochen: {exc}")
            finally:
                socket.close()

            if stop_event is not None and stop_event.wait(delay):
                break
            if stop_event is None:
                time.sleep(delay)
            delay = min(delay * 2, 60.0)


class LiveFeed:
    """Betreibt den Strom im Hintergrund und sammelt die Neuzugaenge.

    Die Trennung ist noetig, weil ein Token in Sekunde eins nicht bewertbar
    ist: es hat noch niemand gehandelt. Gesammelt wird deshalb erst, und der
    Watcher holt sich die Token, sobald sie alt genug fuer eine Aussage sind.
    """

    def __init__(self, ws_url: str, max_pending: int = MAX_PENDING) -> None:
        self.stream = PumpFunStream(ws_url)
        self.max_pending = max_pending
        self._lock = threading.Lock()
        self._pending: dict[str, NewToken] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.received = 0
        self.last_error = ""
        self.connected_since: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def start(self, on_error: Callable[[str], None] | None = None) -> bool:
        if self.running:
            return False
        self._stop.clear()

        def report(message: str) -> None:
            self.last_error = message
            self.connected_since = None
            if on_error is not None:
                on_error(message)

        def run() -> None:
            self.connected_since = time.time()
            for token in self.stream.listen(stop_event=self._stop, on_error=report):
                with self._lock:
                    self._pending[token.mint] = token
                    self.received += 1
                    if len(self._pending) > self.max_pending:
                        # Aelteste zuerst verwerfen - wer nach Stunden noch
                        # wartet, ist ohnehin nicht mehr "frisch".
                        for mint in sorted(
                            self._pending, key=lambda m: self._pending[m].seen_at
                        )[: len(self._pending) - self.max_pending]:
                            del self._pending[mint]

        self._thread = threading.Thread(target=run, daemon=True, name="xeno-live")
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def take_matured(self, min_age_seconds: float, limit: int = 100) -> list[NewToken]:
        """Gibt Token heraus, die alt genug fuer eine Beurteilung sind.

        Herausgegebene Token werden entfernt - jeder wird genau einmal an die
        Pruefkette uebergeben.
        """
        now = time.time()
        with self._lock:
            ready = [
                token
                for token in self._pending.values()
                if now - token.seen_at >= min_age_seconds
            ]
            ready.sort(key=lambda t: t.seen_at)
            taken = ready[:limit]
            for token in taken:
                self._pending.pop(token.mint, None)
            return taken

    def drop_stale(self, max_age_seconds: float) -> int:
        """Entfernt Wartende, die nie geprueft wurden."""
        now = time.time()
        with self._lock:
            stale = [
                mint
                for mint, token in self._pending.items()
                if now - token.seen_at > max_age_seconds
            ]
            for mint in stale:
                del self._pending[mint]
            return len(stale)
