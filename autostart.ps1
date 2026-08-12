# XENO-Autostart einrichten oder entfernen.
#
#   .\autostart.ps1              einrichten
#   .\autostart.ps1 -Entfernen   wieder loswerden
#   .\autostart.ps1 -Zeigen      Status anzeigen
#
# Eingerichtet wird eine Aufgabe in der Windows-Aufgabenplanung, die bei
# der Anmeldung startet. Nicht der Autostart-Ordner: die Aufgabenplanung
# startet den Bot nach einem Absturz von selbst neu, und genau darum geht
# es hier - die Nachverfolgung braucht durchgehende Messungen.
#
# Der Bot laeuft in einem eigenen Fenster. Das ist Absicht: du sollst
# sehen, dass er laeuft, und ihn mit Strg-C anhalten koennen. Ein
# unsichtbarer Prozess, den man nur im Task-Manager findet, ist fuer
# niemanden ein Gewinn.

param(
    [switch]$Entfernen,
    [switch]$Zeigen,
    [string]$Token = "",
    [switch]$OhneLan
)

$ErrorActionPreference = "Stop"
$AufgabenName = "XENO Bot"

function Schreibe($text, $farbe = "White") { Write-Host $text -ForegroundColor $farbe }

# --- Status -----------------------------------------------------------------

if ($Zeigen) {
    $aufgabe = Get-ScheduledTask -TaskName $AufgabenName -ErrorAction SilentlyContinue
    if (-not $aufgabe) {
        Schreibe "Kein Autostart eingerichtet." Yellow
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $AufgabenName
    Schreibe "Autostart ist eingerichtet." Green
    Schreibe "  Zustand      : $($aufgabe.State)"
    Schreibe "  Zuletzt      : $($info.LastRunTime)"
    Schreibe "  Ergebnis     : $($info.LastTaskResult)  (0 = in Ordnung)"
    exit 0
}

# --- Entfernen --------------------------------------------------------------

if ($Entfernen) {
    if (Get-ScheduledTask -TaskName $AufgabenName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $AufgabenName -Confirm:$false
        Schreibe "Autostart entfernt. Der laufende Bot laeuft weiter, bis du ihn beendest." Green
    } else {
        Schreibe "Es war kein Autostart eingerichtet." Yellow
    }
    exit 0
}

# --- Einrichten -------------------------------------------------------------

# Python im vollen Pfad ermitteln. Die Aufgabenplanung hat eine andere
# Umgebung als deine Eingabeaufforderung - "python" allein findet sie
# unter Umstaenden nicht.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Schreibe "Python nicht gefunden." Red
    Schreibe "Installiere es von python.org und setze beim Installieren das Haeckchen" Red
    Schreibe "'Add Python to PATH'." Red
    exit 1
}

$ordner = $PSScriptRoot
if (-not (Test-Path (Join-Path $ordner "xeno"))) {
    Schreibe "Dieses Skript muss im XENO-Ordner liegen." Red
    Schreibe "Gefunden habe ich: $ordner" Red
    exit 1
}

# Zugangswort fuers Handy. Ohne Angabe wuerfelt der Server sich selbst eins
# aus - dann steht es aber nur einmal beim Start im Fenster, und bei einem
# Autostart schaut da niemand hin.
if (-not $Token -and -not $OhneLan) {
    Schreibe ""
    Schreibe "Der Bot soll vom Handy erreichbar sein. Dafuer braucht er ein Zugangswort." Cyan
    Schreibe "Mindestens 6 Zeichen. Leer lassen = nur auf diesem PC erreichbar."
    $Token = Read-Host "Zugangswort"
}

$argumente = "-m xeno serve --live"
if ($Token) {
    if ($Token.Length -lt 6) {
        Schreibe "Das Zugangswort ist zu kurz (mindestens 6 Zeichen)." Red
        exit 1
    }
    $argumente += " --lan --token $Token"
}

$aktion = New-ScheduledTaskAction -Execute $python -Argument $argumente -WorkingDirectory $ordner
$ausloeser = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Die Voreinstellungen der Aufgabenplanung sind fuer Wartungsjobs gedacht und
# hier durchweg falsch: sie beenden Aufgaben nach drei Tagen, halten sie im
# Akkubetrieb an und starten sie auf einem Notebook gar nicht erst.
$einstellungen = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName $AufgabenName -Action $aktion -Trigger $ausloeser `
        -Settings $einstellungen -Description "Startet den XENO-Scanner bei der Anmeldung." `
        -Force | Out-Null
} catch {
    Schreibe "Einrichten fehlgeschlagen: $_" Red
    Schreibe "Meist hilft es, PowerShell als Administrator zu oeffnen." Yellow
    exit 1
}

Schreibe ""
Schreibe "Autostart eingerichtet." Green
Schreibe ""
Schreibe "  Startet    : bei jeder Anmeldung an Windows"
Schreibe "  Ordner     : $ordner"
Schreibe "  Neustart   : bis zu 3x nach je 5 Minuten, falls er abstuerzt"
if ($Token) {
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
           Select-Object -First 1).IPAddress
    Schreibe "  Vom Handy  : http://${ip}:8000   Zugangswort: $Token"
}
Schreibe ""
Schreibe "Jetzt sofort starten, ohne neu anzumelden:" Cyan
Schreibe "  Start-ScheduledTask -TaskName '$AufgabenName'"
Schreibe ""
Schreibe "Wieder loswerden:" Cyan
Schreibe "  .\autostart.ps1 -Entfernen"
Schreibe ""
Schreibe "Deine Daten liegen ausserhalb dieses Ordners und ueberstehen jedes Update:" Cyan
Schreibe "  $env:LOCALAPPDATA\XENO"
