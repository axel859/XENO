# XENO

Scanner und On-Chain-Risikoanalyse für Solana-Memecoins.

XENO sucht neu gestartete und aktuell laufende Token, filtert sie grob vor und
prüft die verbleibenden Kandidaten gründlich: Authorities, LP-Sicherung,
Holder-Verteilung, Bundling, Creator-Historie und ob sich die Position
überhaupt wieder verkaufen lässt.

**XENO handelt nicht.** Es liest ausschließlich Daten, signiert nichts und
sendet keine Transaktionen. Es beantwortet eine Frage: *Ist dieser Token eine
Falle?*

---

## Schnellstart

```bash
git clone https://github.com/axel859/XENO.git
cd XENO
python3 -m xeno scan
```

Keine Abhängigkeiten nötig — alles läuft mit der Python-Standardbibliothek
(ab Python 3.10).

### Geführte Einrichtung

Für Telegram-Meldungen und den optionalen RPC-Key gibt es ein Skript, das
durch alles durchfragt und die `.env` selbst anlegt:

```bash
bash setup.sh
```

Es prüft die Python-Version, testet den Start, richtet Telegram ein (inklusive
Ermitteln der Chat-ID) und fragt nach einem Helius-Key. Alles ist
überspringbar, mehrfaches Ausführen ist unschädlich.

**Windows:** in der PowerShell läuft `bash` nicht. Entweder Git Bash benutzen
(kommt mit [Git für Windows](https://git-scm.com/download/win)), oder WSL —
oder die `.env` von Hand anlegen, siehe [Konfiguration](#konfiguration).

```bash
python3 -m xeno check <mint-adresse>     # einen Token gründlich prüfen
python3 -m xeno scan                     # suchen, filtern, prüfen
python3 -m xeno watch                    # dauerhaft überwachen und melden
python3 -m xeno screen                   # nur Vorfilter, ohne Deep-Check
python3 -m xeno config                   # aktive Einstellungen zeigen
```

## Beispiel

```
$ python3 -m xeno scan --trending-only --max-age-hours 200 --limit 6

URTEIL   PKT  TOKEN                   LIQ  TOP10  WICHTIGSTER BEFUND
------------------------------------------------------------------------------
OK        95  XST            liq   $53.4k  top10    8%  keine Auffaelligkeiten
CAUTION   65  Remus          liq   $94.1k  top10   12%  5 verbundene Wallet-Netzwerke (26 Wallets)
RISKY     35  TOAD           liq  $471.6k  top10   28%  4 verbundene Wallet-Netzwerke (91 Wallets)
AVOID      0  Dealer         liq  $109.0k  top10   49%  Groesste private Wallet haelt 31.0%
AVOID      0  BULLWHALE      liq   $30.8k  top10   63%  Groesste private Wallet haelt 51.3%
```

```
$ python3 -m xeno check A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump

TOAD  [RISKY]  Score 35/100
  Mint: A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump
  Holder: Top10 28.0% | groesste 17.2% | Pools/Burn ausgeklammert 2.2% | gesamt 49015

   X [bundling] 4 verbundene Wallet-Netzwerke erkannt (91 Wallets)
   ! [holders] Groesste private Wallet haelt 17.2% der Supply
   ! [creator] Ersteller hat bereits 6 Token gelauncht
   . [authority] Mint- und Freeze-Authority sind abgegeben
   . [liquidity] LP zu 100.0% verbrannt oder gelockt
   . [tradability] Kauf und Verkauf moeglich, Round-Trip-Verlust 0.3%
```

---

## Wie es arbeitet

Der Aufbau ist ein Trichter, und zwar aus einem konkreten Grund: die Discovery
liefert 20 Kandidaten pro Request, der Deep-Check kostet pro Token mehrere.
Würde man alles tief prüfen, wäre jedes Kontingent nach Minuten aufgebraucht.

**Stage 0 – Discovery.** Neue und laufende Pools von GeckoTerminal und
DexScreener, dedupliziert.

**Stage 1 – Vorfilter.** Nur Marktdaten, kein einziger RPC-Call: Alter,
Liquidität, Volumen, eindeutige Käufer, Kauf/Verkauf-Verhältnis und das
Verhältnis Volumen zu Liquidität (auffällig hohe Werte deuten auf
Wash-Trading). Typisch bleiben davon 20–50 % übrig.

**Stage 2 – Deep-Check.** Erst hier wird es teuer:

| Prüfung | Was gefunden wird |
|---|---|
| **Mint-Authority** | Ersteller kann unbegrenzt nachdrucken → deine Anteile verwässern |
| **Freeze-Authority** | Ersteller kann Wallets einfrieren → du kaufst, verkaufst aber nie |
| **Token-2022-Extensions** | `permanentDelegate` (Zugriff auf deine Token), `transferHook` (Verkauf blockierbar), `transferFee`, `defaultAccountState: frozen`, `nonTransferable` |
| **LP-Sicherung** | Sind die LP-Token verbrannt oder gelockt, oder kann die Liquidität abgezogen werden |
| **Holder-Verteilung** | Konzentration in privaten Wallets — Pools, Burns, Locker und Börsen werden herausgerechnet |
| **Bundling** | Verbundene Wallet-Netzwerke und Wallets mit auffällig gleichen Beständen |
| **Creator-Historie** | Frühere Launches desselben Wallets und dokumentierte Rugs |
| **Handelbarkeit** | Simulierter Kauf **und Rückverkauf** über Jupiter — der eigentliche Honeypot-Test |

### Warum die Pool-Ausklammerung entscheidend ist

Bei einem frischen Token hält der Liquidity-Pool regelmäßig über 90 % der
Supply. Ein Konzentrations-Check, der das nicht herausrechnet, meldet für
**jeden** normalen Token Alarm und ist damit wertlos. XENO markiert bekannte
AMM-Programme, Burn-Adressen, Locker und Börsen-Wallets und bewertet nur, was
tatsächlich in privater Hand liegt.

### Bewertung

Jeder Token startet bei 100 Punkten, jeder Befund zieht ab (LOW 5, MEDIUM 15,
HIGH 30, CRITICAL 100). Ein einziger kritischer Befund bedeutet immer `AVOID`.

Wichtig ist die Gegenrichtung: **fehlende Daten sind kein Freispruch.** Wenn
die Holder-Verteilung nicht abrufbar war, hat der Token diese Prüfung nicht
bestanden — er wurde nicht geprüft. Solche Lücken verhindern ein `OK` und
erscheinen im Report unter „Wissenslücken".

---

## Watch-Modus

`scan` ist eine Momentaufnahme. `watch` läuft dauerhaft, sucht neue Kandidaten
und behält gleichzeitig eine selbst gepflegte Watchlist im Blick.

```bash
python3 -m xeno watch                              # loslegen
python3 -m xeno watch --interval 30 --budget 12    # schneller, mehr Prüfungen
python3 -m xeno watch --add <mint>                 # Token zur Watchlist
python3 -m xeno watch --list                       # Watchlist anzeigen
```

### Was er meldet — und was nicht

Gemeldet wird nur bei echtem Zustandswechsel. Ein Watcher, der im Minutentakt
dieselben zehn Token durchgibt, wird nach kurzer Zeit ignoriert.

| Anlass | Wann |
|---|---|
| **Neuer Kandidat** | Ein frischer Token erreicht `OK` oder `CAUTION` |
| **WARNUNG – kritische Änderung** | Bei einem bekannten Token taucht ein kritischer Befund auf, den es vorher nicht gab |
| **Verschlechtert** | Das Urteil ist gefallen (`OK` → `RISKY`) |
| **Verbessert** | Das Urteil ist gestiegen — einmalig, nicht bei jedem Durchlauf |

Unveränderte Token bleiben still.

Die zweite Zeile ist der eigentliche Grund für den Watch-Modus. Ein einmaliger
Scan sagt dir, wie ein Token *jetzt* aussieht. Der Watcher merkt, wenn die
LP-Sperre verschwindet, eine Wallet anfängt einzusammeln oder die
Verkaufsroute wegfällt — also genau dann, wenn der Rug gerade läuft. Für einen
Token, den du **hältst**, ist das mehr wert als jede Neuentdeckung.

### Wiederholungsprüfung

Wie oft ein Token erneut geprüft wird, hängt von seinem Alter ab — ein fünf
Minuten alter Token ändert sich ständig, ein drei Tage alter kaum:

```
< 1h alt   →  alle 5 min          Watchlist:  mindestens alle 10 min,
< 6h alt   →  alle 15 min                     unabhängig vom Alter
< 24h alt  →  stündlich
älter      →  alle 4h             AVOID mit kritischem Befund:  nie wieder
```

Der letzte Punkt spart spürbar Budget: ein Token mit aktiver Mint-Authority
wird nicht besser, den muss man nicht stündlich neu prüfen. Es sei denn, er
steht auf deiner Watchlist — dann wird weiter geprüft.

### Budget

Ein Deep-Check kostet etwa fünf Requests. `--budget` deckelt, wie viele pro
Durchlauf laufen (Standard 8). Bei 60 Sekunden Intervall sind das rund 40
Requests/Minute — für Helius' Gratis-Tarif unproblematisch, für den
öffentlichen RPC zu viel.

Die Reihenfolge ist priorisiert: **Watchlist zuerst**, dann neue Token, dann
fällige Wiederholungen (die mit dem besten Score zuerst). Eine Verschlechterung
bei einem Token, den du hältst, kostet Geld — ein verpasster Neuzugang nur eine
Gelegenheit.

Der Zustand liegt in `xeno-state.json`, wird atomar geschrieben und übersteht
Neustarts, ohne alles erneut zu melden.

### Telegram einrichten

Ein Watcher ist nur sinnvoll, wenn dich die Meldung erreicht, während du nicht
ins Terminal schaust.

```
1. In Telegram @BotFather anschreiben  →  /newbot  →  Token kopieren
2. echo 'TELEGRAM_BOT_TOKEN=dein-token' >> .env
3. Dem eigenen Bot eine beliebige Nachricht schicken
4. python3 -m xeno telegram-setup      →  zeigt die Chat-ID an
5. echo 'TELEGRAM_CHAT_ID=die-id' >> .env
```

Schritt 4 nimmt dir den unangenehmen Teil ab — die Chat-ID steht nirgends
sichtbar in der App. Danach verschickt der Befehl eine Testnachricht.

Ohne Telegram läuft alles genauso, nur auf der Konsole. `--log-file meldungen.jsonl`
schreibt zusätzlich jede Meldung als JSON-Zeile mit — praktisch, um nach ein
paar Tagen auszuwerten, ob deine Schwellwerte überhaupt taugen.

Ein Ausfall des Meldekanals beendet die Überwachung nie: der Fehler landet auf
der Konsole, die Schleife läuft weiter. Dasselbe gilt für Netzwerkaussetzer bei
der Discovery oder einzelnen Token.

---

## Konfiguration

Alles über Umgebungsvariablen oder eine `.env` im Projektverzeichnis.

### RPC (empfohlen)

Der öffentliche Solana-RPC beantwortet `getTokenLargestAccounts`
**grundsätzlich nicht** — nicht nur gedrosselt, sondern gar nicht. Ohne
eigenen RPC kommen die Holder-Daten deshalb von RugCheck. Das funktioniert,
ist aber eine fremde Quelle statt eigener Chain-Abfrage.

```bash
# Helius hat einen kostenlosen Tarif, der dafür locker reicht
echo "HELIUS_API_KEY=dein-key" >> .env

# oder ein beliebiger anderer RPC
echo "XENO_RPC_URL=https://..." >> .env
```

### Schwellwerte

`python3 -m xeno config` zeigt alle aktiven Werte. Die wichtigsten:

| Variable | Standard | Bedeutung |
|---|---|---|
| `XENO_MIN_LIQUIDITY` | 5000 | Mindestliquidität in USD |
| `XENO_MIN_VOLUME_H1` | 2000 | Mindestvolumen 1 h |
| `XENO_MAX_AGE_HOURS` | 72 | maximales Alter — höher setzen für etablierte Token |
| `XENO_MIN_BUYERS_H1` | 25 | eindeutige Käufer in 1 h |
| `XENO_MAX_TOP10_PCT` | 30 | ab hier gilt die Verteilung als zu konzentriert |
| `XENO_MIN_LP_LOCKED_PCT` | 90 | geforderter Anteil gesicherter LP-Token |

Kurzfristig auch direkt auf der Kommandozeile:

```bash
python3 -m xeno scan --min-liquidity 20000 --max-age-hours 12 --limit 20
python3 -m xeno scan --json > ergebnisse.json
```

---

## Was XENO *nicht* leistet

Das gehört genauso dazu wie die Feature-Liste:

- **Kein Gewinnversprechen.** XENO filtert Betrug heraus, nicht Verlust. Ein
  Token kann jede Prüfung bestehen und trotzdem auf null gehen — die
  überwiegende Mehrheit der Memecoins tut das.
- **Nicht schnell genug fürs Snipen.** Bis belastbare Daten vorliegen, sind
  die ersten Sekunden vorbei. Wer als Erster drin sein will, braucht eigene
  Nodes und konkurriert mit sechsstelligen Infrastrukturbudgets. XENO ist auf
  „ist das eine Falle" ausgelegt, nicht auf „bin ich der Erste".
- **Bundling-Erkennung ist Heuristik.** Wallet-Netzwerke und gleich große
  Bestände sind starke Hinweise, kein Beweis. Wer die Beträge streut und über
  mehrere Funding-Quellen arbeitet, fällt hier nicht auf.
- **Die Holder-Analyse sieht die größten ~20 Accounts.** Das reicht für
  Konzentration, sagt aber nichts über den langen Schwanz.
- **Fremddaten können falsch sein.** RugCheck und die Aggregatoren hängen bei
  ganz frischen Token hinterher. XENO fängt die offensichtlichen Widersprüche
  ab (siehe unten), aber nicht alle.

### Bekannte Fallstricke, die XENO bereits abfängt

Zwei Dinge, die beim Bau aufgefallen sind und die naive Implementierungen
falsch bewerten:

1. **Jupiter-Quotes sind bei frischen Bonding-Curve-Pools inkonsistent.** Im
   Test kam beim Rückverkauf mehr heraus als eingesetzt wurde (bis 190 %) —
   real unmöglich. Solche Ergebnisse werden verworfen statt als Signal
   gewertet, und eine fehlende Verkaufsroute gilt bei Pools unter 30 Minuten
   als möglicher Indexierungsverzug, nicht als Honeypot.
2. **`totalHolders: 0` bei gleichzeitig vorhandenen Top-Holdern** ist ein
   Widerspruch in den Quelldaten, kein fehlender Streubesitz. Wird als
   „unbekannt" behandelt.

---

## Entwicklung

```bash
pip install pytest
python3 -m pytest -q        # 117 Tests, alle ohne Netzwerkzugriff
```

Die Prüfungen in `xeno/checks/` sind reine Funktionen über `TokenData` und
machen selbst keine Netzwerkzugriffe — der gesamte Datenabruf liegt in
`xeno/analyzer.py`. Dadurch sind die Bewertungsregeln vollständig offline
testbar.

Eine neue Prüfung anlegen:

```python
# xeno/checks/meine_pruefung.py
def check_something(data: TokenData, thresholds: RiskThresholds) -> list[Finding]:
    return [finding("meine", "code", Severity.HIGH, "Was gefunden wurde")]
```

und in `xeno/checks/__init__.py` in `ALL_CHECKS` eintragen.

### Aufbau

```
xeno/
  cli.py           Kommandozeile
  pipeline.py      Discovery -> Vorfilter -> Deep-Check
  discovery.py     Kandidaten sammeln und deduplizieren
  screen.py        Stage-1-Vorfilter (ohne RPC)
  analyzer.py      Stage-2: sammelt alle Rohdaten ein
  chain.py         Chain-Rohdaten parsen, Holder-Verteilung bauen
  checks/          die Einzelprüfungen (netzwerkfrei)
  sources/         GeckoTerminal, DexScreener, RugCheck, Jupiter, Solana-RPC
  known.py         bekannte Programme, Pools, Locker, Burn-Adressen
  models.py        Datentypen, Score und Urteil
  watcher.py       Überwachungsschleife und Meldeentscheidung
  watchstate.py    Zustand, Watchlist, Wiederholungsintervalle
  notify.py        Konsole, Telegram, JSON-Log
```

---

## Haftungsausschluss

Kein Finanzrat. Memecoin-Handel ist Totalverlustrisiko. XENO ist ein
Prüfwerkzeug und ersetzt keine eigene Recherche — ein `OK` bedeutet
ausschließlich, dass die durchgeführten Prüfungen nichts gefunden haben.
