"""Tests fuer Systemmeldungen.

Es wird kein echter Prozess gestartet - geprueft wird, welches Kommando
zusammengebaut wuerde. Damit laufen die Tests auf jeder Plattform, auch dort,
wo weder notify-send noch osascript existiert.
"""

from __future__ import annotations

import time

import pytest
from conftest import MINT

from xeno.desktop import LOUD_KINDS, DesktopNotifier
from xeno.models import Finding, RiskReport, Severity
from xeno.notify import Alert, AlertKind


@pytest.fixture
def calls(monkeypatch):
    """Faengt alle Prozessstarts ab."""
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        "xeno.desktop._run_detached", lambda cmd: recorded.append(list(cmd)) or True
    )
    return recorded


def make_alert(kind: AlertKind = AlertKind.NEW, symbol: str = "TESTCOIN") -> Alert:
    report = RiskReport(mint=MINT, symbol=symbol)
    report.findings = [
        Finding(check="t", code="x", severity=Severity.HIGH, message="etwas gefunden")
    ]
    return Alert(kind=kind, report=report)


class TestCommandBuilding:
    def test_linux_uses_notify_send(self):
        cmd = DesktopNotifier(platform="linux")._notify_command("Titel", "Text", urgent=False)
        assert cmd[0] == "notify-send"
        assert "Titel" in cmd and "Text" in cmd

    def test_linux_marks_urgent(self):
        cmd = DesktopNotifier(platform="linux")._notify_command("T", "B", urgent=True)
        assert "--urgency=critical" in cmd

    def test_macos_uses_osascript(self):
        cmd = DesktopNotifier(platform="darwin")._notify_command("Titel", "Text", urgent=False)
        assert cmd[0] == "osascript"
        assert "display notification" in cmd[-1]

    def test_macos_adds_sound_only_when_urgent(self):
        loud = DesktopNotifier(platform="darwin")._notify_command("T", "B", urgent=True)
        quiet = DesktopNotifier(platform="darwin")._notify_command("T", "B", urgent=False)
        assert "sound name" in loud[-1]
        assert "sound name" not in quiet[-1]

    def test_windows_uses_powershell(self):
        cmd = DesktopNotifier(platform="win32")._notify_command("Titel", "Text", urgent=False)
        assert cmd[0] == "powershell"
        assert "NotifyIcon" in cmd[-1]

    def test_unsupported_platform_yields_nothing(self):
        assert DesktopNotifier(platform="haiku")._notify_command("T", "B", False) is None


class TestEscaping:
    def test_applescript_quotes_are_escaped(self):
        """Sonst bricht der Text aus dem String aus und das Skript scheitert."""
        cmd = DesktopNotifier(platform="darwin")._notify_command('a"b', "c", urgent=False)
        assert '\\"' in cmd[-1]

    def test_applescript_backslashes_are_escaped(self):
        cmd = DesktopNotifier(platform="darwin")._notify_command("a", "C:\\x", urgent=False)
        assert "\\\\x" in cmd[-1]

    def test_powershell_single_quotes_are_doubled(self):
        cmd = DesktopNotifier(platform="win32")._notify_command("O'Brien", "x", urgent=False)
        assert "O''Brien" in cmd[-1]


class TestThrottling:
    def test_second_notification_is_suppressed(self, calls):
        """Ohne Drosselung knallen bei einem Durchlauf acht Fenster auf einmal."""
        notifier = DesktopNotifier(platform="linux", sound=False)
        assert notifier.notify("a", "b") is True
        assert notifier.notify("c", "d") is False
        assert len(calls) == 1

    def test_notification_passes_after_the_gap(self, calls, monkeypatch):
        notifier = DesktopNotifier(platform="linux", sound=False)
        notifier.notify("a", "b")
        monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + 3600)
        assert notifier.notify("c", "d") is True


class TestFiltering:
    def test_important_kinds_are_loud(self):
        assert AlertKind.CRITICAL_CHANGE in LOUD_KINDS
        assert AlertKind.DEGRADED in LOUD_KINDS
        assert AlertKind.NEW not in LOUD_KINDS

    def test_only_important_skips_new_candidates(self, calls):
        notifier = DesktopNotifier(platform="linux", only_important=True)
        notifier.send(make_alert(AlertKind.NEW))
        assert calls == []

    def test_only_important_still_reports_degradation(self, calls):
        notifier = DesktopNotifier(platform="linux", only_important=True)
        notifier.send(make_alert(AlertKind.DEGRADED))
        assert len(calls) == 1

    def test_default_reports_everything(self, calls):
        notifier = DesktopNotifier(platform="linux", sound=False)
        notifier.send(make_alert(AlertKind.NEW))
        assert len(calls) == 1

    def test_token_name_appears_in_the_body(self, calls):
        DesktopNotifier(platform="linux", sound=False).send(make_alert(symbol="MEINCOIN"))
        assert any("MEINCOIN" in part for part in calls[0])


class TestRobustness:
    def test_failure_does_not_raise(self, monkeypatch):
        """Ein fehlendes notify-send darf den Watcher nicht beenden."""
        monkeypatch.setattr("xeno.desktop._run_detached", lambda cmd: False)
        errors: list[str] = []
        notifier = DesktopNotifier(platform="linux", on_error=errors.append)
        assert notifier.notify("a", "b") is False
        assert errors

    def test_warning_is_only_shown_once(self, monkeypatch):
        """Sonst steht die Meldung bei jedem Durchlauf erneut im Log."""
        monkeypatch.setattr("xeno.desktop._run_detached", lambda cmd: False)
        monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() * 1e6)
        errors: list[str] = []
        notifier = DesktopNotifier(platform="linux", on_error=errors.append)
        for _ in range(5):
            notifier.notify("a", "b")
        assert len(errors) == 1

    def test_unsupported_platform_is_reported_not_raised(self):
        errors: list[str] = []
        notifier = DesktopNotifier(platform="haiku", on_error=errors.append)
        assert notifier.notify("a", "b") is False
        assert errors and "haiku" in errors[0]

    def test_availability_is_false_without_the_tool(self, monkeypatch):
        monkeypatch.setattr("xeno.desktop.shutil.which", lambda name: None)
        assert DesktopNotifier(platform="linux").available is False

    def test_availability_is_true_with_the_tool(self, monkeypatch):
        monkeypatch.setattr("xeno.desktop.shutil.which", lambda name: "/usr/bin/" + name)
        assert DesktopNotifier(platform="linux").available is True

    def test_sound_falls_back_to_terminal_bell(self, monkeypatch, capsys):
        """Ohne Abspieler bleibt wenigstens die Terminal-Glocke."""
        monkeypatch.setattr("xeno.desktop.shutil.which", lambda name: None)
        DesktopNotifier(platform="linux").play_sound()
        assert "\a" in capsys.readouterr().out
