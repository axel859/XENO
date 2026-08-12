"""Datentypen der Analyse-Pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Gewicht eines einzelnen Befunds."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Punktabzug pro Befund. CRITICAL zieht allein schon unter jede Schwelle.
SEVERITY_PENALTY: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 5,
    Severity.MEDIUM: 15,
    Severity.HIGH: 30,
    Severity.CRITICAL: 100,
}


#: Unter so vielen Trades laesst sich kein Muster ablesen - drei Trades von
#: einer Wallet sind kein Bot, sondern schlicht wenig los.
MIN_TRADES_FOR_PATTERN = 10


class Verdict(str, Enum):
    AVOID = "AVOID"
    RISKY = "RISKY"
    CAUTION = "CAUTION"
    OK = "OK"
    UNKNOWN = "UNKNOWN"


#: Befund-Codes, die eine Wissensluecke markieren statt eines Risikos.
#: Solange einer davon vorliegt, kann das Gesamturteil nicht "OK" lauten.
GAP_CODES = frozenset(
    {
        "authority_unknown",
        "holders_unavailable",
        "lp_status_unknown",
        "bundling_unchecked",
        "round_trip_unreliable",
        "sell_route_missing_young",
        # "distribution_truncated" gehoert bewusst NICHT hierher: der Hinweis
        # erscheint bei jeder erfolgreichen Holder-Analyse, weil der RPC nie
        # mehr als die groessten Accounts liefert. Als Luecke gewertet waere
        # "OK" nie erreichbar und das Urteil damit nutzlos.
    }
)


@dataclass
class Finding:
    """Ein einzelnes Analyse-Ergebnis.

    ``check`` ist die Herkunft (z.B. "authority"), ``code`` der maschinenlesbare
    Bezeichner (z.B. "mint_authority_active"), ``message`` der Klartext.
    """

    check: str
    code: str
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def penalty(self) -> int:
        return SEVERITY_PENALTY[self.severity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class TokenCandidate:
    """Ein Kandidat aus der Discovery-Phase - reine Marktdaten, kein On-Chain."""

    mint: str
    symbol: str = ""
    name: str = ""
    pool_address: str = ""
    dex: str = ""
    source: str = ""
    created_at: datetime | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_h1_usd: float | None = None
    volume_h24_usd: float | None = None
    fdv_usd: float | None = None
    #: Bewertung der *umlaufenden* Supply. Bei den meisten Memecoins identisch
    #: mit fdv_usd, weil alles im Umlauf ist - aber eben nicht bei allen.
    market_cap_usd: float | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    buyers_h1: int | None = None
    sellers_h1: int | None = None
    price_change_h1_pct: float | None = None
    #: Verlinkte Aussenauftritte, z.B. {"twitter": "https://x.com/...";}.
    socials: dict[str, str] = field(default_factory=dict)
    websites: list[str] = field(default_factory=list)

    @property
    def mcap_usd(self) -> float | None:
        """Marktkapitalisierung mit Rueckfall auf die FDV.

        Angezeigt und geprueft wird die umlaufende Bewertung, weil das die
        Zahl ist, die auf DexScreener steht. Fehlt sie, ist die FDV die beste
        verfuegbare Naeherung - bei fixer Supply ohne Sperren sind beide
        ohnehin gleich.
        """
        return self.market_cap_usd or self.fdv_usd

    @property
    def volume_to_mcap(self) -> float | None:
        """Stundenvolumen im Verhaeltnis zur Bewertung.

        Ein niedriger Wert heisst, dass die Token kaum den Besitzer wechseln,
        obwohl die Bewertung hoch ist - die Supply sitzt also fest. Das kann
        eine ueberzeugte Gemeinschaft sein oder ein einzelner Halter, der den
        Kurs stellt; unterscheiden laesst sich das hier noch nicht.
        """
        mcap = self.mcap_usd
        if not mcap or self.volume_h1_usd is None:
            return None
        return self.volume_h1_usd / mcap

    @property
    def has_socials(self) -> bool:
        return bool(self.socials or self.websites)

    @property
    def age_minutes(self) -> float | None:
        if self.created_at is None:
            return None
        now = datetime.now(timezone.utc)
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds() / 60.0

    @property
    def buy_sell_ratio(self) -> float | None:
        """Kaeufe je Verkauf. None wenn keine Daten, inf wenn nur Kaeufe."""
        if self.buys_h1 is None or self.sells_h1 is None:
            return None
        if self.sells_h1 == 0:
            return float("inf") if self.buys_h1 > 0 else None
        return self.buys_h1 / self.sells_h1

    @property
    def volume_to_liquidity(self) -> float | None:
        if not self.liquidity_usd or self.volume_h1_usd is None:
            return None
        return self.volume_h1_usd / self.liquidity_usd

    @property
    def trade_count_h1(self) -> int | None:
        if self.buys_h1 is None and self.sells_h1 is None:
            return None
        return (self.buys_h1 or 0) + (self.sells_h1 or 0)

    @property
    def wallet_count_h1(self) -> int | None:
        """Wie viele verschiedene Wallets ueberhaupt beteiligt waren.

        Kaeufer und Verkaeufer ueberschneiden sich stark, deshalb das Maximum
        statt der Summe - das ist die vorsichtige Schaetzung und vermeidet,
        dass Bot-Handel harmloser aussieht als er ist.
        """
        if self.buyers_h1 is None and self.sellers_h1 is None:
            return None
        return max(self.buyers_h1 or 0, self.sellers_h1 or 0)

    @property
    def trades_per_wallet(self) -> float | None:
        """Trades je beteiligter Wallet - das klarste Bot-Merkmal.

        Ein Mensch kauft ein-, vielleicht zweimal. Wer fuenfzig Trades mit
        drei Wallets macht, schiebt Token zwischen eigenen Adressen hin und
        her, um Volumen und einen belebten Chart zu erzeugen.

        Gemessen an 138 Pools mit nennenswertem Handel: Median 2.7, oberes
        Viertel ab 6.5, oberste 5% ab 21. Etwas maschineller Handel ist also
        voellig normal - erst die Ausreisser sind ein Signal.

        None, wenn zu wenig gehandelt wurde, um etwas abzulesen.
        """
        trades = self.trade_count_h1
        wallets = self.wallet_count_h1
        if trades is None or wallets is None or trades < MIN_TRADES_FOR_PATTERN:
            return None
        if wallets <= 0:
            return float(trades)
        return trades / wallets

    @property
    def momentum(self) -> int:
        """Schwung von 0 bis 100 - **beschreibend, nicht bewertend**.

        Bewusst getrennt vom Risikourteil, weil beides gegenlaeufig sein kann:
        ein Token mit konzentrierter Supply und koordinierten Wallets steigt
        oft besonders schnell - genau weil ihn jemand kontrolliert und stuetzt.
        Ein hoher Wert sagt also, dass gerade Bewegung drin ist, und nichts
        darueber, wie es ausgeht.

        50 ist neutral. Darueber: Kurs steigt und es wird mehr gekauft als
        verkauft. Darunter: Kurs faellt oder es wird verteilt.
        """
        score = 50.0

        if self.price_change_h1_pct is not None:
            # +200% ergibt den vollen Zuschlag, danach flacht es ab - der
            # Unterschied zwischen 200% und 900% sagt wenig ueber den Schwung.
            score += max(-35.0, min(35.0, self.price_change_h1_pct / 6.0))

        ratio = self.buy_sell_ratio
        if ratio is not None:
            capped = 5.0 if ratio == float("inf") else min(ratio, 5.0)
            score += max(-10.0, min(10.0, (capped - 1.0) * 4.0))

        buyers = self.buyers_h1 or 0
        if buyers >= 100:
            score += 5.0
        elif buyers >= 30:
            score += 3.0
        elif buyers < 5:
            score -= 5.0

        return int(max(0.0, min(100.0, score)))

    @property
    def traction(self) -> float:
        """Mass fuer frueh einsetzende Beteiligung.

        Bei einem wenige Minuten alten Token sagt die Liquiditaet fast nichts -
        die kann eine einzelne Wallet stellen. Aussagekraeftig ist, wie viele
        *verschiedene* Leute kaufen und ob sie halten oder sofort wieder
        rausgehen.

        Der Kaufueberhang wird gedeckelt: ein Verhaeltnis von 40:1 entsteht
        meist dadurch, dass schlicht noch niemand verkauft hat, und ist kein
        vierzigfach besseres Signal als 5:1.
        """
        buyers = self.buyers_h1 or 0
        if buyers <= 0:
            return 0.0
        ratio = self.buy_sell_ratio
        if ratio is None:
            factor = 1.0
        elif ratio == float("inf"):
            factor = 3.0
        else:
            factor = min(ratio, 5.0)
        return buyers * factor

    @property
    def label(self) -> str:
        return self.symbol or self.name or self.mint[:8]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        data["age_minutes"] = self.age_minutes
        data["momentum"] = self.momentum
        data["traction"] = self.traction
        data["mcap_usd"] = self.mcap_usd
        data["volume_to_mcap"] = self.volume_to_mcap
        return data


@dataclass
class ScreenResult:
    """Ergebnis des guenstigen Vorfilters."""

    candidate: TokenCandidate
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.candidate.mint,
            "symbol": self.candidate.symbol,
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


@dataclass
class MintInfo:
    """Geparster Mint-Account."""

    mint: str
    program: str
    decimals: int
    supply_raw: int
    mint_authority: str | None = None
    freeze_authority: str | None = None
    extensions: list[dict[str, Any]] = field(default_factory=list)
    name: str = ""
    symbol: str = ""

    @property
    def is_token_2022(self) -> bool:
        from .known import TOKEN_2022_PROGRAM

        return self.program == TOKEN_2022_PROGRAM

    @property
    def supply(self) -> float:
        return self.supply_raw / (10**self.decimals) if self.decimals >= 0 else 0.0

    def extension(self, name: str) -> dict[str, Any] | None:
        for ext in self.extensions:
            if ext.get("extension") == name:
                return ext.get("state", {}) or {}
        return None


@dataclass
class Holder:
    """Ein Token-Account mit Besitzer."""

    token_account: str
    amount_raw: int
    ui_amount: float
    owner: str | None = None
    pct: float = 0.0
    tag: str = ""

    @property
    def is_excluded(self) -> bool:
        """Pools, Burn-Adressen und Locker zaehlen nicht als Holder-Konzentration."""
        return bool(self.tag)


@dataclass
class HolderDistribution:
    holders: list[Holder] = field(default_factory=list)
    total_supply: float = 0.0
    top10_pct: float = 0.0
    top20_pct: float = 0.0
    largest_pct: float = 0.0
    excluded_pct: float = 0.0
    holder_count: int | None = None
    truncated: bool = True


@dataclass
class RiskReport:
    """Gesamtergebnis des Deep-Checks fuer einen Token."""

    mint: str
    symbol: str = ""
    name: str = ""
    candidate: TokenCandidate | None = None
    findings: list[Finding] = field(default_factory=list)
    mint_info: MintInfo | None = None
    distribution: HolderDistribution | None = None
    #: Ausgewerteter Kursverlauf, falls Kerzen vorlagen. Bewusst getrennt von
    #: den Befunden: die Richtung ist eine Beobachtung, keine Bewertung.
    structure: Any | None = None
    errors: list[str] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def score(self) -> int:
        """0-100. Startet bei 100, jeder Befund zieht ab."""
        total = 100 - sum(f.penalty for f in self.findings)
        return max(0, min(100, total))

    @property
    def data_gaps(self) -> list[str]:
        """Pruefungen, die mangels Daten kein Ergebnis liefern konnten."""
        return [f.code for f in self.findings if f.code in GAP_CODES]

    @property
    def verdict(self) -> Verdict:
        if any(f.severity is Severity.CRITICAL for f in self.findings):
            return Verdict.AVOID
        # Ohne belastbare On-Chain-Daten kein Urteil - lieber UNKNOWN als falsches OK.
        if self.mint_info is None:
            return Verdict.UNKNOWN

        score = self.score
        if score >= 80:
            best = Verdict.OK
        elif score >= 60:
            best = Verdict.CAUTION
        elif score >= 35:
            best = Verdict.RISKY
        else:
            best = Verdict.AVOID

        # Fehlende Daten sind kein Freispruch. Der Score zieht nur bei
        # tatsaechlichen Befunden ab - ein Token, ueber den noch nichts
        # bekannt ist, saehe sonst genauso gut aus wie ein geprueft sauberer.
        if best is Verdict.OK and self.data_gaps:
            return Verdict.CAUTION
        return best

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "name": self.name,
            "score": self.score,
            "verdict": self.verdict.value,
            "findings": [f.to_dict() for f in self.findings],
            "market": self.candidate.to_dict() if self.candidate else None,
            "holders": {
                "top10_pct": self.distribution.top10_pct,
                "top20_pct": self.distribution.top20_pct,
                "largest_pct": self.distribution.largest_pct,
                "excluded_pct": self.distribution.excluded_pct,
                "holder_count": self.distribution.holder_count,
            }
            if self.distribution
            else None,
            "structure": self.structure.to_dict() if self.structure else None,
            "errors": list(self.errors),
        }
