"""Gedaechtnis fuer die Herkunft von Wallets.

Die Herkunftspruefung hat eine Eigenschaft, die sie von allen anderen
unterscheidet: **ihr Ergebnis aendert sich nie.** Wer eine Wallet zuerst mit
SOL versorgt hat, ist ein Ereignis der Vergangenheit. Es kann sich nicht
anders zutragen, wenn man in fuenf Minuten noch einmal nachsieht.

Ohne dieses Gedaechtnis fragte XENO genau das trotzdem staendig neu ab. Ein
Token wird in der ersten Stunde alle fuenf Minuten geprueft, jedes Mal mit
bis zu zehn Anfragen fuer dieselben zehn Wallets - zwoelf Pruefungen, hundert
Anfragen, ein einziges Ergebnis. Beim ersten Dauerbetrieb hat das ein
Monatskontingent in wenigen Tagen aufgebraucht.

Der zweite Gewinn ist weniger offensichtlich: **Buendel benutzen ihre Wallets
wieder.** Wer heute vier Adressen fuer einen Token bestueckt, taucht mit
denselben Adressen naechste Woche beim naechsten auf. Jede erkannte Wallet
kommt dem naechsten Token also zugute.

Gespeichert wird neben dem Zustand, damit das Gedaechtnis einen Neustart und
jedes Update ueberlebt.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .sources.helius import Origin

CACHE_FILE_NAME = "wallet-origins.json"

#: Obergrenze der gemerkten Wallets. Bei zehn Wallets je Token und ein paar
#: hundert Token am Tag reicht das fuer Wochen; darueber wird das aelteste
#: Drittel verworfen.
MAX_ENTRIES = 20_000

#: Mindestabstand zwischen zwei Schreibvorgaengen. Ohne ihn schriebe XENO
#: die ganze Datei mehrmals je Minute neu.
SAVE_INTERVAL = 60.0


class OriginCache:
    """Merkt sich, woher eine Wallet ihr erstes Geld hatte."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .paths import data_dir

            path = data_dir() / CACHE_FILE_NAME
        self.path = Path(path)
        self.entries: dict[str, Origin] = {}
        #: Wann eine Wallet zuletzt gebraucht wurde - steuert das Verwerfen.
        self.seen: dict[str, float] = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        self._saved_at = 0.0
        self._lock = threading.RLock()
        self.load()

    # -- Persistenz -------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Ein kaputtes Gedaechtnis darf den Start nicht verhindern -
            # schlimmstenfalls wird einmal mehr abgefragt als noetig.
            return
        with self._lock:
            for wallet, raw in (payload.get("wallets") or {}).items():
                if not isinstance(raw, dict):
                    continue
                self.entries[wallet] = Origin(
                    wallet=wallet,
                    funder=raw.get("funder", "") or "",
                    funded_at=int(raw.get("funded_at") or 0),
                    tx_count=int(raw.get("tx_count") or 0),
                    established=bool(raw.get("established")),
                )
                self.seen[wallet] = float(raw.get("seen_at") or 0.0)

    def save(self, force: bool = False) -> None:
        """Schreibt atomar, hoechstens einmal je ``SAVE_INTERVAL``."""
        now = time.time()
        with self._lock:
            if not self._dirty:
                return
            if not force and now - self._saved_at < SAVE_INTERVAL:
                return
            self._prune()
            payload = {
                "version": 1,
                "saved_at": now,
                "wallets": {
                    wallet: {
                        "funder": origin.funder,
                        "funded_at": origin.funded_at,
                        "tx_count": origin.tx_count,
                        "established": origin.established,
                        "seen_at": self.seen.get(wallet, now),
                    }
                    for wallet, origin in self.entries.items()
                },
            }
            self._dirty = False
            self._saved_at = now

        # Das Gedaechtnis ist Komfort. Laesst es sich nicht schreiben, arbeitet
        # XENO weiter - nur eben wieder mit mehr Anfragen. Ein Schreibfehler
        # darf den Watcher deshalb nie abbrechen.
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".wallet-origins-", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            os.replace(tmp, self.path)
        except OSError:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    def _prune(self) -> None:
        """Verwirft die am laengsten ungenutzten Eintraege."""
        if len(self.entries) <= MAX_ENTRIES:
            return
        keep = sorted(self.seen.items(), key=lambda kv: kv[1], reverse=True)
        keep = keep[: int(MAX_ENTRIES * 0.7)]
        wanted = {wallet for wallet, _ in keep}
        self.entries = {w: o for w, o in self.entries.items() if w in wanted}
        self.seen = {w: t for w, t in self.seen.items() if w in wanted}

    # -- Benutzung --------------------------------------------------------

    def get(self, wallet: str) -> Origin | None:
        with self._lock:
            found = self.entries.get(wallet)
            if found is None:
                self.misses += 1
                return None
            self.hits += 1
            self.seen[wallet] = time.time()
            self._dirty = True
            return found

    def put(self, origin: Origin) -> None:
        if not origin.wallet:
            return
        with self._lock:
            self.entries[origin.wallet] = origin
            self.seen[origin.wallet] = time.time()
            self._dirty = True

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return 100.0 * self.hits / total if total else 0.0

    def __len__(self) -> int:
        return len(self.entries)
