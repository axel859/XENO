"""Konfiguration: RPC-Zugang und Schwellwerte fuer Screening und Bewertung.

Werte kommen aus Umgebungsvariablen (optional aus einer .env-Datei im
Projektverzeichnis). Ohne Konfiguration laeuft alles gegen den oeffentlichen
Solana-RPC - der ist stark rate-limited und taugt nur zum Ausprobieren.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # nur fuer die Typpruefung - zur Laufzeit waere es zirkulaer
    from .profiles import Profile

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def load_dotenv(path: str | Path = ".env") -> None:
    """Liest KEY=VALUE-Zeilen in os.environ, ohne bestehende Werte zu ueberschreiben.

    Gelesen wird als ``utf-8-sig``: Windows-Werkzeuge wie Notepad oder
    ``Out-File`` setzen gern eine unsichtbare Byte-Order-Mark an den
    Dateianfang. Ohne diese Behandlung hiesse der erste Schluessel
    ``\\ufeffHELIUS_API_KEY`` statt ``HELIUS_API_KEY`` - die Datei saehe
    voellig richtig aus, der Wert waere aber wirkungslos.
    """
    p = Path(path)
    if not p.is_file():
        return
    try:
        content = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # Manche Editoren speichern als UTF-16. Lieber einen zweiten Versuch
        # als eine unerklaerlich wirkungslose Konfiguration.
        try:
            content = p.read_text(encoding="utf-16")
        except (UnicodeDecodeError, OSError):
            return

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


@dataclass(frozen=True)
class ScreenThresholds:
    """Stage-1-Filter. Rein aus Markt-Metadaten, kostet keine RPC-Calls."""

    min_liquidity_usd: float = 5_000.0
    max_liquidity_usd: float = 5_000_000.0
    min_volume_h1_usd: float = 2_000.0
    min_age_minutes: float = 3.0
    max_age_hours: float = 72.0
    min_unique_buyers_h1: int = 25
    # Verhaeltnis Kaeufe/Verkaeufe. Deutlich unter 1 heisst: es wird verteilt.
    min_buy_sell_ratio: float = 0.8
    # Volumen/Liquiditaet. Extrem hohe Werte deuten auf Wash-Trading.
    max_volume_to_liquidity: float = 60.0


@dataclass(frozen=True)
class RiskThresholds:
    """Stage-2-Grenzwerte fuer die On-Chain-Analyse."""

    # Anteil der Supply in den Top-Holdern (ohne Pools/Locker/Burn-Adressen).
    max_top10_pct: float = 30.0
    max_single_holder_pct: float = 10.0
    max_creator_pct: float = 5.0
    # Anteil der Supply, der auf verdaechtig aehnliche Wallet-Cluster entfaellt.
    max_bundle_pct: float = 20.0
    # Mindestanteil der LP-Token, der verbrannt oder gelockt sein muss.
    min_lp_locked_pct: float = 90.0
    min_holder_count: int = 100
    # Ab hier gilt Liquiditaet als tragfaehig. Bewusst getrennt vom
    # Screening-Wert: dort steuert er die Auswahl, hier die Bewertung.
    healthy_liquidity_usd: float = 10_000.0


@dataclass(frozen=True)
class Settings:
    rpc_url: str = PUBLIC_RPC
    # Requests pro Sekunde gegen den RPC. Oeffentlicher Endpunkt vertraegt ~2.
    rpc_rate_limit: float = 2.0
    http_rate_limit: float = 4.0
    request_timeout: float = 25.0
    max_retries: int = 4
    cache_dir: Path | None = None
    screen: ScreenThresholds = field(default_factory=ScreenThresholds)
    risk: RiskThresholds = field(default_factory=RiskThresholds)
    #: Aktives Suchprofil. Bestimmt Schwellwerte, Suchbreite und Reihenfolge.
    profile: "Profile | Any" = None

    @property
    def uses_public_rpc(self) -> bool:
        return self.rpc_url.rstrip("/") == PUBLIC_RPC

    @classmethod
    def from_env(cls, profile_name: str | None = None) -> "Settings":
        load_dotenv()

        # Erst hier importieren: profiles baut auf config auf.
        from .profiles import get_profile

        profile = get_profile(profile_name)
        # Das Profil liefert die Ausgangswerte, einzelne Umgebungsvariablen
        # duerfen sie weiterhin uebersteuern.
        base = profile.screen

        rpc_url = os.environ.get("XENO_RPC_URL", "").strip()
        helius_key = os.environ.get("HELIUS_API_KEY", "").strip()
        if not rpc_url and helius_key:
            rpc_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        if not rpc_url:
            rpc_url = PUBLIC_RPC

        default_rate = 2.0 if rpc_url.rstrip("/") == PUBLIC_RPC else 10.0
        cache_raw = os.environ.get("XENO_CACHE_DIR", "").strip()

        return cls(
            rpc_url=rpc_url,
            rpc_rate_limit=_env_float("XENO_RPC_RATE_LIMIT", default_rate),
            http_rate_limit=_env_float("XENO_HTTP_RATE_LIMIT", 4.0),
            request_timeout=_env_float("XENO_TIMEOUT", 25.0),
            max_retries=_env_int("XENO_MAX_RETRIES", 4),
            cache_dir=Path(cache_raw) if cache_raw else None,
            profile=profile,
            screen=ScreenThresholds(
                min_liquidity_usd=_env_float("XENO_MIN_LIQUIDITY", base.min_liquidity_usd),
                max_liquidity_usd=_env_float("XENO_MAX_LIQUIDITY", base.max_liquidity_usd),
                min_volume_h1_usd=_env_float("XENO_MIN_VOLUME_H1", base.min_volume_h1_usd),
                min_age_minutes=_env_float("XENO_MIN_AGE_MINUTES", base.min_age_minutes),
                max_age_hours=_env_float("XENO_MAX_AGE_HOURS", base.max_age_hours),
                min_unique_buyers_h1=_env_int(
                    "XENO_MIN_BUYERS_H1", base.min_unique_buyers_h1
                ),
                min_buy_sell_ratio=_env_float(
                    "XENO_MIN_BUY_SELL_RATIO", base.min_buy_sell_ratio
                ),
                max_volume_to_liquidity=_env_float(
                    "XENO_MAX_VOL_LIQ", base.max_volume_to_liquidity
                ),
            ),
            risk=RiskThresholds(
                max_top10_pct=_env_float("XENO_MAX_TOP10_PCT", 30.0),
                max_single_holder_pct=_env_float("XENO_MAX_SINGLE_HOLDER_PCT", 10.0),
                max_creator_pct=_env_float("XENO_MAX_CREATOR_PCT", 5.0),
                max_bundle_pct=_env_float("XENO_MAX_BUNDLE_PCT", 20.0),
                min_lp_locked_pct=_env_float("XENO_MIN_LP_LOCKED_PCT", 90.0),
                min_holder_count=_env_int("XENO_MIN_HOLDER_COUNT", 100),
                healthy_liquidity_usd=_env_float("XENO_HEALTHY_LIQUIDITY", 10_000.0),
            ),
        )
