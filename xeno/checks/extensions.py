"""Token-2022-Extensions.

Der Grossteil neuer Solana-Token laeuft inzwischen ueber Token-2022 statt
ueber das klassische SPL-Token-Programm. Token-2022 erlaubt Erweiterungen am
Mint, die dieselbe Wirkung haben wie eine Freeze-Authority - nur werden sie
von Checkern uebersehen, die nur ``mintAuthority`` und ``freezeAuthority``
ansehen:

* ``permanentDelegate`` - eine feste Adresse darf Token aus *jeder* Wallet
  wegtransferieren oder verbrennen. Faktisch Vollzugriff auf deinen Bestand.
* ``transferHook`` - bei jedem Transfer laeuft fremder Programmcode mit, der
  den Transfer ablehnen kann. Damit laesst sich der Verkauf gezielt sperren.
* ``defaultAccountState: frozen`` - neue Token-Accounts sind eingefroren und
  muessen einzeln freigeschaltet werden.
* ``transferFeeConfig`` - Abgabe bei jedem Transfer. Solange eine
  Config-Authority existiert, kann die Gebuehr spaeter erhoeht werden.
* ``nonTransferable`` - der Token laesst sich ueberhaupt nicht uebertragen.
"""

from __future__ import annotations

from typing import Any

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "token2022"

#: Extensions ohne Risiko fuer Halter - vor allem Metadaten.
BENIGN_EXTENSIONS = frozenset(
    {
        "metadataPointer",
        "tokenMetadata",
        "groupPointer",
        "groupMemberPointer",
        "tokenGroup",
        "tokenGroupMember",
        "immutableOwner",
        "memoTransfer",
        "cpiGuard",
        "uninitialized",
    }
)


def _basis_points(state: dict[str, Any]) -> int:
    fee = state.get("newerTransferFee") or state.get("olderTransferFee") or {}
    try:
        return int(fee.get("transferFeeBasisPoints", 0) or 0)
    except (TypeError, ValueError):
        return 0


def check_extensions(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    mint_info = data.mint_info
    if mint_info is None or not mint_info.is_token_2022:
        return []

    findings: list[Finding] = []
    seen: list[str] = []

    for ext in mint_info.extensions:
        name = ext.get("extension", "") or ""
        state = ext.get("state") or {}
        seen.append(name)

        if name == "permanentDelegate":
            delegate = state.get("delegate")
            if delegate:
                findings.append(
                    finding(
                        CHECK,
                        "permanent_delegate",
                        Severity.CRITICAL,
                        "Permanent Delegate gesetzt - diese Adresse kann Token aus "
                        "jeder Wallet abziehen oder verbrennen",
                        delegate=delegate,
                    )
                )

        elif name == "transferHook":
            program_id = state.get("programId")
            if program_id:
                findings.append(
                    finding(
                        CHECK,
                        "transfer_hook",
                        Severity.CRITICAL,
                        "Transfer-Hook aktiv - fremder Code laeuft bei jedem Transfer "
                        "mit und kann Verkaeufe blockieren",
                        program_id=program_id,
                        authority=state.get("authority"),
                    )
                )

        elif name == "defaultAccountState":
            if str(state.get("accountState", "")).lower() == "frozen":
                findings.append(
                    finding(
                        CHECK,
                        "default_frozen",
                        Severity.CRITICAL,
                        "Neue Token-Accounts sind standardmaessig eingefroren",
                    )
                )

        elif name == "nonTransferable":
            findings.append(
                finding(
                    CHECK,
                    "non_transferable",
                    Severity.CRITICAL,
                    "Token ist als nicht uebertragbar markiert - Verkauf unmoeglich",
                )
            )

        elif name == "transferFeeConfig":
            bps = _basis_points(state)
            authority = state.get("transferFeeConfigAuthority")
            if bps > 0:
                severity = Severity.CRITICAL if bps >= 1000 else (
                    Severity.HIGH if bps >= 300 else Severity.MEDIUM
                )
                findings.append(
                    finding(
                        CHECK,
                        "transfer_fee",
                        severity,
                        f"Transfer-Gebuehr von {bps / 100:.2f}% bei jedem Transfer",
                        basis_points=bps,
                    )
                )
            if authority:
                findings.append(
                    finding(
                        CHECK,
                        "transfer_fee_authority",
                        Severity.HIGH,
                        "Transfer-Fee-Authority aktiv - die Gebuehr kann spaeter "
                        "erhoeht werden",
                        authority=authority,
                    )
                )

        elif name == "mintCloseAuthority":
            if state.get("closeAuthority"):
                findings.append(
                    finding(
                        CHECK,
                        "mint_close_authority",
                        Severity.MEDIUM,
                        "Mint kann geschlossen werden",
                        authority=state.get("closeAuthority"),
                    )
                )

        elif name == "interestBearingConfig":
            findings.append(
                finding(
                    CHECK,
                    "interest_bearing",
                    Severity.LOW,
                    "Interest-Bearing-Extension - angezeigte Betraege weichen vom "
                    "tatsaechlichen Bestand ab",
                )
            )

        elif name == "confidentialTransferMint":
            findings.append(
                finding(
                    CHECK,
                    "confidential_transfer",
                    Severity.LOW,
                    "Confidential Transfers aktiv - Bewegungen sind nur eingeschraenkt "
                    "nachvollziehbar",
                )
            )

        elif name and name not in BENIGN_EXTENSIONS:
            findings.append(
                finding(
                    CHECK,
                    "unknown_extension",
                    Severity.LOW,
                    f"Unbekannte Token-2022-Extension '{name}' - nicht bewertet",
                    extension=name,
                )
            )

    if not findings:
        findings.append(
            finding(
                CHECK,
                "token2022_clean",
                Severity.INFO,
                "Token-2022 ohne riskante Extensions",
                extensions=seen,
            )
        )
    return findings
