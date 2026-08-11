# XENO unter Windows — Schritt für Schritt

Für Leute, die noch nie ein Terminal benutzt haben. Kein Vorwissen nötig,
kein Git nötig. Dauert etwa 10 Minuten.

---

## Schritt 1 — Python installieren

> **Schon Python drauf?** Dann diesen Schritt überspringen. Alles ab **3.10**
> funktioniert, nach oben gibt es keine Grenze — XENO ist auf 3.11 **und 3.14**
> vollständig getestet (Tests, echter Scan, Dashboard).
>
> Prüfen lässt sich das mit `python --version` in PowerShell.

Python ist die Sprache, in der XENO geschrieben ist. Windows hat es nicht
vorinstalliert.

1. **Microsoft Store** öffnen (Startmenü → „Store" tippen)
2. Oben nach **`Python 3.12`** suchen (jede neuere Version geht genauso)
3. Auf **Installieren** klicken, warten

> Warum aus dem Store? Weil Windows Python damit automatisch richtig
> einrichtet. Bei der Installation von python.org muss man ein Häkchen bei
> „Add Python to PATH" setzen — wird das vergessen, funktioniert nachher
> nichts, und der Fehler ist schwer zu finden.

---

## Schritt 2 — XENO herunterladen

1. Diesen Link im Browser öffnen — der Download startet sofort:

   ```
   https://github.com/axel859/XENO/archive/refs/heads/claude/bot-programming-ygi80v.zip
   ```

2. Die heruntergeladene ZIP-Datei im **Downloads**-Ordner suchen
3. Rechtsklick darauf → **Alle extrahieren** → **Extrahieren**

Es entsteht ein Ordner namens `XENO-claude-bot-programming-ygi80v`.
Darin liegt noch einmal ein gleichnamiger Ordner — **den** brauchen wir. Er
enthält `README.md` und einen Ordner `xeno`.

---

## Schritt 3 — Das Terminal im richtigen Ordner öffnen

Das ist der Schritt, an dem die meisten hängenbleiben. Es gibt einen Trick,
mit dem man sich alles Weitere spart:

1. Den Ordner öffnen, in dem `README.md` liegt
2. Oben in die **Adressleiste** klicken (dort steht der Pfad)
3. Alles markieren, `powershell` eintippen, **Enter**

Es öffnet sich ein blaues Fenster — das ist PowerShell, und es steht bereits
im richtigen Ordner. Genau dort werden alle Befehle eingetippt.

---

## Schritt 4 — Prüfen, ob alles da ist

Diese Zeile eintippen und **Enter** drücken:

```
python -m xeno --version
```

**Erwartete Ausgabe:** `xeno 0.1.0`

<details>
<summary>Falls stattdessen ein Fehler kommt</summary>

- **„Python wurde nicht gefunden"** → einmal `py -m xeno --version` probieren.
  `py` ist der Windows-Starter und funktioniert auch dann, wenn `python`
  nicht im Suchpfad steht. Klappt auch das nicht: Schritt 1 nachholen und
  PowerShell **schließen und neu öffnen**.
- **„No module named xeno"** → du bist im falschen Ordner. Prüfen mit `dir` —
  in der Liste müssen `README.md` und `xeno` auftauchen. Sonst Schritt 3
  wiederholen, aber im richtigen Unterordner.

Funktioniert bei dir `py` statt `python`, dann in allen folgenden Befehlen
einfach `py` verwenden.

</details>

---

## Schritt 5 — Der erste Test

```
python -m xeno scan --limit 3
```

Jetzt sucht XENO echte Token und prüft sie. Das dauert ein paar Minuten —
solange nur der öffentliche Zugang genutzt wird, ist es langsam. Am Ende
erscheint eine Tabelle mit `OK`, `CAUTION`, `RISKY` oder `AVOID`.

**Läuft.** Ab hier ist alles andere nur noch Komfort.

---

## Schritt 6 — Das Dashboard

```
python -m xeno serve
```

Dann im Browser aufrufen:

```
http://127.0.0.1:8000
```

Das PowerShell-Fenster muss dabei **offen bleiben**. Beenden mit `Strg` + `C`.

---

## Schritt 7 — Vom Handy aus

```
python -m xeno serve --lan --token meinbot123
```

(`meinbot123` durch ein eigenes Wort ersetzen, mindestens 6 Zeichen.)

PowerShell zeigt dann so etwas:

```
Vom Handy im WLAN:   http://192.168.1.42:8000/?token=meinbot123
```

Diese Adresse am Handy im Browser eingeben — **exakt so, wie sie dort steht**,
die Zahlen sind bei dir andere.

**Windows fragt beim ersten Mal nach der Firewall.** Dort **„Private
Netzwerke"** ankreuzen und **Zugriff zulassen** — sonst kommt das Handy nicht
durch.

Wenn die Seite lädt: im Handy-Browser auf **Teilen → Zum Startbildschirm
hinzufügen**. Danach hat XENO ein eigenes Icon und startet wie eine App.

> Handy und PC müssen im **selben WLAN** sein. Über Mobilfunk geht es nicht.

---

## Wenn etwas nicht klappt

| Problem | Ursache |
|---|---|
| „Python wurde nicht gefunden" | Schritt 1 fehlt, oder PowerShell nach der Installation nicht neu geöffnet |
| „No module named xeno" | Falscher Ordner — mit `dir` prüfen, ob `xeno` und `README.md` dort liegen |
| Seite lädt am PC nicht | Läuft `serve` noch? Das Fenster darf nicht geschlossen sein |
| Handy kommt nicht drauf | Firewall blockiert, oder Handy hängt am Mobilfunk statt am WLAN |
| Port belegt | `python -m xeno serve --port 8080`, dann auch im Browser `:8080` |
| Farbiger Kauderwelsch wie `←[91m` | Sollte nicht mehr vorkommen; falls doch: `set NO_COLOR=1` |

---

## Später: dauerhaft laufen lassen

Solange das PowerShell-Fenster offen ist, arbeitet der Bot. Schließt du es
oder fährst den PC herunter, ist er aus.

Damit er wirklich rund um die Uhr läuft, muss er auf einem Gerät liegen, das
immer an ist — ein kleiner Server für etwa 4 € im Monat oder ein Raspberry Pi.
Dann wird auch Telegram sinnvoll, damit dich Meldungen unterwegs erreichen.
Solange XENO nur bei laufendem PC arbeitet, genügen die Systemmeldungen von
Windows.
