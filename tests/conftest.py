"""Testbausteine.

Alle Tests laufen ohne Netzwerk. Die Pruefungen in ``xeno/checks`` sind
reine Funktionen ueber ``TokenData``, deshalb reicht es, diese Struktur mit
realistischen Werten zu fuellen.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from xeno.checks.base import TokenData
from xeno.config import RiskThresholds, ScreenThresholds
from xeno.known import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from xeno.models import Holder, HolderDistribution, MintInfo, TokenCandidate
from xeno.sources.rugcheck import RugCheckReport

MINT = "25a7whvEzPqceUt5vVSfxVbqzjJrg4oEqbCHvAsupump"

#: Umgebungsvariablen, die das Verhalten von XENO steuern.
_ENV_KEYS = (
    "HELIUS_API_KEY",
    "XENO_RPC_URL",
    "XENO_STATE_FILE",
    "XENO_WEB_TOKEN",
    "XENO_PROFILE",
)


@pytest.fixture(autouse=True)
def isolated_environment():
    """Trennt jeden Test von der Umgebung - davor und danach.

    Zwei Wege fuehren sonst zu Tests, die je nach Rechner anders ausgehen:
    ein gesetzter Schluessel auf dem Entwicklungsrechner, und ``load_dotenv``,
    das beim Pruefen des Einlesens direkt in ``os.environ`` schreibt. Letzteres
    entzieht sich ``monkeypatch``, weil die Variable vorher gar nicht existierte -
    sie bleibt danach stehen und versetzt spaetere Tests still in einen
    anderen Zustand.
    """
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def risk() -> RiskThresholds:
    return RiskThresholds()


@pytest.fixture
def screen_thresholds() -> ScreenThresholds:
    return ScreenThresholds()


def make_mint_info(
    program: str = TOKEN_PROGRAM,
    mint_authority: str | None = None,
    freeze_authority: str | None = None,
    extensions: list[dict] | None = None,
) -> MintInfo:
    return MintInfo(
        mint=MINT,
        program=program,
        decimals=6,
        supply_raw=1_000_000_000_000_000,
        mint_authority=mint_authority,
        freeze_authority=freeze_authority,
        extensions=extensions or [],
    )


def make_distribution(
    percentages: list[float],
    tags: list[str] | None = None,
    holder_count: int | None = 500,
) -> HolderDistribution:
    """Baut eine Verteilung aus Prozentwerten; ``tags`` markiert Pools/Burns."""
    tags = tags or [""] * len(percentages)
    holders = [
        Holder(
            token_account=f"acc{i}",
            amount_raw=0,
            ui_amount=pct * 10_000_000,
            owner=f"owner{i}",
            pct=pct,
            tag=tag,
        )
        for i, (pct, tag) in enumerate(zip(percentages, tags))
    ]
    real = [h for h in holders if not h.is_excluded]
    return HolderDistribution(
        holders=holders,
        total_supply=1_000_000_000.0,
        top10_pct=sum(h.pct for h in real[:10]),
        top20_pct=sum(h.pct for h in real[:20]),
        largest_pct=real[0].pct if real else 0.0,
        excluded_pct=sum(h.pct for h in holders if h.is_excluded),
        holder_count=holder_count,
        truncated=True,
    )


def make_candidate(age_minutes: float = 120.0, **kwargs) -> TokenCandidate:
    defaults = dict(
        mint=MINT,
        symbol="TEST",
        liquidity_usd=50_000.0,
        volume_h1_usd=25_000.0,
        buys_h1=100,
        sells_h1=80,
        buyers_h1=60,
        sellers_h1=40,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    defaults.update(kwargs)
    return TokenCandidate(**defaults)


def make_data(**kwargs) -> TokenData:
    defaults = dict(
        mint=MINT,
        mint_info=make_mint_info(),
        rugcheck=RugCheckReport(mint=MINT, raw={}),
        distribution=make_distribution([5.0, 3.0, 2.0]),
        holder_source="rugcheck",
    )
    defaults.update(kwargs)
    return TokenData(**defaults)


def rugcheck(**raw) -> RugCheckReport:
    return RugCheckReport(mint=MINT, raw=raw)
