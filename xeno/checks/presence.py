"""Aussenauftritt und Umschlag - zwei billige Zusatzsignale.

Beide Werte liegen laengst in den Marktdaten, die XENO ohnehin abruft. Sie
kosten also keine einzige zusaetzliche Anfrage und wurden bisher nur nicht
gelesen.

**Aussenauftritt.** Ob ein Token eine Seite, einen X-Account oder eine Gruppe
hinterlegt hat, sagt nichts ueber Qualitaet - die Links stehen einfach in den
Startdaten und lassen sich frei eintragen. Sie sagen aber etwas ueber
Aufwand: wer gar nichts hinterlegt, hat auch nicht vor, dass jemand
nachfragt. Das ist ein schwaches Signal und wird bewusst schwach gewichtet.

**Umschlag.** Das Stundenvolumen im Verhaeltnis zur Bewertung beantwortet:
wechseln die Token ueberhaupt den Besitzer? Eine hohe Bewertung bei kaum
Handel heisst, dass die Supply festsitzt. Wer sie haelt, ist damit nicht
gesagt - eine ueberzeugte Gemeinschaft sieht hier genauso aus wie ein
einzelner Halter, der den Kurs stellt. Zusammen mit den Befunden zu
Konzentration und Herkunft wird daraus aber ein Bild.
"""

from __future__ import annotations

from ..config import RiskThresholds
from ..models import Finding, Severity
from .base import TokenData, finding

CHECK = "presence"

#: Stundenvolumen geteilt durch Bewertung. Unterhalb dieser Marke hat der
#: Token in einer Stunde weniger als ein Zwanzigstel seiner Bewertung
#: umgesetzt. Die gaengige Faustregel ("Volumen sollte ueber der Bewertung
#: liegen") bezieht sich auf das Tagesvolumen - auf eine Stunde
#: heruntergerechnet ist das diese Groessenordnung.
LOW_TURNOVER = 0.05
VERY_LOW_TURNOVER = 0.02

#: Erst ab dieser Bewertung sagt der Umschlag etwas aus. Bei einem Token mit
#: 3000 Dollar Bewertung ist jedes Verhaeltnis Zufall.
MIN_MCAP_FOR_TURNOVER = 15_000.0


def check_presence(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    candidate = data.candidate
    if candidate is None:
        return []

    findings: list[Finding] = []

    links = dict(candidate.socials)
    if candidate.websites:
        links["website"] = candidate.websites[0]

    if not links:
        findings.append(
            finding(
                CHECK,
                "no_public_presence",
                Severity.LOW,
                "Weder Website noch Social-Links hinterlegt",
            )
        )
    else:
        findings.append(
            finding(
                CHECK,
                "has_public_presence",
                Severity.INFO,
                "Hinterlegt: " + ", ".join(sorted(links)),
                links=links,
            )
        )

    turnover = candidate.volume_to_mcap
    mcap = candidate.mcap_usd
    if turnover is not None and mcap and mcap >= MIN_MCAP_FOR_TURNOVER:
        if turnover < VERY_LOW_TURNOVER:
            findings.append(
                finding(
                    CHECK,
                    "supply_barely_trades",
                    Severity.MEDIUM,
                    f"Bewertung ${mcap:,.0f} bei nur ${candidate.volume_h1_usd:,.0f} "
                    f"Stundenvolumen - die Token wechseln kaum den Besitzer",
                    turnover=round(turnover, 4),
                    mcap_usd=round(mcap, 2),
                )
            )
        elif turnover < LOW_TURNOVER:
            findings.append(
                finding(
                    CHECK,
                    "low_turnover",
                    Severity.LOW,
                    f"Geringer Umschlag: ${candidate.volume_h1_usd:,.0f} Volumen "
                    f"gegen ${mcap:,.0f} Bewertung",
                    turnover=round(turnover, 4),
                    mcap_usd=round(mcap, 2),
                )
            )

    return findings
