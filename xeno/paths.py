"""Wo XENO seine eigenen Dateien ablegt.

Zustand und Zugangsdaten lagen bisher im Arbeitsverzeichnis - also im
Programmordner. Das faellt in dem Moment auf die Fuesse, in dem eine neue
Version in einen neuen Ordner ausgepackt wird: die Nachverfolgung faengt bei
null an, der Schluessel muss neu eingetragen werden, und ein versehentlich
geloeschter Ordner nimmt beides mit.

Beides gehoert aber nicht zum Programm, sondern zum Benutzer. Deshalb liegt
es dort, wo das jeweilige System Benutzerdaten erwartet:

    Windows   %LOCALAPPDATA%\\XENO
    macOS     ~/Library/Application Support/XENO
    Linux     ~/.local/share/xeno   (bzw. $XDG_DATA_HOME)

Ein vorhandener Altbestand im Arbeitsverzeichnis wird beim ersten Start
uebernommen. **Kopiert, nicht verschoben** - ein Fehlschlag darf nichts
kosten, und der alte Ordner bleibt so lange gueltig, bis der Benutzer ihn
selbst loescht.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

STATE_FILE_NAME = "xeno-state.json"
ENV_FILE_NAME = ".env"


def data_dir() -> Path:
    """Verzeichnis fuer alles, was einen Neu-Download ueberleben soll."""
    override = os.environ.get("XENO_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or "~\\AppData\\Local"
        return Path(base).expanduser() / "XENO"
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/XENO").expanduser()

    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "xeno"


def legacy_path(name: str) -> Path:
    """Der alte Ort: schlicht das Arbeitsverzeichnis."""
    return Path.cwd() / name


def target_path(name: str) -> Path:
    """Wo die Datei hingehoert - ohne irgendetwas zu tun.

    Bewusst getrennt von ``adopt``: eine Anzeige wie ``xeno config`` soll
    Auskunft geben und nicht nebenbei Dateien kopieren. Sonst haette der
    Benutzer den Umzug schon hinter sich, bevor ihm jemand davon erzaehlt.
    """
    return data_dir() / name


def pending_adoption(name: str) -> Path | None:
    """Ein Altbestand, der beim naechsten Oeffnen uebernommen wuerde."""
    if target_path(name).exists():
        return None
    legacy = legacy_path(name)
    return legacy if legacy.is_file() else None


def adopt(name: str) -> tuple[Path, Path | None]:
    """Zielpfad im Benutzerverzeichnis, bei Bedarf mit uebernommenem Altbestand.

    Rueckgabe ist ``(pfad, uebernommen_von)``. Uebernommen wird nur, wenn am
    Ziel noch nichts liegt - ein bereits gepflegter Bestand wird niemals
    ueberschrieben, auch nicht von einer aelteren Datei im Programmordner.
    """
    target = target_path(name)
    legacy = pending_adoption(name)
    if legacy is None:
        return target, None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    except OSError:
        # Die Uebernahme ist Komfort, keine Voraussetzung. Schlaegt sie fehl,
        # wird der Altbestand weiterbenutzt statt der Start verhindert.
        return legacy, None
    return target, legacy


def state_path(explicit: str | Path | None = None) -> tuple[Path, Path | None]:
    """Pfad der Zustandsdatei. Ausdrueckliche Angaben gewinnen immer."""
    if explicit:
        return Path(explicit).expanduser(), None
    from_env = os.environ.get("XENO_STATE_FILE", "").strip()
    if from_env:
        return Path(from_env).expanduser(), None
    return adopt(STATE_FILE_NAME)


def env_files() -> list[Path]:
    """Zu lesende ``.env``-Dateien, naeher am Projekt zuerst.

    Die Reihenfolge entscheidet: ``load_dotenv`` ueberschreibt nichts, was
    bereits gesetzt ist. Eine Datei im Projektordner kann damit gezielt
    etwas anderes einstellen als die dauerhafte im Benutzerverzeichnis.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in (legacy_path(ENV_FILE_NAME), data_dir() / ENV_FILE_NAME):
        if not candidate.is_file():
            continue
        key = str(candidate.resolve())
        if key not in seen:
            seen.add(key)
            found.append(candidate)
    return found


def describe() -> dict[str, str]:
    """Wo was liegt - fuer ``xeno config``. Aendert nichts."""
    from_env = os.environ.get("XENO_STATE_FILE", "").strip()
    state = Path(from_env).expanduser() if from_env else target_path(STATE_FILE_NAME)

    described = {
        "Datenordner": str(data_dir()),
        "Zustand": str(state),
        "Zugangsdaten": ", ".join(str(p) for p in env_files()) or "keine gefunden",
    }

    waiting = pending_adoption(STATE_FILE_NAME) if not from_env else None
    if waiting is not None:
        described["Zu uebernehmen"] = f"{waiting} (beim naechsten Start)"

    # Zugangsdaten werden bewusst *nicht* von selbst umgezogen - eine Datei
    # mit Schluesseln ungefragt an eine zweite Stelle zu kopieren, waere ein
    # Uebergriff. Stattdessen der Hinweis, wie es dauerhaft geht.
    if legacy_path(ENV_FILE_NAME).is_file() and not (data_dir() / ENV_FILE_NAME).is_file():
        described["Hinweis"] = (
            f"Zugangsdaten liegen nur im Programmordner und gehen bei einem "
            f"Neu-Download verloren. Dauerhaft: nach {data_dir() / ENV_FILE_NAME} kopieren"
        )
    return described
