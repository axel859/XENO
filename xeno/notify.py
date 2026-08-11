"""Benachrichtigungen.

Ein Watcher ist nur sinnvoll, wenn die Meldung dich erreicht, waehrend du
gerade nicht ins Terminal schaust - sonst koennte man genauso gut ``scan``
laufen lassen.

Wichtigste Regel hier: **ein fehlgeschlagener Versand darf den Watcher nie
abbrechen.** Wenn Telegram gerade nicht erreichbar ist, soll die Ueberwachung
weiterlaufen und der Fehler auf der Konsole landen, statt dass der Prozess
stirbt und niemand es merkt.
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .models import Finding, RiskReport, Severity, Verdict
from .net import HttpClient

TELEGRAM_API = "https://api.telegram.org"


class AlertKind(str, Enum):
    """Anlass einer Meldung."""

    NEW = "new"
    #: Neuer kritischer Befund bei einem bereits bekannten Token. Der
    #: dringendste Fall - typischerweise laeuft der Rug gerade.
    CRITICAL_CHANGE = "critical_change"
    DEGRADED = "degraded"
    IMPROVED = "improved"


_TITLES = {
    AlertKind.NEW: "Neuer Kandidat",
    AlertKind.CRITICAL_CHANGE: "WARNUNG - kritische Aenderung",
    AlertKind.DEGRADED: "Verschlechtert",
    AlertKind.IMPROVED: "Verbessert",
}

_ICONS = {
    AlertKind.NEW: "\U0001f7e2",       # gruener Kreis
    AlertKind.CRITICAL_CHANGE: "\U0001f6a8",  # Warnleuchte
    AlertKind.DEGRADED: "\U0001f534",  # roter Kreis
    AlertKind.IMPROVED: "\U0001f535",  # blauer Kreis
}


@dataclass
class Alert:
    kind: AlertKind
    report: RiskReport
    previous_verdict: Verdict | None = None
    #: Befunde, die beim letzten Durchlauf noch nicht da waren.
    new_findings: list[Finding] = field(default_factory=list)
    watchlisted: bool = False

    @property
    def title(self) -> str:
        return _TITLES[self.kind]

    @property
    def token_label(self) -> str:
        return self.report.symbol or self.report.mint[:12]

    def summary_lines(self) -> list[str]:
        """Die inhaltlichen Zeilen - von allen Kanaelen gemeinsam genutzt."""
        report = self.report
        lines: list[str] = []

        if self.previous_verdict is not None:
            lines.append(
                f"Urteil: {self.previous_verdict.value} -> {report.verdict.value} "
                f"({report.score}/100)"
            )
        else:
            lines.append(f"Urteil: {report.verdict.value} ({report.score}/100)")

        candidate = report.candidate
        if candidate:
            parts = []
            if candidate.liquidity_usd:
                parts.append(f"Liq ${candidate.liquidity_usd:,.0f}")
            if candidate.volume_h1_usd:
                parts.append(f"Vol1h ${candidate.volume_h1_usd:,.0f}")
            age = candidate.age_minutes
            if age is not None:
                parts.append(f"Alter {age / 60:.1f}h" if age >= 60 else f"Alter {age:.0f}min")
            if parts:
                lines.append(" | ".join(parts))

        distribution = report.distribution
        if distribution:
            lines.append(
                f"Top10 {distribution.top10_pct:.1f}% | "
                f"groesste {distribution.largest_pct:.1f}%"
            )

        relevant = self.new_findings or [
            f
            for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM)
            for f in report.by_severity(s)
        ]
        for f in relevant[:6]:
            lines.append(f"- [{f.severity.value}] {f.message}")

        return lines


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


class ConsoleNotifier:
    """Ausgabe im Terminal."""

    def __init__(self, stream=None, color: bool | None = None) -> None:
        from .report import use_color

        self.stream = stream or sys.stdout
        self.color = use_color(self.stream) if color is None else color

    def send(self, alert: Alert) -> None:
        colors = {
            AlertKind.NEW: "\033[92m",
            AlertKind.CRITICAL_CHANGE: "\033[91m",
            AlertKind.DEGRADED: "\033[91m",
            AlertKind.IMPROVED: "\033[96m",
        }
        stamp = time.strftime("%H:%M:%S")
        head = f"[{stamp}] {alert.title}: {alert.token_label}"
        if alert.watchlisted:
            head += "  (Watchlist)"
        if self.color:
            head = f"{colors[alert.kind]}\033[1m{head}\033[0m"

        print(head, file=self.stream)
        for line in alert.summary_lines():
            print(f"    {line}", file=self.stream)
        print(f"    {alert.report.mint}", file=self.stream)
        self.stream.flush()


class TelegramNotifier:
    """Meldung per Telegram-Bot.

    Einrichtung: bei @BotFather einen Bot anlegen, den Token setzen, dem Bot
    einmal schreiben und die Chat-ID ermitteln (``xeno telegram-setup``).
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        http: HttpClient | None = None,
        on_error=None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        # Telegram erlaubt ~30 Nachrichten/Sekunde; hier reicht deutlich weniger.
        self.http = http or HttpClient(rate_limit=1.0, max_retries=3)
        self.on_error = on_error

    @classmethod
    def from_env(cls, http: HttpClient | None = None, on_error=None) -> "TelegramNotifier | None":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return None
        return cls(token, chat_id, http=http, on_error=on_error)

    def _format(self, alert: Alert) -> str:
        esc = html.escape
        icon = _ICONS[alert.kind]
        head = f"{icon} <b>{esc(alert.title)}</b>"
        if alert.watchlisted:
            head += " (Watchlist)"

        lines = [head, f"<b>{esc(alert.token_label)}</b>"]
        lines.extend(esc(line) for line in alert.summary_lines())
        lines.append(f"<code>{esc(alert.report.mint)}</code>")
        lines.append(
            f'<a href="https://dexscreener.com/solana/{esc(alert.report.mint)}">DexScreener</a>'
            f' | <a href="https://rugcheck.xyz/tokens/{esc(alert.report.mint)}">RugCheck</a>'
        )
        return "\n".join(lines)

    def send(self, alert: Alert) -> None:
        self.send_text(self._format(alert))

    def send_text(self, text: str) -> bool:
        """Sendet Rohtext. Fehler werden gemeldet, aber nicht geworfen -
        ein Ausfall des Kanals darf die Ueberwachung nicht beenden."""
        try:
            self.http.post(
                f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            return True
        except Exception as exc:  # noqa: BLE001
            message = f"Telegram-Versand fehlgeschlagen: {exc}"
            if self.on_error:
                self.on_error(message)
            else:
                print(f"  ! {message}", file=sys.stderr)
            return False


class JsonlNotifier:
    """Schreibt jede Meldung als JSON-Zeile - zum spaeteren Auswerten."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, alert: Alert) -> None:
        record = {
            "at": time.time(),
            "kind": alert.kind.value,
            "watchlisted": alert.watchlisted,
            "previous_verdict": (
                alert.previous_verdict.value if alert.previous_verdict else None
            ),
            "new_findings": [f.to_dict() for f in alert.new_findings],
            "report": alert.report.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


class MultiNotifier:
    """Verteilt an mehrere Kanaele. Ein defekter Kanal stoppt die anderen nicht."""

    def __init__(self, notifiers: list[Notifier], on_error=None) -> None:
        self.notifiers = notifiers
        self.on_error = on_error

    def send(self, alert: Alert) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(alert)
            except Exception as exc:  # noqa: BLE001
                message = f"{type(notifier).__name__} fehlgeschlagen: {exc}"
                if self.on_error:
                    self.on_error(message)
                else:
                    print(f"  ! {message}", file=sys.stderr)


def telegram_discover_chat_id(token: str, http: HttpClient | None = None) -> list[dict]:
    """Liest die letzten Updates des Bots aus, um die Chat-ID zu finden.

    Das ist der unangenehme Teil der Telegram-Einrichtung: die Chat-ID steht
    nirgends sichtbar. Man schreibt dem Bot einmal, danach taucht sie hier auf.
    """
    client = http or HttpClient(rate_limit=1.0, max_retries=2)
    payload = client.get(f"{TELEGRAM_API}/bot{token}/getUpdates")
    chats = {}
    for update in (payload or {}).get("result") or []:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = {
                "id": chat["id"],
                "type": chat.get("type", ""),
                "name": chat.get("title")
                or " ".join(
                    part
                    for part in (chat.get("first_name"), chat.get("last_name"))
                    if part
                )
                or chat.get("username", ""),
            }
    return list(chats.values())
