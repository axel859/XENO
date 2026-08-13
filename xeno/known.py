"""Bekannte Solana-Adressen und Programm-IDs.

Wird gebraucht, um Holder korrekt einzuordnen: ein Liquidity-Pool oder eine
Burn-Adresse mit 60% der Supply ist voellig normal, dieselben 60% in einer
privaten Wallet sind ein Ausschlusskriterium. Ohne diese Unterscheidung
meldet jeder Konzentrations-Check Fehlalarm.

Die Listen sind bewusst erweiterbar - sie decken die gaengigen Programme ab,
sind aber nicht vollstaendig.
"""

from __future__ import annotations

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

#: Quote-Token, gegen die sinnvoll gehandelt wird.
QUOTE_MINTS = frozenset({WSOL_MINT, USDC_MINT, USDT_MINT})

#: Adressen, auf denen Token endgueltig verbrannt sind.
BURN_ADDRESSES = frozenset(
    {
        "1nc1nerator11111111111111111111111111111111",
        SYSTEM_PROGRAM,
    }
)

#: AMM- und Launchpad-Programme. Token-Accounts, die diesen Programmen
#: gehoeren, sind Pool-Vaults - keine echten Holder.
AMM_PROGRAMS: dict[str, str] = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS": "Raydium Route",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap AMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora Pools",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Orca v1",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": "Moonshot",
    "BSwp6bEBihVLdqJRKGgzjcGLHkcTuzmSo1TQkHepzH8p": "Bonkswap",
    "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP": "Penguin Swap",
}

#: Programme, die Token oder LP-Token treuhaenderisch halten (Vesting/Lock).
LOCKER_PROGRAMS: dict[str, str] = {
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m": "Streamflow",
    "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn": "Jupiter Lock",
    "2r5VekMNiWPzi1pWwvJczrdPaZnJG59u91unSrTunwJg": "Streamflow Vesting",
    "CLoCKyJ6DXBJqqu2VWx9RLbgnwwR6BMHHuyasVmfMzBh": "Clockwork Lock",
}

#: Zentrale Boersen. Grosse Bestaende dort sind kein Rug-Signal.
CEX_WALLETS: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance 2",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Coinbase",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase 2",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate.io",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Kraken",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken 2",
    "5PAhQiYdLBd6SVdjzBQDxUAEFyDdF5ExNPQfcscnPRj5": "OKX",
}


def classify_owner(owner: str | None) -> str:
    """Ordnet den Besitzer eines Token-Accounts einer bekannten Kategorie zu.

    Rueckgabe ist ein kurzes Label ("Pool: Raydium AMM v4") oder "" fuer
    unbekannte, also potenziell private Wallets.
    """
    if not owner:
        return ""
    if owner in BURN_ADDRESSES:
        return "Burn"
    if owner in AMM_PROGRAMS:
        return f"Pool: {AMM_PROGRAMS[owner]}"
    if owner in LOCKER_PROGRAMS:
        return f"Lock: {LOCKER_PROGRAMS[owner]}"
    if owner in CEX_WALLETS:
        return f"CEX: {CEX_WALLETS[owner]}"
    return ""


def is_burn_address(address: str | None) -> bool:
    return bool(address) and address in BURN_ADDRESSES


#: Base58 kennt kein 0, O, I und l - genau die Zeichen, die man beim
#: Abtippen verwechselt. Adressen mit ihnen sind sicher falsch.
BASE58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def looks_like_mint(value: str) -> bool:
    """Grobe Form einer Solana-Adresse: Base58, 32-44 Zeichen.

    Trennt in der Suche die Adresse vom Namen: eine Adresse wird direkt
    nachgeschlagen, alles andere geht durch die Textsuche.
    """
    if not 32 <= len(value or "") <= 44:
        return False
    return set(value) <= BASE58_ALPHABET
