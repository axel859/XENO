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

> **Windows und noch nie ein Terminal benutzt?** → [WINDOWS.md](WINDOWS.md)
> führt Schritt für Schritt durch alles, ohne Vorwissen und ohne Git.

## Schnellstart

```bash
git clone https://github.com/axel859/XENO.git
cd XENO
python3 -m xeno scan
```

Keine Abhängigkeiten nötig — alles läuft mit der Python-Standardbibliothek.
Ab **Python 3.10**, ohne Obergrenze; getestet auf 3.11 und 3.14.

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
python3 -m xeno serve                    # Dashboard im Browser + Watcher
python3 -m xeno check <mint-adresse>     # einen Token gründlich prüfen
python3 -m xeno scan                     # suchen, filtern, prüfen
python3 -m xeno watch                    # überwachen, nur Konsole/Telegram
python3 -m xeno stats                    # auswerten, was aus den Urteilen wurde
python3 -m xeno screen                   # nur Vorfilter, ohne Deep-Check
python3 -m xeno config                   # aktive Einstellungen zeigen
```

## Beispiel

```
$ python3 -m xeno scan --limit 3        # Standardprofil: früh dran

URTEIL   PKT  TOKEN                   LIQ  TOP10  WICHTIGSTER BEFUND
------------------------------------------------------------------------------
OK        80  ACME           liq    $4.3k  top10   18%  Duenne Liquiditaet ($4,338)
AVOID      0  BOT            liq   $17.5k  top10   81%  Nur 0.0% der LP-Token sind gesichert
AVOID      0  TEAMMATES      liq    $2.1k  top10   94%  Groesste private Wallet haelt 85.0%
```

Alle drei waren zwischen **2 und 7 Minuten alt** — die beiden Fallen sind
erkannt, bevor irgendein Anstieg begonnen hat.

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
| **Maschineller Handel** | Trades pro Wallet und Umsatz ohne Kursbewegung — sieht der Chart echt aus oder gemacht |
| **Handelsmuster** | Gleichförmige Beträge in den echten Transaktionen — erkennt Wallet-Bündel, die ein Skript fahren |
| **Herkunft der Gelder** | Wer die größten Halter zuerst mit SOL versorgt hat — ein geteilter Geldgeber ist ein Halter, nicht mehrere |
| **Marktstruktur** | Richtung des Kursverlaufs und Abstand zum Höchststand — läuft es noch, oder ist der Lauf vorbei |
| **Außenauftritt & Umschlag** | Hinterlegte Website/Socials und Volumen gegen Bewertung |

### Warum die Pool-Ausklammerung entscheidend ist

Bei einem frischen Token hält der Liquidity-Pool regelmäßig über 90 % der
Supply. Ein Konzentrations-Check, der das nicht herausrechnet, meldet für
**jeden** normalen Token Alarm und ist damit wertlos. XENO markiert bekannte
AMM-Programme, Burn-Adressen, Locker und Börsen-Wallets und bewertet nur, was
tatsächlich in privater Hand liegt.

### Echter Chart oder gemachter?

Volumen und ein grüner Chart erzeugen Aufmerksamkeit — genau deshalb werden
sie hergestellt. Ein Programm schiebt Token zwischen eigenen Wallets hin und
her, der Token sieht belebt aus, und die echten Käufer kommen von selbst.
**Von außen wirkt so ein Token oft besser als ein ehrlicher.**

Das klarste Merkmal sind **Trades pro Wallet**:

```
AI    121 Trades von 8 Wallets   (15 je Wallet)   Kurs +57 %, Volumen 12.677 $
```

Sieht stark aus — bis man sieht, dass acht Wallets das unter sich ausmachen.

Die Schwellen stammen aus einer Messung an 138 Pools mit nennenswertem
Handel, nicht aus dem Bauch:

| Trades je Wallet | Anteil der Token | Bewertung |
|---|---|---|
| bis 6 | ~70 % | unauffällig, kein Befund |
| 6 – 12 | ~15 % | Hinweis (LOW) |
| 12 – 25 | ~10 % | überwiegend maschinell (MEDIUM) |
| ab 25 | ~5 % | fast nur maschinell (HIGH) |

**Etwas Bot-Handel steckt in fast jedem Chart** — Arbitrage, Sniper, Bots.
Deshalb ist die Prüfung abgestuft und schlägt erst bei Ausreißern an. Nur die
obersten Prozent fliegen schon im Vorfilter raus, damit das teure Prüfbudget
nicht für offensichtliches Wash-Trading draufgeht.

Zweites Signal: **Umsatz ohne Kursbewegung.** Dreht sich das Zwanzigfache der
Liquidität um, ohne dass der Kurs sich bewegt, kreist dasselbe Geld im Kreis.

Beides kostet **keine zusätzliche Anfrage** — die Zahlen kommen aus Daten, die
ohnehin geholt werden.

### Eine Ebene tiefer: die echten Transaktionen

Die Zahlen oben sind zusammengefasst. Wer viele Wallets benutzt und mit jeder
nur einmal handelt, fällt dort **nicht** auf. Genau dieser Fall trat beim
Ausprobieren auf:

```
[info  ] Handel wirkt echt: 164 Trades von 90 Wallets (1.8 je Wallet)
[medium] auffaellig viele identische Betraege: 26% sind exakt 0.0021 SOL
```

90 verschiedene Wallets — und ein Viertel davon handelt auf den Cent denselben
Betrag. Das ist ein Bündel, das ein Skript fährt.

Das Merkmal ist die **Gleichförmigkeit der Beträge**. Ein Programm kauft mit
fest eingestelltem Einsatz; Menschen kaufen 0,13, dann 2,40, dann 0,07.
Gemessen an echten Token:

| Anteil identischer Beträge | Bewertung |
|---|---|
| 2 – 10 % | beobachteter Normalbereich |
| ab 15 % | Hinweis |
| ab 25 % | auffällig |
| ab 40 % | fast alles maschinell |

Kostet **eine** Anfrage pro Token: Helius liefert 100 vorgeparste
Transaktionen auf einmal. Abschaltbar mit `--no-trade-pattern`, und ohne
Helius-Key läuft die Prüfung schlicht nicht — dann fehlt sie, statt eine
Entwarnung vorzutäuschen.

Die Grenzen stammen aus sechs ausgewerteten Token. Das trifft die
Größenordnung, reicht aber nicht für Feinjustierung — deshalb mit Abstand zum
Normalbereich gesetzt.

**Nicht umgesetzt:** die Regelmäßigkeit der Zeitabstände. Solana-Zeitstempel
haben Sekundenauflösung, bei 400-Millisekunden-Blöcken landen zu viele Trades
im selben Zeitpunkt — daraus lässt sich nichts ablesen.

### Bewertung

Jeder Token startet bei 100 Punkten, jeder Befund zieht ab (LOW 5, MEDIUM 15,
HIGH 30, CRITICAL 100). Ein einziger kritischer Befund bedeutet immer `AVOID`.

Wichtig ist die Gegenrichtung: **fehlende Daten sind kein Freispruch.** Wenn
die Holder-Verteilung nicht abrufbar war, hat der Token diese Prüfung nicht
bestanden — er wurde nicht geprüft. Solche Lücken verhindern ein `OK` und
erscheinen im Report unter „Wissenslücken".

---

## Suchprofile — früh dran oder auf Nummer sicher

Ein einziger Satz Schwellwerte kann nicht beides. Wer bei einem vier Minuten
alten Pool 5.000 $ Liquidität und 25 Käufer verlangt, bekommt ausschließlich
Token, deren Bewegung bereits gelaufen ist.

```bash
python3 -m xeno serve --profile early         # Standard
python3 -m xeno scan --profile established
```

| Profil | Findet | Alter | Quelle |
|---|---|---|---|
| **`early`** (Standard) | Frische Pools mit erster echter Beteiligung | 2 min – 6 h | nur neue Pools |
| `balanced` | Bereits handelbare Token mit Substanz | 3 min – 3 Tage | neu + Trending |
| `established` | Große, laufende Token | ab 1 h | nur Trending |

Dauerhaft festlegen über `XENO_PROFILE=early` in der `.env`.

### Live-Modus — Sekunden statt Minuten

```bash
python3 -m xeno serve --live
python3 -m xeno watch --live
```

Statt alle 60 Sekunden zu fragen, ob es Neues gibt, meldet der Solana-Knoten
es sofort. XENO abonniert die Programm-Logs von pump.fun; jede Token-Erstellung
erscheint dort binnen einer Sekunde.

**Kostet keine einzige zusätzliche Anfrage.** Mint, Name, Symbol und Ersteller
stehen bereits im Log — es muss nichts nachgeladen werden. Gemessen: rund 170
Ereignisse pro Sekunde über alle pump.fun-Aktivitäten, davon etwa **40
Token-Erstellungen pro Minute**. Gefiltert wird im Client, bevor irgendetwas
abgerufen wird.

Zwei Dinge, die der Live-Modus bringt:

- **Vollständigkeit** — jeder neue Token, nicht nur die, die ein Datendienst
  zufällig schon erfasst hat
- **Der exakte Geburtszeitpunkt** — statt eines geschätzten Alters

Was er **nicht** bringt: ein Token in Sekunde eins ist nicht bewertbar, denn
dann hat noch niemand gehandelt. Der Strom sammelt deshalb, und geprüft wird,
sobald genug Handel für eine Aussage stattgefunden hat. Der Gewinn ist, dass
XENO den Token dann **von Anfang an kennt** — inklusive genauem Alter.

**Voraussetzung:** ein eigener RPC-Zugang. Der öffentliche Solana-Knoten
erlaubt keine Abonnements; mit `HELIUS_API_KEY` läuft es.

Technisch: der WebSocket-Client ist selbst geschrieben (RFC 6455), damit das
Projekt abhängigkeitsfrei bleibt — Python bringt keinen mit. Bricht die
Verbindung ab, wird mit wachsendem Abstand neu verbunden.

### Suchbreite: einmalig ≠ dauerhaft

Ein einmaliger `scan` holt bei `early` **10 Seiten** (200 Pools). Der
Dauerbetrieb (`watch`, `serve`) nimmt bewusst nur **3** — aus zwei Gründen:

1. Die kostenlose GeckoTerminal-API sperrt sonst. Nachgemessen: 10 Seiten im
   Minutentakt führen ab dem **dritten** Durchlauf zu `HTTP 429`.
2. Tiefe Seiten bringen im Dauerbetrieb nichts. Dort stehen Pools, die eine
   Minute vorher schon auf Seite 1 standen und längst geprüft sind.

Wird die Quelle trotzdem einmal gedrosselt, steht das jetzt **im Log** — ein
gesperrter Zugang sah vorher genauso aus wie ein ruhiger Markt.

### Warum das nötig war

Die Werte sind nicht geschätzt, sondern aus **200 tatsächlich frisch erstellten
Pools** abgeleitet. Deren Wirklichkeit:

| Merkmal | Median | ursprüngliche Schwelle | Folge |
|---|---|---|---|
| Alter | **3 Minuten** | mind. 3 min | 89 % raus |
| Liquidität | 1.746 $ | mind. 5.000 $ | 85 % raus |
| Käufer/1 h | **4** | mind. 25 | 79 % raus |
| Volumen/1 h | 793 $ | mind. 2.000 $ | 73 % raus |

Von 100 frischen Pools kam **einer** durch — nicht weil die Token schlecht
waren, sondern weil die Messlatte für ihr Alter unerreichbar war. Übrig blieben
nur alte Trending-Token, deren Anstieg vorbei war.

### Der entscheidende Maßstabswechsel

Bei einem vier Minuten alten Token sagt die **Größe** nichts — 14.000 $
Liquidität kann eine einzelne Wallet stellen. Aussagekräftig ist die
**Beteiligung**: wie viele *verschiedene* Leute kaufen, und ob sie halten.

Deshalb sortiert `early` die Kandidaten nicht nach Liquidität, sondern nach
Käuferzahl mal Kaufdruck. Extreme Verhältnisse werden gedeckelt — 40:1 heißt
meist nur, dass noch niemand verkauft hat, und ist kein vierzigfach besseres
Signal als 5:1.

So sieht das Ergebnis aus:

```
$ python3 -m xeno scan --profile early --limit 3

OK     80  ACME        top10 18%   nur dünne Liquidität
AVOID   0  BOT         top10 81%   LP zu 0% gesichert
AVOID   0  TEAMMATES   top10 94%   85% der Supply in einer Wallet
```

Alle drei zwischen 2 und 7 Minuten alt — und die beiden Fallen sind erkannt,
bevor irgendein Anstieg begonnen hat.

### Was früh trotzdem nicht geht

Die Prüfungen, die zählen, funktionieren auch nach drei Minuten: Mint- und
Freeze-Authority, Token-2022-Extensions, LP-Sicherung und die
Creator-Historie stehen sofort in der Chain.

Dünner wird es bei **Holder-Verteilung und Bundling** — RugCheck braucht ein
paar Minuten, bis es einen neuen Token vollständig erfasst hat. Fehlende Daten
führen dann zu `CAUTION` statt `OK`, nie zu einem falschen Freibrief.

Und: `early` lässt deutlich mehr Kandidaten durch. Ohne eigenen RPC-Key wird
das Prüfbudget schnell knapp — **hier lohnt sich der kostenlose Helius-Key
wirklich.**

---

## Taugen die Urteile? — `xeno stats`

Ein Risikourteil ohne Rückmeldung bleibt eine Behauptung. XENO merkt sich
deshalb zu jedem geprüften Token den Ausgangskurs und schaut nach **15 min,
1 h, 6 h und 24 h** nach, was daraus wurde.

```bash
python3 -m xeno stats
```

```
URTEIL    ZEITPUNKT  ANZAHL    MEDIAN   >= 2x  ~ NULL    BESTE
--------------------------------------------------------------
OK              15m       3     0.99x      0%      0%     1.2x
OK               1h       3     0.99x      0%      0%     1.2x

AVOID           15m       2     0.89x      0%      0%     1.0x
AVOID            1h       2     0.89x      0%      0%     1.0x
```

Gemessen wird gegen das **erste** Urteil, nicht gegen das aktuelle — sonst
wanderte der Bezugspunkt mit und die Statistik hätte im Nachhinein immer
recht.

Zwei bewusste Entscheidungen:

- **Median statt Durchschnitt.** Ein einzelner Token, der sich verhundertfacht,
  würde einen Durchschnitt so verzerren, dass zwanzig Totalverluste daneben
  unsichtbar bleiben.
- **Verschwundener Markt zählt als 0.** Findet sich kein Paar mehr, ist das
  keine fehlende Messung, sondern das Ergebnis — sonst fiele genau der
  schlimmste Ausgang aus der Auswertung.

- **Verpasste Zeitpunkte bleiben Lücken.** Läuft der Bot nicht durchgehend,
  sind nach einer Pause mehrere Zeitpunkte gleichzeitig überfällig. Sie alle
  mit dem aktuellen Kurs zu füllen wäre bequem und falsch — der Wert gehörte
  dann zu einem ganz anderen Zeitpunkt als seiner Beschriftung. Eine Messung
  zählt nur innerhalb ihres Zeitfensters:

  | Zeitpunkt | fällig ab | gültig bis |
  |---|---|---|
  | 15m | 15 min | 22 min |
  | 1h | 60 min | 90 min |
  | 6h | 6 h | 9 h |
  | 24h | 24 h | 36 h |

  War der PC drei Stunden aus, erscheinen `15m` und `1h` als Lücke statt als
  erfundener Wert. Je durchgehender der Bot läuft, desto vollständiger die
  Auswertung — **die 24h-Zahlen bekommst du nur mit einem Dauerbetrieb.**

Der Abruf kostet nichts vom Prüfbudget: DexScreener liefert 30 Kurse pro
Anfrage, ohne RPC.

## Risiko ≠ Potenzial

Beides wird **getrennt** angezeigt, weil es gegenläufig sein kann:

```
BOT   [AVOID]  Score 0/100
  Schwung: 88/100  [#########.]   (beschreibt Bewegung, nicht Qualität)
```

Was einen Token steigen lässt, ist oft genau das, was ihn gefährlich macht:

| Merkmal | Wirkung auf den Kurs | Wirkung auf das Risiko |
|---|---|---|
| Wenig freier Umlauf | Kleine Käufe bewegen viel | Ein Wallet kann alles kippen |
| Koordinierte Wallets | Sauberer Chart, Aufmerksamkeit | Die Gruppe bestimmt das Ende |
| Team stützt den Kurs | Wirkt „stabil nach oben" | Stützt nur, bis es sich lohnt auszusteigen |

Umgekehrt sind breit gestreute Token oft langweilig — **weil sie niemand
kontrolliert**. Sicherheit und Bewegungslosigkeit hängen zusammen.

Der Schwung-Wert ist deshalb ausdrücklich **beschreibend, nicht empfehlend**:
er sagt, dass gerade Bewegung drin ist, und nichts darüber, wie es endet.

### Die Richtung — was lange fehlte

Lange hat XENO ausschließlich gemessen, **ob ein Token eine Falle ist**. Nicht,
ob er läuft. Das ist ein Unterschied, und er erklärt einen unangenehmen Effekt:
ein sauberer Token ohne Bewegung bekam dasselbe gute Urteil wie einer, der
gerade ausbricht.

Die Strukturanalyse liest deshalb den Kursverlauf mit — nach der ältesten Regel
der Chartlehre:

```
höhere Hochs UND höhere Tiefs   →  Aufwärtstrend
tiefere Hochs UND tiefere Tiefs →  Abwärtstrend
alles andere                    →  seitwärts
```

Zwei Entscheidungen dabei sind wichtig:

**Gemessen wird an den Kerzenkörpern, nicht an den Dochten.** Bei dünner
Liquidität reißt ein einzelner Kauf den Kurs für einen Moment weit nach oben;
auf dem Chart bleibt ein langer Docht stehen. Das ist die Handlung einer
einzelnen Wallet, nicht die Aussage des Marktes. Der Körper — Eröffnung bis
Schluss — überlebt nur, wenn der Kurs auch dort geblieben ist.

**Der Abstand zum Höchststand wird eigens gemeldet.** Das beantwortet die
Frage, die auf zusammengefassten Marktdaten unsichtbar bleibt: ob ein Token
seinen Lauf schon hinter sich hat. Ein echtes Beispiel aus einem Testlauf:

```
FOMEOW  [RISKY]  Score 50/100
  Markt:   MCap $2.2k | Liq $2.4k | Alter 21.4h
  Verlauf: Abwärts | -99% über 120 Kerzen | 99% unter Hoch
  Holder:  Top10 11.7% | größte 3.2%
```

Autoritäten abgegeben, LP verbrannt, Verteilung unauffällig, kein Bundling —
nach allen Sicherheitsprüfungen ein sauberer Token. Der Lauf hatte nur
zwanzig Stunden vorher stattgefunden. Ohne die Strukturanalyse wäre das ein
gutes Urteil geworden.

**Ein Abwärtstrend ist trotzdem kein Betrugsvorwurf.** Der Befund senkt die
Bewertung, weil ein fallender Kurs ein schlechter Einstieg ist — nicht, weil
am Token etwas faul wäre. Und umgekehrt macht ein Aufwärtstrend einen
gebündelten Token nicht sicher; genau so laufen gebündelte Token ja nach oben.

### Herkunft der Gelder — Nachweis statt Indiz

Die übrige Bundle-Erkennung arbeitet mit Ähnlichkeiten: gleich große Bestände,
benachbarte Konten, auffällige Muster. Das sind Indizien.

Hier geht es um einen Nachweis. Auf Solana kann eine Wallet ohne SOL nichts
tun — nicht einmal die Gebühr für den ersten Kauf bezahlen. Jede Wallet hat
deshalb einen Moment, in dem ihr **jemand das erste Geld geschickt hat**. Wenn
fünf der größten Halter dieses Geld von derselben Adresse bekamen, sind das
nicht fünf Käufer. Das ist einer.

Zwei Einschränkungen sind eingebaut, ohne die der Nachweis wertlos wäre:

* **Börsen zählen nicht.** Sie zahlen an tausende Wallets aus; wer über
  dieselbe Börse einsteigt, teilt den Absender, ohne sich zu kennen.
* **Wallets mit langer Historie werden gar nicht untersucht.** Wer hunderte
  Transaktionen hat, wurde nicht für diesen Token angelegt.

Der Abruf ist dabei bewusst auf **eine Anfrage je Wallet** begrenzt. Das ist
kein Kompromiss, sondern folgt aus der Sache: eine volle Seite Transaktionen
heißt „zu aktiv, um frisch zu sein" — und für alle anderen liegt damit die
gesamte Historie vor, die älteste Transaktion inklusive.

#### Warum diese Prüfung ein Gedächtnis braucht

Sie hat eine Eigenschaft, die sie von allen anderen unterscheidet: **ihr
Ergebnis ändert sich nie.** Wer eine Wallet zuerst mit SOL versorgt hat, ist
ein Ereignis der Vergangenheit.

In der ersten Fassung wurde genau das trotzdem ständig neu abgefragt. Ein
Token wird in der ersten Stunde alle fünf Minuten geprüft — zwölf Prüfungen,
hundert Anfragen, ein einziges Ergebnis. Beim ersten Dauerbetrieb war ein
Monatskontingent nach wenigen Tagen zu 38 % aufgebraucht.

Gemessen an einem echten Token, viermal hintereinander geprüft:

```
Prüfung 1:  6 Anfragen | Gedächtnis  5 Wallets | Trefferquote  0%
Prüfung 2:  6 Anfragen | Gedächtnis 10 Wallets | Trefferquote 29%
Prüfung 3:  3 Anfragen | Gedächtnis 12 Wallets | Trefferquote 52%
Prüfung 4:  1 Anfrage  | Gedächtnis 12 Wallets | Trefferquote 66%
```

Der zweite Gewinn ist weniger offensichtlich: **Bündel benutzen ihre Wallets
wieder.** Wer heute vier Adressen für einen Token bestückt, taucht mit
denselben Adressen nächste Woche beim nächsten auf. Jede erkannte Wallet kommt
also dem nächsten Token zugute.

Der Watcher meldet den Verbrauch seither in jeder Zeile mit — ein Kontingent,
das nur auf der Webseite des Anbieters steht, bemerkt man sonst erst, wenn es
fast leer ist:

```
Durchlauf 7: 132 gefunden, 4 gefiltert, 4 geprüft, 1 gemeldet, 9 API (23 gespart)
```

---

## Dashboard

```bash
python3 -m xeno serve
```

Öffnet unter **http://127.0.0.1:8000** eine Oberfläche im Browser — und startet
den Watcher gleich mit. Solange das Fenster offen ist, läuft der Bot.

Zu sehen sind: Status und Start/Stopp des Watchers, alle geprüften Token mit
Ampel und Punktzahl, die Watchlist, die letzten Meldungen und ein Log. Token
lassen sich direkt per Mint-Adresse prüfen oder auf die Watchlist setzen.

### Vom Handy aus

```bash
python3 -m xeno serve --lan
```

Gibt den Zugriff im lokalen WLAN frei und zeigt beim Start den passenden Link:

```
Auf diesem Rechner:  http://127.0.0.1:8000/?token=xxx
Vom Handy im WLAN:   http://192.168.1.42:8000/?token=xxx
```

Den Link am Handy öffnen, dann im Browser **Zum Startbildschirm hinzufügen** —
danach hat XENO ein eigenes Icon und sieht aus wie eine App.

Das Abtippen des Zufalls-Tokens ist am Handy lästig. Deshalb lässt sich ein
eigenes Wort setzen — dann muss man es nur einmal eingeben und kann die Seite
danach als Lesezeichen behalten:

```bash
python3 -m xeno serve --lan --token meinbot123
```

Es sind mindestens 6 Zeichen nötig; kürzere lehnt XENO ab. Alternativ dauerhaft
über `XENO_WEB_TOKEN` in der `.env`.

Sobald der Server nicht mehr nur auf dem eigenen Rechner lauscht, wird
automatisch ein Zugriffs-Token verlangt. Ohne das könnte jedes Gerät im selben
Netz die Watchlist ändern oder Prüfungen auslösen. Der Token steckt im
angezeigten Link — deshalb komplett kopieren.

### Was das Dashboard nicht kann

**Es macht den Bot nicht dauerhaft.** Wenn du den Rechner herunterfährst oder
das Terminal schließt, ist der Watcher weg. Eine App — egal ob Web oder nativ —
kann daran nichts ändern: ein Programm, das nicht läuft, prüft nichts.

Für echten Dauerbetrieb muss der Prozess auf etwas laufen, das immer an ist:

| | Kosten | |
|---|---|---|
| Kleiner VPS | ~4 €/Monat | unabhängig von Strom und Internet zuhause |
| Raspberry Pi | ~60 € einmalig | hängt an deinem Anschluss |
| Alter PC/Laptop | 0 € | muss durchlaufen |

Der Umzug ist reines Kopieren — dieselben Befehle, nur als Dienst gestartet.
Das Dashboard erreichst du dann von überall (am besten über ein privates Netz
wie Tailscale statt öffentlich).

Bis dahin gilt: **Telegram-Meldungen kommen nur, während der Bot läuft.**

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

### Meldekanäle

Alle sind optional und lassen sich einzeln abschalten. **Ohne jede Einrichtung**
laufen bereits diese drei:

| Kanal | Erreicht dich | Einrichtung |
|---|---|---|
| **Systemmeldung** | am Rechner, auch wenn das Fenster hinten liegt | keine |
| **Signalton** | am Rechner | keine |
| **Browser-Hinweis** | solange das Dashboard offen ist, plus Titel-Blinken im Hintergrundtab | einmal auf 🔔 tippen |
| Konsole | im Terminal | keine |
| Telegram | überall | Bot anlegen, siehe unten |
| JSON-Datei | zum späteren Auswerten | `--log-file` |

```bash
python3 -m xeno serve --only-important   # nur Verschlechterungen und neue kritische Befunde
python3 -m xeno serve --no-sound         # still
python3 -m xeno serve --no-desktop       # keine Systemmeldungen
```

Systemmeldungen nutzen die Bordmittel des Betriebssystems — `notify-send`
unter Linux, `osascript` unter macOS, PowerShell unter Windows. Kein
Zusatzmodul, kein Konto, kein fremder Dienst; alles bleibt auf deinem Rechner.
Unter Linux muss ggf. `sudo apt install libnotify-bin` nachinstalliert werden;
fehlt es, sagt XENO das beim Start und meldet weiter über die Konsole.

Zwei Vorsichtsmaßnahmen sind eingebaut: Systemmeldungen sind auf eine alle drei
Sekunden gedrosselt (sonst öffnen sich bei einem Durchlauf acht Fenster
gleichzeitig), und einen **Ton** gibt es nur bei Verschlechterung oder neuem
kritischem Befund — neue Kandidaten melden sich still.

**Wann Telegram sinnvoll wird:** wenn der Bot dauerhaft auf einem Server läuft
und du nicht davor sitzt. Solange er nur läuft, während dein PC an ist,
reichen Systemmeldung und Ton — ist der Rechner aus, gibt es ohnehin nichts zu
melden.

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

## Was der Bot verbraucht — und warum das eine eigene Schicht ist

Ein RPC-Anbieter rechnet nicht in Anfragen ab, sondern in **Credits**. Und
die sind nicht gleich viel wert:

| | Helius | QuickNode |
|---|---|---|
| Gewöhnlicher Aufruf | 1 Credit | 30 Credits |
| Große Abfrage | 1 | 120 |
| Geparste Transaktionen | **100** | — |
| Gratis-Kontingent / Monat | 1 Mio. | 10 Mio. |

Diese Tabelle ist teuer bezahlt. XENO hat pro geprüftem Token rund 600
Credits ausgegeben — drei gewöhnliche Aufrufe und sechs geparste
Transaktionen. Bei acht Token je Minute war ein Monatskontingent nach drei
Stunden leer, und bemerkt wurde es am nächsten Morgen auf der Webseite des
Anbieters.

**Der Fehler war nicht die Höhe des Verbrauchs.** Der Fehler war, dass ein
Bot überhaupt in der Lage war, ein Monatsbudget in einer Nacht auszugeben,
ohne dass ihn etwas daran gehindert oder auch nur davon erzählt hätte.

Daraus sind zwei Dinge entstanden:

### Der Zähler

`xeno/credits.py` kennt die Preisliste des erkannten Anbieters, zählt
persistent mit und verteilt das Monatsbudget auf die verbleibenden Tage. Wer
heute spart, hat morgen mehr — das ist selbstkorrigierend und braucht keine
Feineinstellung.

Reicht das Budget nicht, wird die Abfrage **verweigert**. Das ist
ausdrücklich keine Entwarnung: die betroffene Prüfung meldet eine
Wissenslücke, und ein Token mit Wissenslücken kommt nie über CAUTION hinaus.

`xeno config` zeigt den Stand, der Watcher meldet ihn in jeder Zeile:

```
Durchlauf 7: 132 gefunden, 4 gefiltert, 4 geprüft, 1 gemeldet, 600 Credits (32.400 heute frei)
```

### Die Stufung

Mehr als die Hälfte der Prüfungen kostet nichts — RugCheck, Jupiter,
GeckoTerminal und DexScreener sind fremde Dienste ohne Kontingent. Bisher
hing trotzdem alles zusammen: ein Token bekam die komplette Tiefprüfung oder
gar keine.

Jetzt läuft sie nach Kosten sortiert:

```
1. kostenlos    Marktdaten, RugCheck, Kursverlauf, Honeypot-Test
2. 1 Aufruf     Mint-Account (Mint- und Freeze-Authority)
   ── Abbruch, wenn hier schon ein schwerwiegender Befund steht ──
3. teurer       Holder-Verteilung über eigene Chain-Abfrage
4. am teuersten geparste Transaktionen: Handelsmuster, Herkunft der Gelder
```

Ein Token mit lebender Mint-Authority ist erledigt, egal wie seine Halter
verteilt sind. Ihm danach noch Abfragen hinterherzuwerfen, die das
Hundertfache kosten, war reine Verschwendung. Gemessen an einem Testfall
kostet ein so aussortierter Token jetzt **weniger als ein Zehntel** eines
vollständig geprüften.

---

## Wo die eigenen Daten liegen

Zustand und Zugangsdaten gehören dem Benutzer, nicht dem Programm. Sie liegen
deshalb außerhalb des Projektordners:

| System | Ort |
|---|---|
| Windows | `%LOCALAPPDATA%\XENO` |
| macOS | `~/Library/Application Support/XENO` |
| Linux | `~/.local/share/xeno` (bzw. `$XDG_DATA_HOME`) |

Der Grund ist praktischer Natur: Lagen sie im Projektordner, fing die
Nachverfolgung nach jedem Update in einem neuen Ordner wieder bei null an,
und ein versehentlich gelöschter Ordner nahm sie mit. Beides ist passiert.

Beim ersten Start nach dem Umzug wird ein vorhandener Altbestand aus dem
Arbeitsverzeichnis übernommen — **kopiert, nicht verschoben**, und niemals in
ein bereits belegtes Ziel. XENO meldet das einmal auf der Konsole; still
Dateien zu verschieben wäre unhöflich.

`XENO_DATA_DIR` setzt den Ort abweichend fest, `XENO_STATE_FILE` gezielt nur
die Zustandsdatei. `xeno config` zeigt unter **Ablage**, was gerade gilt.

---

## Konfiguration

Alles über Umgebungsvariablen oder eine `.env`. Gelesen werden zwei Orte: die
`.env` im Projektverzeichnis und die im Datenordner oben. Der Projektordner
wird zuerst gelesen und gewinnt damit — praktisch, um für einen einzelnen Lauf
etwas anderes einzustellen, ohne die dauerhafte Einrichtung anzufassen.

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
  mehrere Funding-Quellen arbeitet, fällt hier nicht auf. Die Herkunftsanalyse
  schließt einen Teil dieser Lücke — aber wer jede Wallet aus einer eigenen
  Quelle speist und ihr vorher etwas Handelshistorie verschafft, entgeht auch ihr.
- **Die Strukturanalyse liest keine Zukunft.** Sie beschreibt, was der Kurs
  bisher getan hat. Ein Aufwärtstrend kann in der nächsten Kerze brechen, und
  bei einem wenige Minuten alten Token gibt es überhaupt keinen Verlauf, aus
  dem sich etwas ablesen ließe — dann sagt sie ausdrücklich nichts.
- **Hinterlegte Links sagen wenig.** Website, X-Account und Telegram stehen frei
  eintragbar in den Startdaten. XENO prüft nur, *ob* etwas hinterlegt ist —
  nicht, ob der Account echt, alt oder gekauft ist. Entsprechend schwach fällt
  die Gewichtung aus.
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
python3 -m pytest -q        # 317 Tests, alle ohne Netzwerkzugriff
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
  desktop.py       Systemmeldungen und Signalton
  server.py        Dashboard-Server und JSON-API
  ws.py            WebSocket-Client (RFC 6455, ohne Fremdbibliothek)
  live.py          Live-Strom neuer Token, Reifephase vor der Prüfung
  web/index.html   die Oberfläche
```

---

## Haftungsausschluss

Kein Finanzrat. Memecoin-Handel ist Totalverlustrisiko. XENO ist ein
Prüfwerkzeug und ersetzt keine eigene Recherche — ein `OK` bedeutet
ausschließlich, dass die durchgeführten Prüfungen nichts gefunden haben.
