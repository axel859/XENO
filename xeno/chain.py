"""Rohdaten von der Chain in auswertbare Strukturen ueberfuehren."""

from __future__ import annotations

from typing import Any

from .known import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, classify_owner
from .models import Holder, HolderDistribution, MintInfo
from .net import SolanaRpc


def parse_mint_account(mint: str, account: dict[str, Any] | None) -> MintInfo | None:
    """Wandelt eine ``getAccountInfo``-Antwort (jsonParsed) in ``MintInfo``.

    Gibt None zurueck, wenn der Account nicht existiert oder keinem der
    beiden Token-Programme gehoert - dann ist die Adresse kein Mint.
    """
    if not account:
        return None
    program = account.get("owner", "")
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return None

    parsed = ((account.get("data") or {}).get("parsed") or {})
    if parsed.get("type") not in (None, "mint"):
        return None
    info = parsed.get("info") or {}

    extensions = info.get("extensions") or []
    name = symbol = ""
    for ext in extensions:
        if ext.get("extension") == "tokenMetadata":
            state = ext.get("state") or {}
            name = state.get("name", "") or ""
            symbol = state.get("symbol", "") or ""
            break

    try:
        supply_raw = int(info.get("supply", 0))
    except (TypeError, ValueError):
        supply_raw = 0

    return MintInfo(
        mint=mint,
        program=program,
        decimals=int(info.get("decimals", 0) or 0),
        supply_raw=supply_raw,
        mint_authority=info.get("mintAuthority") or None,
        freeze_authority=info.get("freezeAuthority") or None,
        extensions=extensions,
        name=name,
        symbol=symbol,
    )


def build_distribution(
    largest: list[dict[str, Any]],
    owners: list[str | None],
    total_supply: float,
    extra_pool_addresses: set[str] | None = None,
    holder_count: int | None = None,
) -> HolderDistribution:
    """Baut die Holder-Verteilung und markiert Pools, Burns und Locker.

    Der Ausschluss ist der entscheidende Teil: ein Liquidity-Pool haelt bei
    frischen Token regelmaessig 90%+ der Supply. Ohne Markierung wuerde jeder
    normale Token als extrem konzentriert gelten.
    """
    extra = extra_pool_addresses or set()
    holders: list[Holder] = []

    for entry, owner in zip(largest, owners + [None] * (len(largest) - len(owners))):
        try:
            amount_raw = int(entry.get("amount", 0))
        except (TypeError, ValueError):
            amount_raw = 0
        ui_amount = entry.get("uiAmount")
        if ui_amount is None:
            ui_amount = float(entry.get("uiAmountString", 0) or 0)

        token_account = entry.get("address", "") or ""
        tag = classify_owner(owner)
        if not tag and (token_account in extra or (owner and owner in extra)):
            tag = "Pool"

        holders.append(
            Holder(
                token_account=token_account,
                amount_raw=amount_raw,
                ui_amount=float(ui_amount),
                owner=owner,
                pct=(float(ui_amount) / total_supply * 100.0) if total_supply > 0 else 0.0,
                tag=tag,
            )
        )

    holders.sort(key=lambda h: h.pct, reverse=True)
    real = [h for h in holders if not h.is_excluded]

    return HolderDistribution(
        holders=holders,
        total_supply=total_supply,
        top10_pct=sum(h.pct for h in real[:10]),
        top20_pct=sum(h.pct for h in real[:20]),
        largest_pct=real[0].pct if real else 0.0,
        excluded_pct=sum(h.pct for h in holders if h.is_excluded),
        holder_count=holder_count,
        truncated=True,  # getTokenLargestAccounts liefert maximal 20 Eintraege
    )


def fetch_distribution_via_rpc(
    rpc: SolanaRpc,
    mint: str,
    total_supply: float,
    extra_pool_addresses: set[str] | None = None,
) -> HolderDistribution | None:
    """Holt die Top-Holder ueber den RPC.

    Gibt None zurueck, wenn der Endpunkt die Methode nicht bedient - der
    oeffentliche Solana-RPC sperrt ``getTokenLargestAccounts`` grundsaetzlich.
    Der Aufrufer weicht dann auf RugCheck aus.
    """
    try:
        largest = rpc.get_token_largest_accounts(mint)
    except Exception:  # noqa: BLE001 - jeder Fehler bedeutet hier: keine Holder-Daten
        return None
    if not largest:
        return None

    addresses = [entry.get("address", "") for entry in largest if entry.get("address")]
    owners: list[str | None] = []
    try:
        for account in rpc.get_multiple_accounts(addresses):
            info = ((account or {}).get("data") or {}).get("parsed", {}).get("info", {})
            owners.append(info.get("owner"))
    except Exception:  # noqa: BLE001
        owners = [None] * len(addresses)

    return build_distribution(
        largest, owners, total_supply, extra_pool_addresses=extra_pool_addresses
    )


def distribution_from_rugcheck(
    top_holders: list[Any],
    total_supply: float,
    pool_addresses: set[str] | None = None,
    holder_count: int | None = None,
) -> HolderDistribution:
    """Baut dieselbe Struktur aus RugCheck-Daten.

    RugCheck liefert die Prozente bereits fertig, aber ohne Pool-Ausschluss -
    den machen wir hier selbst, sonst ist jeder frische Token "konzentriert".
    """
    pools = pool_addresses or set()
    holders: list[Holder] = []

    for entry in top_holders:
        owner = entry.owner or ""
        tag = classify_owner(owner)
        if not tag and (owner in pools or entry.address in pools):
            tag = "Pool"
        holders.append(
            Holder(
                token_account=entry.address,
                amount_raw=0,
                ui_amount=entry.ui_amount,
                owner=owner or None,
                pct=entry.pct,
                tag=tag,
            )
        )

    holders.sort(key=lambda h: h.pct, reverse=True)
    real = [h for h in holders if not h.is_excluded]

    return HolderDistribution(
        holders=holders,
        total_supply=total_supply,
        top10_pct=sum(h.pct for h in real[:10]),
        top20_pct=sum(h.pct for h in real[:20]),
        largest_pct=real[0].pct if real else 0.0,
        excluded_pct=sum(h.pct for h in holders if h.is_excluded),
        holder_count=holder_count,
        truncated=True,
    )
