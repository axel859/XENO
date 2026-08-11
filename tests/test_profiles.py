"""Tests fuer die Suchprofile.

Hintergrund: mit den urspruenglichen Schwellwerten kam von 200 frisch
erstellten Pools genau einer durch - nicht weil die Token schlecht waren,
sondern weil fuer ihr Alter unerreichbare Groessen verlangt wurden. Die Tests
halten fest, dass das Profil "early" genau diese Token findet und die
Reihenfolge nicht wieder auf Liquiditaet zurueckfaellt.
"""

from __future__ import annotations

from conftest import make_candidate

from xeno.config import Settings
from xeno.models import TokenCandidate
from xeno.pipeline import rank_key
from xeno.profiles import BALANCED, EARLY, ESTABLISHED, PROFILES, get_profile
from xeno.screen import screen


def fresh(**kwargs) -> TokenCandidate:
    """Ein typischer frischer Pool: wenige Minuten alt, kleine Betraege."""
    defaults = dict(
        age_minutes=4.0,
        liquidity_usd=1_800.0,
        volume_h1_usd=900.0,
        buyers_h1=12,
        sellers_h1=3,
        buys_h1=30,
        sells_h1=6,
    )
    defaults.update(kwargs)
    return make_candidate(**defaults)


class TestProfileSelection:
    def test_default_is_early(self, monkeypatch):
        monkeypatch.delenv("XENO_PROFILE", raising=False)
        assert get_profile().name == "early"

    def test_name_wins_over_environment(self, monkeypatch):
        monkeypatch.setenv("XENO_PROFILE", "established")
        assert get_profile("balanced").name == "balanced"

    def test_environment_is_used_without_a_name(self, monkeypatch):
        monkeypatch.setenv("XENO_PROFILE", "established")
        assert get_profile().name == "established"

    def test_unknown_name_falls_back(self, monkeypatch):
        monkeypatch.delenv("XENO_PROFILE", raising=False)
        assert get_profile("gibtsnicht").name == "early"

    def test_case_and_spaces_are_tolerated(self, monkeypatch):
        monkeypatch.delenv("XENO_PROFILE", raising=False)
        assert get_profile("  BALANCED ").name == "balanced"


class TestEarlyProfile:
    def test_finds_a_typical_fresh_pool(self):
        """Der Kern der Sache - genau diese Token sollen durchkommen."""
        assert screen(fresh(), EARLY.screen).passed

    def test_old_defaults_would_have_rejected_it(self):
        """Beleg fuer die Ursache: dasselbe Token faellt bei 'balanced' durch."""
        result = screen(fresh(), BALANCED.screen)
        assert not result.passed
        assert any("Liquiditaet" in r or "Kaeufer" in r for r in result.reasons)

    def test_rejects_pools_without_participation(self):
        """Ein frischer Pool ohne Kaeufer ist kein Fruehsignal, nur leer."""
        assert not screen(fresh(buyers_h1=1), EARLY.screen).passed

    def test_rejects_selling_pressure(self):
        assert not screen(fresh(buys_h1=5, sells_h1=20), EARLY.screen).passed

    def test_rejects_the_first_seconds(self):
        """Unter zwei Minuten liegen schlicht noch keine Daten vor."""
        assert not screen(fresh(age_minutes=0.5), EARLY.screen).passed

    def test_rejects_tokens_past_the_early_window(self):
        assert not screen(fresh(age_minutes=20 * 60), EARLY.screen).passed

    def test_skips_trending_as_a_source(self):
        """Was im Trending steht, laeuft bereits - zu spaet fuer 'frueh'."""
        assert EARLY.include_trending is False
        assert EARLY.include_new is True

    def test_searches_broadly(self):
        assert EARLY.pages >= 10


class TestRanking:
    def test_early_ranks_by_participation(self):
        quiet_but_rich = fresh(liquidity_usd=100_000.0, buyers_h1=5, buys_h1=5, sells_h1=5)
        busy_but_small = fresh(liquidity_usd=2_000.0, buyers_h1=90, buys_h1=180, sells_h1=40)
        assert rank_key(busy_but_small, EARLY) > rank_key(quiet_but_rich, EARLY)

    def test_other_profiles_rank_by_size(self):
        quiet_but_rich = fresh(liquidity_usd=100_000.0, buyers_h1=5)
        busy_but_small = fresh(liquidity_usd=2_000.0, buyers_h1=90)
        assert rank_key(quiet_but_rich, BALANCED) > rank_key(busy_but_small, BALANCED)

    def test_without_a_profile_size_decides(self):
        assert rank_key(fresh(liquidity_usd=5_000.0), None) == 5_000.0


class TestTraction:
    def test_no_buyers_means_no_signal(self):
        assert fresh(buyers_h1=0).traction == 0.0

    def test_more_buyers_score_higher(self):
        assert fresh(buyers_h1=50).traction > fresh(buyers_h1=10).traction

    def test_buy_pressure_counts(self):
        pushing = fresh(buyers_h1=20, buys_h1=100, sells_h1=10)
        stalling = fresh(buyers_h1=20, buys_h1=30, sells_h1=30)
        assert pushing.traction > stalling.traction

    def test_extreme_ratios_are_capped(self):
        """40:1 heisst meist nur, dass noch niemand verkauft hat - das ist
        kein vierzigfach besseres Signal."""
        extreme = fresh(buyers_h1=10, buys_h1=400, sells_h1=1)
        strong = fresh(buyers_h1=10, buys_h1=50, sells_h1=10)
        assert extreme.traction <= strong.traction * 1.01

    def test_no_sales_at_all_is_handled(self):
        assert fresh(buyers_h1=10, buys_h1=10, sells_h1=0).traction > 0


class TestSettingsIntegration:
    def test_profile_thresholds_are_applied(self, monkeypatch):
        monkeypatch.delenv("XENO_MIN_LIQUIDITY", raising=False)
        settings = Settings.from_env("early")
        assert settings.screen.min_liquidity_usd == EARLY.screen.min_liquidity_usd
        assert settings.profile.name == "early"

    def test_environment_can_override_a_single_value(self, monkeypatch):
        monkeypatch.setenv("XENO_MIN_LIQUIDITY", "9999")
        settings = Settings.from_env("early")
        assert settings.screen.min_liquidity_usd == 9999.0
        # Der Rest bleibt beim Profil.
        assert settings.screen.min_unique_buyers_h1 == EARLY.screen.min_unique_buyers_h1

    def test_every_profile_is_usable(self):
        for name in PROFILES:
            settings = Settings.from_env(name)
            assert settings.profile.name == name
            assert settings.screen.min_liquidity_usd >= 0

    def test_established_is_stricter_than_early(self):
        assert (
            ESTABLISHED.screen.min_liquidity_usd > EARLY.screen.min_liquidity_usd
        )
        assert ESTABLISHED.screen.max_age_hours > EARLY.screen.max_age_hours


class TestDotenvRobustness:
    """Die .env wird unter Windows haeufig mit Notepad oder Out-File angelegt.
    Beide setzen unsichtbare Zeichen an den Dateianfang - ohne Behandlung
    heisst der erste Schluessel dann nicht so, wie er aussieht."""

    def _load(self, tmp_path, monkeypatch, data: bytes):
        env = tmp_path / ".env"
        env.write_bytes(data)
        monkeypatch.delenv("HELIUS_API_KEY", raising=False)
        from xeno.config import load_dotenv

        load_dotenv(env)
        import os

        return os.environ.get("HELIUS_API_KEY")

    def test_plain_utf8(self, tmp_path, monkeypatch):
        assert self._load(tmp_path, monkeypatch, b"HELIUS_API_KEY=abc123\n") == "abc123"

    def test_utf8_with_bom(self, tmp_path, monkeypatch):
        """Genau der Windows-Fall: sieht richtig aus, waere aber wirkungslos."""
        assert (
            self._load(tmp_path, monkeypatch, b"\xef\xbb\xbfHELIUS_API_KEY=abc123\n")
            == "abc123"
        )

    def test_utf16(self, tmp_path, monkeypatch):
        data = "HELIUS_API_KEY=abc123\n".encode("utf-16")
        assert self._load(tmp_path, monkeypatch, data) == "abc123"

    def test_windows_line_endings(self, tmp_path, monkeypatch):
        assert self._load(tmp_path, monkeypatch, b"HELIUS_API_KEY=abc123\r\n") == "abc123"

    def test_quotes_are_stripped(self, tmp_path, monkeypatch):
        assert self._load(tmp_path, monkeypatch, b'HELIUS_API_KEY="abc123"\n') == "abc123"

    def test_existing_environment_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIUS_API_KEY", "aus-der-umgebung")
        env = tmp_path / ".env"
        env.write_bytes(b"HELIUS_API_KEY=aus-der-datei\n")
        from xeno.config import load_dotenv

        load_dotenv(env)
        import os

        assert os.environ["HELIUS_API_KEY"] == "aus-der-umgebung"

    def test_missing_file_is_fine(self, tmp_path):
        from xeno.config import load_dotenv

        load_dotenv(tmp_path / "gibtsnicht")
