#!/usr/bin/env bash
# Gefuehrte Einrichtung: prueft Python, legt die .env an und richtet
# Telegram ein. Mehrfaches Ausfuehren ist unschaedlich - bestehende Werte
# werden ersetzt, nicht doppelt angehaengt.
#
#   bash setup.sh

set -u

ENV_FILE=".env"
BOLD=$'\033[1m'; DIM=$'\033[90m'; RED=$'\033[91m'; GREEN=$'\033[92m'; RESET=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '%s  %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s  %s%s\n' "$RED" "$*" "$RESET"; }

# Setzt einen Schluessel in der .env - ersetzt ihn, falls schon vorhanden.
set_env() {
    local key="$1" value="$2"
    touch "$ENV_FILE"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        # Umweg ueber eine temporaere Datei, weil sed -i sich auf macOS
        # und Linux unterschiedlich verhaelt.
        grep -v "^${key}=" "$ENV_FILE" > "${ENV_FILE}.tmp"
        mv "${ENV_FILE}.tmp" "$ENV_FILE"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}

get_env() {
    grep "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-
}

say "${BOLD}XENO - Einrichtung${RESET}"
say "${DIM}Abbrechen jederzeit mit Strg-C.${RESET}"

# --- 1. Python -------------------------------------------------------------
step "1. Python pruefen"
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    warn "Python 3.10 oder neuer nicht gefunden."
    say  "  Mac:     brew install python3"
    say  "  Ubuntu:  sudo apt install python3"
    say  "  Windows: https://python.org/downloads (bei der Installation"
    say  "           'Add Python to PATH' ankreuzen)"
    exit 1
fi
ok "$($PY --version) gefunden"

if [ ! -d "xeno" ]; then
    warn "Kein xeno/-Ordner hier. Das Skript im XENO-Projektordner ausfuehren."
    exit 1
fi

# --- 2. Kurzer Funktionstest ----------------------------------------------
step "2. Funktionstest (ohne Netzwerk)"
if $PY -m xeno --version >/dev/null 2>&1; then
    ok "XENO laeuft"
else
    warn "XENO startet nicht - bitte die Ausgabe pruefen:"
    $PY -m xeno --version
    exit 1
fi

# --- 3. Telegram -----------------------------------------------------------
step "3. Telegram einrichten"
say "${DIM}Ueberspringen mit Enter - Meldungen kommen dann nur auf der Konsole.${RESET}"
say ""
say "  Falls noch kein Bot existiert:"
say "    - In Telegram @BotFather anschreiben"
say "    - /newbot senden, Namen und Username vergeben"
say "    - Den Token kopieren"
say "    - Dem eigenen Bot einmal /start schicken  ${DIM}(wichtig!)${RESET}"
say ""

EXISTING_TOKEN="$(get_env TELEGRAM_BOT_TOKEN)"
if [ -n "$EXISTING_TOKEN" ]; then
    say "  Es ist bereits ein Token hinterlegt (${DIM}...${EXISTING_TOKEN: -6}${RESET})."
    printf '  Neuen Token eingeben oder Enter zum Behalten: '
else
    printf '  Bot-Token: '
fi
read -r TOKEN

if [ -z "$TOKEN" ] && [ -n "$EXISTING_TOKEN" ]; then
    TOKEN="$EXISTING_TOKEN"
fi

if [ -z "$TOKEN" ]; then
    say ""
    say "  Telegram uebersprungen."
else
    # Grobe Formpruefung, bevor wir das Netz bemuehen: <ziffern>:<rest>
    if ! printf '%s' "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
        warn "Das sieht nicht wie ein Telegram-Token aus."
        say  "  Erwartet wird etwas wie 7891234567:AAF-Xy9kLm3nQpRs7TuVwXyZ"
        say  "  Ohne Anfuehrungszeichen und ohne Leerzeichen."
        exit 1
    fi

    set_env TELEGRAM_BOT_TOKEN "$TOKEN"
    ok "Token gespeichert in $ENV_FILE"

    say ""
    say "  Suche deinen Chat ..."
    if ! $PY -m xeno telegram-setup; then
        say ""
        warn "Chat-Suche fehlgeschlagen - siehe Hinweise oben."
        exit 1
    fi

    if [ -z "$(get_env TELEGRAM_CHAT_ID)" ]; then
        say ""
        printf '  Chat-ID von oben hier eingeben: '
        read -r CHAT_ID
        if [ -n "$CHAT_ID" ]; then
            set_env TELEGRAM_CHAT_ID "$CHAT_ID"
            ok "Chat-ID gespeichert"
            say ""
            say "  Sende Testnachricht ..."
            $PY -m xeno telegram-setup >/dev/null 2>&1 && ok "Testnachricht verschickt - schau in Telegram"
        fi
    fi
fi

# --- 4. RPC ----------------------------------------------------------------
step "4. RPC (optional, aber empfohlen)"
say "  Der oeffentliche Solana-RPC beantwortet die Holder-Abfrage nicht."
say "  Ohne eigenen Key kommen diese Daten von RugCheck - das funktioniert,"
say "  ist aber eine fremde Quelle statt eigener Chain-Abfrage."
say ""
say "  Kostenlosen Key holen: ${DIM}https://helius.dev${RESET}"
say ""
if [ -n "$(get_env HELIUS_API_KEY)" ]; then
    ok "Helius-Key bereits hinterlegt"
else
    printf '  Helius-Key (Enter zum Ueberspringen): '
    read -r HELIUS
    if [ -n "$HELIUS" ]; then
        set_env HELIUS_API_KEY "$HELIUS"
        ok "Key gespeichert"
    else
        say "  Uebersprungen - laeuft auch ohne."
    fi
fi

# --- Fertig ----------------------------------------------------------------
step "Fertig"
say "  Einmalig pruefen:      $PY -m xeno scan"
say "  Dauerhaft ueberwachen: $PY -m xeno watch"
say "  Token beobachten:      $PY -m xeno watch --add <mint>"
say "  Einstellungen zeigen:  $PY -m xeno config"
say ""
say "${DIM}  Die .env enthaelt deine Zugangsdaten und ist per .gitignore${RESET}"
say "${DIM}  vom Repository ausgeschlossen.${RESET}"
