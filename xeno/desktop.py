"""Systemmeldungen und Signalton.

Meldet ueber die Bordmittel des Betriebssystems - ohne Fremdbibliothek, ohne
Konto, ohne fremden Dienst. Passt zum Betrieb am eigenen Rechner: solange der
Bot laeuft, sitzt man meistens ohnehin davor, und eine Systemmeldung erreicht
einen auch dann, wenn das Browserfenster hinten liegt.

Aufrufe laufen bewusst als abgekoppelter Prozess ohne Warten. Ein haengender
``notify-send`` darf den Watcher nicht aufhalten, und ein fehlendes Programm
darf ihn nicht beenden - im Zweifel bleibt der Terminal-Piepton als Rueckfall.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Sequence

from .notify import Alert, AlertKind

#: Anlaesse, die einen Ton wert sind - hier geht es um Geld, das gerade
#: verloren geht. Neue Kandidaten melden sich still.
LOUD_KINDS = frozenset({AlertKind.CRITICAL_CHANGE, AlertKind.DEGRADED})

#: Mindestabstand zwischen zwei Systemmeldungen. Ohne Drosselung knallen bei
#: einem Durchlauf acht Meldungen gleichzeitig auf den Bildschirm.
MIN_GAP_SECONDS = 3.0


def _run_detached(command: Sequence[str]) -> bool:
    """Startet ein Kommando, ohne auf sein Ende zu warten."""
    try:
        subprocess.Popen(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (OSError, ValueError):
        return False


def _applescript_escape(text: str) -> str:
    """Maskiert Anfuehrungszeichen und Backslashes fuer AppleScript."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _powershell_escape(text: str) -> str:
    """In einfache Anfuehrungszeichen gefasste PowerShell-Strings verdoppeln '."""
    return text.replace("'", "''")


class DesktopNotifier:
    """Systemmeldung plus optionalem Ton.

    ``only_important`` beschraenkt die Meldungen auf Verschlechterungen und
    neue kritische Befunde - also auf das, was bei einem gehaltenen Token
    sofort zaehlt.
    """

    def __init__(
        self,
        sound: bool = True,
        only_important: bool = False,
        platform: str | None = None,
        on_error=None,
    ) -> None:
        self.sound = sound
        self.only_important = only_important
        self.platform = platform or sys.platform
        self.on_error = on_error
        self._lock = threading.Lock()
        self._last_sent = 0.0
        self._warned = False

    # -- Verfuegbarkeit ---------------------------------------------------

    @property
    def available(self) -> bool:
        if self.platform.startswith("linux"):
            return shutil.which("notify-send") is not None
        if self.platform == "darwin":
            return shutil.which("osascript") is not None
        if self.platform.startswith("win"):
            return shutil.which("powershell") is not None
        return False

    def _warn_once(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        if self.on_error:
            self.on_error(message)
        else:
            print(f"  ! {message}", file=sys.stderr)

    # -- Bausteine --------------------------------------------------------

    def _notify_command(self, title: str, body: str, urgent: bool) -> list[str] | None:
        if self.platform.startswith("linux"):
            return [
                "notify-send",
                "--app-name=XENO",
                f"--urgency={'critical' if urgent else 'normal'}",
                title,
                body,
            ]

        if self.platform == "darwin":
            script = (
                f'display notification "{_applescript_escape(body)}" '
                f'with title "XENO" subtitle "{_applescript_escape(title)}"'
            )
            if self.sound and urgent:
                script += ' sound name "Basso"'
            return ["osascript", "-e", script]

        if self.platform.startswith("win"):
            # NotifyIcon-Sprechblase: laeuft auf Windows 10 und 11 ohne
            # Zusatzmodul und erscheint dort als normale Benachrichtigung.
            icon = "Warning" if urgent else "Info"
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                f"$n.BalloonTipIcon='{icon}';"
                f"$n.BalloonTipTitle='{_powershell_escape(title)}';"
                f"$n.BalloonTipText='{_powershell_escape(body)}';"
                "$n.Visible=$true;$n.ShowBalloonTip(8000);"
                "Start-Sleep -Seconds 8;$n.Dispose()"
            )
            return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]

        return None

    def _sound_command(self) -> list[str] | None:
        if self.platform.startswith("linux"):
            for player, path in (
                ("paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"),
                ("aplay", "/usr/share/sounds/sound-icons/prompt.wav"),
            ):
                if shutil.which(player):
                    return [player, path]
            return None

        if self.platform == "darwin":
            # Der Ton haengt unter macOS schon an der Meldung selbst.
            return None

        if self.platform.startswith("win"):
            return [
                "powershell",
                "-NoProfile",
                "-Command",
                "[System.Media.SystemSounds]::Exclamation.Play()",
            ]

        return None

    def _beep(self) -> None:
        """Terminal-Glocke als Rueckfall, wenn kein Abspieler da ist."""
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def play_sound(self) -> None:
        command = self._sound_command()
        if command is None or not _run_detached(command):
            self._beep()

    # -- Schnittstelle ----------------------------------------------------

    def notify(self, title: str, body: str, urgent: bool = False) -> bool:
        """Zeigt eine Systemmeldung. Gibt zurueck, ob sie abgesetzt wurde."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_sent < MIN_GAP_SECONDS:
                return False
            self._last_sent = now

        command = self._notify_command(title, body, urgent)
        if command is None:
            self._warn_once(
                f"Systemmeldungen werden auf '{self.platform}' nicht unterstuetzt"
            )
            return False

        if not _run_detached(command):
            self._warn_once(
                "Systemmeldung fehlgeschlagen "
                + (
                    "- unter Linux hilft: sudo apt install libnotify-bin"
                    if self.platform.startswith("linux")
                    else ""
                )
            )
            return False

        if self.sound and urgent and self.platform != "darwin":
            self.play_sound()
        return True

    def send(self, alert: Alert) -> None:
        """Notifier-Schnittstelle - wird vom Watcher aufgerufen."""
        urgent = alert.kind in LOUD_KINDS
        if self.only_important and not urgent:
            return

        lines = alert.summary_lines()
        body = f"{alert.token_label} - {lines[0] if lines else ''}"
        if len(lines) > 2:
            body += f"\n{lines[-1]}"
        self.notify(alert.title, body, urgent=urgent)
