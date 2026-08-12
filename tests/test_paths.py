"""Tests des Ablageorts.

Der Umzug loest ein konkretes Problem: eine neue Version wird in einen neuen
Ordner ausgepackt, und die Nachverfolgung faengt bei null an. Gleichzeitig
darf er kein neues schaffen - die gefaehrlichste Stelle ist die Uebernahme
des Altbestands, weil dort Daten ueberschrieben werden koennten.

Deshalb steht hier die Regel im Mittelpunkt: **uebernommen wird nur in ein
leeres Ziel, und kopiert statt verschoben.**
"""

from __future__ import annotations

import json
import os

import pytest

from xeno.paths import (
    ENV_FILE_NAME,
    STATE_FILE_NAME,
    adopt,
    data_dir,
    describe,
    env_files,
    state_path,
)
from xeno.watchstate import WatchState


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Ein eigener Datenordner und ein eigenes Arbeitsverzeichnis je Test."""
    store = tmp_path / "datenordner"
    work = tmp_path / "programmordner"
    work.mkdir()
    monkeypatch.setenv("XENO_DATA_DIR", str(store))
    monkeypatch.chdir(work)
    return store, work


def write_state(path, mints: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "tokens": {m: {"mint": m} for m in mints}}),
        encoding="utf-8",
    )


class TestDataDir:
    def test_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XENO_DATA_DIR", str(tmp_path / "woanders"))
        assert data_dir() == tmp_path / "woanders"

    def test_windows_uses_localappdata(self, monkeypatch):
        """Geprueft wird die Auswahlregel, nicht die Schreibweise: dieser Test
        laeuft auch auf Linux, wo der Backslash kein Trennzeichen ist."""
        monkeypatch.delenv("XENO_DATA_DIR", raising=False)
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
        result = data_dir()
        assert result.name == "XENO"
        assert str(result).startswith(r"C:\Users\test\AppData\Local")

    def test_linux_uses_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XENO_DATA_DIR", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert data_dir() == tmp_path / "xeno"

    def test_the_path_is_outside_the_program_folder(self, home):
        """Der ganze Zweck: der Ort darf nicht im Programmordner liegen."""
        store, work = home
        assert work not in data_dir().parents
        assert data_dir() != work


class TestAdoption:
    def test_without_a_legacy_file_nothing_happens(self, home):
        store, _ = home
        path, adopted = adopt(STATE_FILE_NAME)
        assert path == store / STATE_FILE_NAME
        assert adopted is None

    def test_legacy_file_is_taken_over(self, home):
        store, work = home
        write_state(work / STATE_FILE_NAME, ["alt1", "alt2"])

        path, adopted = adopt(STATE_FILE_NAME)
        assert path == store / STATE_FILE_NAME
        assert adopted == work / STATE_FILE_NAME
        assert json.loads(path.read_text())["tokens"].keys() == {"alt1", "alt2"}

    def test_the_original_stays_where_it_was(self, home):
        """Kopiert, nicht verschoben - ein Fehlschlag darf nichts kosten,
        und der alte Ordner bleibt gueltig, bis der Benutzer ihn loescht."""
        store, work = home
        legacy = work / STATE_FILE_NAME
        write_state(legacy, ["alt1"])

        adopt(STATE_FILE_NAME)
        assert legacy.is_file()

    def test_an_existing_target_is_never_overwritten(self, home):
        """Die gefaehrlichste Stelle: ein gepflegter Bestand darf nicht von
        einer aelteren Datei aus dem Programmordner ueberschrieben werden."""
        store, work = home
        write_state(store / STATE_FILE_NAME, ["neu"])
        write_state(work / STATE_FILE_NAME, ["alt"])

        path, adopted = adopt(STATE_FILE_NAME)
        assert adopted is None
        assert json.loads(path.read_text())["tokens"].keys() == {"neu"}

    def test_adoption_happens_only_once(self, home):
        store, work = home
        write_state(work / STATE_FILE_NAME, ["alt1"])

        assert adopt(STATE_FILE_NAME)[1] is not None
        assert adopt(STATE_FILE_NAME)[1] is None

    def test_a_failed_copy_falls_back_to_the_old_place(self, home, monkeypatch):
        """Die Uebernahme ist Komfort. Schlaegt sie fehl, laeuft XENO mit dem
        Altbestand weiter statt gar nicht zu starten."""
        store, work = home
        legacy = work / STATE_FILE_NAME
        write_state(legacy, ["alt1"])

        def boom(*args, **kwargs):
            raise OSError("Platte voll")

        monkeypatch.setattr("shutil.copy2", boom)
        path, adopted = adopt(STATE_FILE_NAME)
        assert path == legacy
        assert adopted is None


class TestStatePath:
    def test_explicit_path_wins(self, home, tmp_path):
        target = tmp_path / "eigene.json"
        assert state_path(target) == (target, None)

    def test_environment_variable_wins_over_the_default(self, home, tmp_path):
        os.environ["XENO_STATE_FILE"] = str(tmp_path / "aus-umgebung.json")
        try:
            assert state_path()[0] == tmp_path / "aus-umgebung.json"
        finally:
            del os.environ["XENO_STATE_FILE"]

    def test_an_explicit_path_skips_adoption(self, home, tmp_path):
        store, work = home
        write_state(work / STATE_FILE_NAME, ["alt1"])
        assert state_path(tmp_path / "egal.json")[1] is None


class TestEnvFiles:
    def test_none_found(self, home):
        assert env_files() == []

    def test_project_folder_comes_first(self, home):
        """Reihenfolge entscheidet: load_dotenv ueberschreibt nichts, was
        schon gesetzt ist - also gewinnt die zuerst gelesene Datei."""
        store, work = home
        store.mkdir(parents=True, exist_ok=True)
        (work / ENV_FILE_NAME).write_text("A=1", encoding="utf-8")
        (store / ENV_FILE_NAME).write_text("A=2", encoding="utf-8")

        assert env_files() == [work / ENV_FILE_NAME, store / ENV_FILE_NAME]

    def test_the_durable_one_is_found_alone(self, home):
        store, _ = home
        store.mkdir(parents=True, exist_ok=True)
        (store / ENV_FILE_NAME).write_text("HELIUS_API_KEY=k", encoding="utf-8")
        assert env_files() == [store / ENV_FILE_NAME]

    def test_the_project_env_still_wins(self, home, monkeypatch):
        from xeno.config import load_env_files

        store, work = home
        store.mkdir(parents=True, exist_ok=True)
        (work / ENV_FILE_NAME).write_text("HELIUS_API_KEY=projekt", encoding="utf-8")
        (store / ENV_FILE_NAME).write_text("HELIUS_API_KEY=dauerhaft", encoding="utf-8")

        monkeypatch.delenv("HELIUS_API_KEY", raising=False)
        load_env_files()
        assert os.environ["HELIUS_API_KEY"] == "projekt"


class TestWatchStateIntegration:
    def test_history_survives_a_new_program_folder(self, tmp_path, monkeypatch):
        """Der eigentliche Anwendungsfall, von Anfang bis Ende."""
        store = tmp_path / "datenordner"
        monkeypatch.setenv("XENO_DATA_DIR", str(store))

        alt = tmp_path / "XENO-alt"
        alt.mkdir()
        monkeypatch.chdir(alt)
        write_state(alt / STATE_FILE_NAME, ["gemerkt"])

        # Erster Start nach dem Umzug: uebernimmt den Altbestand.
        first = WatchState()
        assert first.adopted_from == alt / STATE_FILE_NAME
        assert first.known("gemerkt")

        # Neue Version, neuer Ordner, kein Altbestand darin.
        neu = tmp_path / "XENO-neu"
        neu.mkdir()
        monkeypatch.chdir(neu)

        second = WatchState()
        assert second.adopted_from is None
        assert second.known("gemerkt")

    def test_writes_land_in_the_durable_place(self, home):
        store, work = home
        state = WatchState()
        state.add_to_watchlist("beobachtet")
        state.save()

        assert (store / STATE_FILE_NAME).is_file()
        assert not (work / STATE_FILE_NAME).exists()


class TestDescribe:
    def test_lists_every_location(self, home):
        described = describe()
        assert set(described) == {"Datenordner", "Zustand", "Zugangsdaten"}
        assert described["Zugangsdaten"] == "keine gefunden"

    def test_it_never_moves_anything(self, home):
        """Ein Befehl, der nur anzeigt, darf nichts kopieren - sonst haette
        der Benutzer den Umzug hinter sich, bevor ihm jemand davon erzaehlt."""
        store, work = home
        write_state(work / STATE_FILE_NAME, ["alt1"])

        describe()
        assert not (store / STATE_FILE_NAME).exists()

    def test_a_waiting_legacy_file_is_announced(self, home):
        store, work = home
        write_state(work / STATE_FILE_NAME, ["alt1"])
        assert str(work / STATE_FILE_NAME) in describe()["Zu uebernehmen"]

    def test_nothing_waiting_when_already_moved(self, home):
        store, _ = home
        write_state(store / STATE_FILE_NAME, ["schon-da"])
        assert "Zu uebernehmen" not in describe()

    def test_a_project_only_env_gets_a_hint(self, home):
        """Zugangsdaten werden nicht von selbst umgezogen - eine Datei mit
        Schluesseln ungefragt zu kopieren waere ein Uebergriff. Also ein Hinweis."""
        store, work = home
        (work / ENV_FILE_NAME).write_text("HELIUS_API_KEY=k", encoding="utf-8")
        assert "Neu-Download" in describe()["Hinweis"]

    def test_no_hint_once_it_is_durable(self, home):
        store, work = home
        store.mkdir(parents=True, exist_ok=True)
        (work / ENV_FILE_NAME).write_text("HELIUS_API_KEY=k", encoding="utf-8")
        (store / ENV_FILE_NAME).write_text("HELIUS_API_KEY=k", encoding="utf-8")
        assert "Hinweis" not in describe()
