"""Kommandozeile.

    xeno check <mint>    einen Token tief pruefen
    xeno search <name>   Token nach Adresse, Ticker oder Namen finden
    xeno scan            neue und laufende Token suchen und pruefen
    xeno screen          nur Discovery und Vorfilter zeigen (ohne Deep-Check)
    xeno config          zeigen, welche Konfiguration aktiv ist
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .analyzer import TokenAnalyzer
from .config import Settings
from .discovery import Discovery
from .known import looks_like_mint
from .models import Verdict
from .net import HttpError
from .pipeline import Scanner
from .report import (
    format_report,
    format_report_line,
    format_screen_line,
    format_search_hit,
    to_json,
)
from .screen import screen_all

_RPC_HINT = (
    "Hinweis: Es laeuft der oeffentliche Solana-RPC. Der beantwortet\n"
    "         getTokenLargestAccounts nicht, die Holder-Daten kommen deshalb\n"
    "         von RugCheck. Fuer eigene On-Chain-Pruefung einen Key setzen:\n"
    "         HELIUS_API_KEY=... (kostenlos) oder XENO_RPC_URL=..."
)


def _open_state(args: argparse.Namespace):
    """Oeffnet den Zustand und meldet einmal, falls Altbestand uebernommen wurde.

    Die Uebernahme geschieht beim ersten Start nach dem Umzug. Sie
    stillschweigend zu erledigen waere falsch: der Benutzer soll wissen, dass
    seine Daten jetzt woanders liegen - schon damit er sie wiederfindet.
    """
    from .watchstate import WatchState

    state = WatchState(getattr(args, "state_file", None))
    if state.adopted_from is not None:
        print(
            f"Bisherige Daten uebernommen:\n"
            f"  von  {state.adopted_from}\n"
            f"  nach {state.path}\n"
            f"Dort ueberstehen sie ab jetzt jedes Update und jeden Neu-Download.",
            file=sys.stderr,
        )
    return state


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    """CLI-Argumente auf die Schwellwerte anwenden."""
    from dataclasses import replace

    screen = settings.screen
    changes = {}
    if getattr(args, "min_liquidity", None) is not None:
        changes["min_liquidity_usd"] = args.min_liquidity
    if getattr(args, "max_age_hours", None) is not None:
        changes["max_age_hours"] = args.max_age_hours
    if getattr(args, "min_volume", None) is not None:
        changes["min_volume_h1_usd"] = args.min_volume
    if changes:
        screen = replace(screen, **changes)
    return replace(settings, screen=screen)


def _source_choice(args: argparse.Namespace) -> tuple[bool | None, bool | None]:
    """Welche Quellen genutzt werden. ``None`` heisst: das Profil entscheidet.

    Ohne diese Unterscheidung wuerden die Schalter das Profil immer
    uebersteuern - ``not args.trending_only`` ist ja auch dann wahr, wenn der
    Schalter gar nicht gesetzt wurde.
    """
    if getattr(args, "new_only", False):
        return True, False
    if getattr(args, "trending_only", False):
        return False, True
    return None, None


def cmd_check(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    analyzer = TokenAnalyzer(settings)

    reports = []
    for mint in args.mint:
        report = analyzer.analyze(
            mint,
            test_trade=not args.no_trade_test,
            trade_pattern=not getattr(args, "no_trade_pattern", False),
        )
        reports.append(report)

    if args.json:
        print(to_json(reports))
    else:
        for index, report in enumerate(reports):
            if index:
                print()
            print(format_report(report, verbose=args.verbose))
        if settings.uses_public_rpc:
            print(f"\n{_RPC_HINT}", file=sys.stderr)

    return 0 if all(r.verdict is not Verdict.AVOID for r in reports) else 1


def cmd_search(args: argparse.Namespace) -> int:
    """Token nach Adresse, Ticker oder Namen finden.

    Ohne ``--check`` kostet das keine RPC-Credits: gesucht wird bei
    DexScreener, tief geprueft wird nur, was ausdruecklich verlangt ist.
    Eine Adresse wird direkt nachgeschlagen statt gesucht - das ist die
    genauere Abfrage.
    """
    term = " ".join(args.query).strip()
    settings = Settings.from_env()
    analyzer = TokenAnalyzer(settings)
    is_mint = looks_like_mint(term)

    if is_mint:
        pair = analyzer.dexscreener.best_pair(term)
        candidate = analyzer.dexscreener.as_candidate(pair) if pair else None
        matches = [candidate] if candidate else []
    else:
        matches = analyzer.dexscreener.search(term, limit=args.limit)

    if args.check:
        # Bei einer Adresse wird immer sie selbst geprueft, auch ohne Paar:
        # kein Handelspaar heisst nicht, dass es den Token nicht gibt - die
        # Pruefung liest den Mint direkt von der Chain.
        mint = term if is_mint else (matches[0].mint if matches else "")
        if not mint:
            print(f"Nichts gefunden zu '{term}'.", file=sys.stderr)
            return 1
        report = analyzer.analyze(mint)
        if args.json:
            print(to_json([report]))
        else:
            print(format_report(report, verbose=args.verbose))
            if settings.uses_public_rpc:
                print(f"\n{_RPC_HINT}", file=sys.stderr)
        return 0 if report.verdict is not Verdict.AVOID else 1

    if args.json:
        print(json.dumps([c.to_dict() for c in matches], indent=2, default=str))
        return 0 if matches else 1

    if not matches:
        hint = f" Pruefen geht trotzdem: xeno check {term}" if is_mint else ""
        print(f"Nichts gefunden zu '{term}'.{hint}")
        return 1

    for candidate in matches:
        print(format_search_hit(candidate))
    print(f"\n{len(matches)} Treffer. Pruefen mit: xeno check <mint>")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    settings = _apply_overrides(Settings.from_env(getattr(args, 'profile', None)), args)
    profile = settings.profile
    include_new, include_trending = _source_choice(args)
    candidates = Discovery().collect(
        include_new=profile.include_new if include_new is None else include_new,
        include_trending=(
            profile.include_trending if include_trending is None else include_trending
        ),
        pages=args.pages or profile.pages,
    )
    results = screen_all(candidates, settings.screen)

    from .pipeline import rank_key

    results.sort(key=lambda r: (not r.passed, -rank_key(r.candidate, profile)))

    for result in results:
        if result.passed or args.verbose:
            print(format_screen_line(result))

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed} von {len(results)} Kandidaten bestehen den Vorfilter")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    settings = _apply_overrides(Settings.from_env(getattr(args, 'profile', None)), args)
    scanner = Scanner(settings)

    def progress(message: str) -> None:
        if not args.json:
            print(f"  {message}", file=sys.stderr)

    include_new, include_trending = _source_choice(args)
    result = scanner.run(
        include_new=include_new,
        include_trending=include_trending,
        pages=args.pages,
        limit=args.limit,
        test_trade=not args.no_trade_test,
        on_progress=progress,
    )

    if args.json:
        print(to_json(result.reports))
        return 0

    print()
    if not result.reports:
        print("Kein Kandidat hat den Vorfilter bestanden.")
        print("Schwellwerte lockern, z.B. --min-liquidity 1000 --min-volume 500")
        return 0

    print(f"{'URTEIL':8} {'PKT':>3}  {'TOKEN':14} {'LIQ':>12}  {'TOP10':>5}  WICHTIGSTER BEFUND")
    print("-" * 100)
    for report in result.reports:
        print(format_report_line(report))

    good = [r for r in result.reports if r.verdict in (Verdict.OK, Verdict.CAUTION)]
    print(f"\n{len(good)} von {len(result.reports)} geprueften Token ohne schweren Befund.")
    print("Details zu einem Token:  xeno check <mint>")

    if settings.uses_public_rpc:
        print(f"\n{_RPC_HINT}", file=sys.stderr)
    return 0


def _build_notifier(args: argparse.Namespace):
    """Stellt die Meldekanaele zusammen. Konsole ist immer dabei."""
    from .desktop import DesktopNotifier
    from .notify import ConsoleNotifier, JsonlNotifier, MultiNotifier, TelegramNotifier

    channels = [ConsoleNotifier()]

    if not args.no_desktop:
        desktop = DesktopNotifier(
            sound=not args.no_sound, only_important=args.only_important
        )
        if desktop.available:
            channels.append(desktop)
            print("  Systemmeldungen aktiv", file=sys.stderr)
        else:
            print(
                "  Systemmeldungen nicht verfuegbar"
                + (
                    " (unter Linux: sudo apt install libnotify-bin)"
                    if sys.platform.startswith("linux")
                    else ""
                ),
                file=sys.stderr,
            )

    if not args.no_telegram:
        telegram = TelegramNotifier.from_env()
        if telegram is not None:
            channels.append(telegram)
            print("  Telegram aktiv", file=sys.stderr)
        else:
            print(
                "  Telegram inaktiv (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt)\n"
                "  Einrichtung:  xeno telegram-setup",
                file=sys.stderr,
            )

    if args.log_file:
        channels.append(JsonlNotifier(args.log_file))

    return MultiNotifier(channels) if len(channels) > 1 else channels[0]


def _live_feed(settings: Settings, enabled: bool, log=None):
    """Erzeugt den Live-Strom aus der RPC-Adresse.

    Der WebSocket laeuft ueber dieselbe Adresse wie der RPC, nur mit wss://
    statt https://. Der oeffentliche Solana-Knoten laesst keine Abos zu,
    deshalb braucht der Live-Modus einen eigenen Zugang.
    """
    if not enabled:
        return None
    if settings.uses_public_rpc:
        print(
            "  Live-Modus nicht moeglich: der oeffentliche RPC erlaubt keine\n"
            "  Abonnements. HELIUS_API_KEY in der .env setzen.",
            file=sys.stderr,
        )
        return None

    from .live import LiveFeed

    ws_url = settings.rpc_url.replace("https://", "wss://").replace("http://", "ws://")
    feed = LiveFeed(ws_url)
    if feed.start(on_error=log):
        print("  Live-Strom aktiv (neue Token in Sekunden statt Minuten)", file=sys.stderr)
    return feed


def cmd_watch(args: argparse.Namespace) -> int:
    from .watcher import Watcher

    state = _open_state(args)

    # Watchlist-Verwaltung: ausfuehren und beenden, nicht ueberwachen.
    if args.add:
        for mint in args.add:
            state.add_to_watchlist(mint)
            print(f"Zur Watchlist hinzugefuegt: {mint}")
        state.save()
        return 0

    if args.remove:
        for mint in args.remove:
            if state.remove_from_watchlist(mint):
                print(f"Von der Watchlist entfernt: {mint}")
            else:
                print(f"Stand nicht auf der Watchlist: {mint}")
        state.save()
        return 0

    if args.list:
        entries = state.watchlist
        if not entries:
            print("Watchlist ist leer.  Hinzufuegen:  xeno watch --add <mint>")
            return 0
        print(f"{'URTEIL':8} {'PKT':>3}  {'TOKEN':14} {'GEPRUEFT':>9}  MINT")
        for entry in sorted(entries, key=lambda s: s.score, reverse=True):
            checked = (
                f"{(time.time() - entry.last_checked) / 60:.0f}min"
                if entry.last_checked
                else "nie"
            )
            print(
                f"{entry.verdict:8} {entry.score:3d}  {entry.symbol or '?':14.14} "
                f"{checked:>9}  {entry.mint}"
            )
        return 0

    settings = _apply_overrides(Settings.from_env(getattr(args, 'profile', None)), args)
    live = _live_feed(settings, getattr(args, "live", False))
    watcher = Watcher(
        settings, state=state, notifier=_build_notifier(args), live=live
    )
    try:
        watcher.run(
            interval=args.interval,
            budget=args.budget,
            test_trade=not args.no_trade_test,
            max_cycles=args.cycles,
        )
    finally:
        if live is not None:
            live.stop()
    return 0


def _local_ip() -> str:
    """Ermittelt die eigene Adresse im lokalen Netz - fuer den Handy-Zugriff."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Es wird nichts gesendet; der Aufruf waehlt nur das Interface aus.
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import build_server

    settings = _apply_overrides(Settings.from_env(getattr(args, 'profile', None)), args)
    state = _open_state(args)

    token_override = args.token or os.environ.get("XENO_WEB_TOKEN", "").strip() or None
    if token_override and len(token_override) < 6:
        print(
            "Das Zugangswort ist sehr kurz. Mindestens 6 Zeichen waehlen -\n"
            "sonst kann es im Netzwerk zu leicht erraten werden.",
            file=sys.stderr,
        )
        return 1

    try:
        httpd, app, watcher_thread = build_server(
            host=args.host,
            port=args.port,
            auth_token=token_override,
            settings=settings,
            state=state,
            use_telegram=not args.no_telegram,
            use_desktop=not args.no_desktop,
            sound=not args.no_sound,
            only_important=args.only_important,
            live=_live_feed(settings, getattr(args, "live", False)),
        )
    except OSError as exc:
        print(f"Server konnte nicht starten: {exc}", file=sys.stderr)
        if getattr(exc, "errno", None) in (48, 98):
            print(f"Port {args.port} ist belegt. Anderen waehlen: --port 8080", file=sys.stderr)
        return 1

    token = getattr(httpd, "auth_token", "")
    suffix = f"?token={token}" if token else ""

    print("\nXENO Dashboard laeuft\n")
    print(f"  Auf diesem Rechner:  http://127.0.0.1:{args.port}/{suffix}")
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"  Vom Handy im WLAN:   http://{_local_ip()}:{args.port}/{suffix}")
        print(
            "\n  Der Zugriff ist mit einem Token geschuetzt, weil der Server\n"
            "  im ganzen Netzwerk erreichbar ist. Den Link komplett kopieren."
        )
    print("\n  Beenden mit Strg-C\n")

    if not args.no_autostart:
        watcher_thread.start(interval=args.interval, budget=args.budget)
        app.log("Watcher automatisch gestartet")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeende ...")
    finally:
        watcher_thread.stop()
        httpd.shutdown()
        try:
            state.save()
        except OSError as exc:
            print(f"Zustand konnte nicht gespeichert werden: {exc}", file=sys.stderr)
    return 0


def cmd_telegram_setup(args: argparse.Namespace) -> int:
    """Fuehrt durch die Telegram-Einrichtung.

    Die Chat-ID ist der unangenehme Teil - sie steht nirgends sichtbar in der
    App. Dieser Befehl liest sie aus den Updates des Bots aus.
    """
    import os

    from .notify import TelegramNotifier, telegram_discover_chat_id

    Settings.from_env()  # laedt .env
    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    if not token:
        print(
            "Kein Bot-Token.\n\n"
            "  1. In Telegram @BotFather anschreiben, /newbot senden\n"
            "  2. Den erhaltenen Token hier eintragen:\n"
            "       echo 'TELEGRAM_BOT_TOKEN=dein-token' >> .env\n"
            "  3. Dem eigenen Bot in Telegram irgendeine Nachricht schicken\n"
            "  4. Diesen Befehl erneut ausfuehren\n"
        )
        return 1

    try:
        chats = telegram_discover_chat_id(token)
    except HttpError as exc:
        if exc.status == 401:
            print(
                "Telegram lehnt den Token ab (401).\n\n"
                "  - Vollstaendig kopiert? Der Token sieht aus wie\n"
                "    123456789:AAF-abcdefghijklmnopqrstuvwxyz123456789\n"
                "  - Kein Leerzeichen und keine Anfuehrungszeichen in der .env\n"
                "  - Notfalls bei @BotFather mit /revoke einen neuen erzeugen"
            )
        else:
            print(f"Telegram nicht erreichbar: {exc}")
        return 1

    if not chats:
        print(
            "Bot erreichbar, aber keine Chats gefunden.\n"
            "Schick deinem Bot in Telegram eine beliebige Nachricht und\n"
            "fuehre diesen Befehl dann erneut aus."
        )
        return 1

    print("Gefundene Chats:\n")
    for chat in chats:
        print(f"  Chat-ID {chat['id']}   {chat['type']}   {chat['name']}")
    print("\nIn die .env eintragen:")
    print(f"  TELEGRAM_CHAT_ID={chats[0]['id']}")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id:
        notifier = TelegramNotifier(token, chat_id)
        if notifier.send_text("XENO ist eingerichtet. Meldungen kommen hier an."):
            print("\nTestnachricht verschickt.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Zeigt, was aus den geprueften Token tatsaechlich geworden ist."""
    from .follow import HORIZONS, IMPLAUSIBLE_MULTIPLE, summarise

    state = _open_state(args)
    entries = [
        s
        for s in state.tokens.values()
        if s.first_verdict and (s.outcomes or s.baseline_at)
    ]

    if not entries:
        print("Noch keine Daten.\n")
        print("XENO merkt sich ab jetzt zu jedem geprueften Token den Kurs und")
        print("schaut nach 15min, 1h, 6h und 24h nach. Lass den Bot ein paar")
        print("Stunden laufen, dann steht hier die Auswertung.")
        _print_paper(state.path)
        return 0

    summary = summarise(entries)
    if not summary:
        wartend = len(entries)
        print(f"{wartend} Token werden beobachtet, aber noch keine Messung faellig.")
        print("Die erste kommt 15 Minuten nach der jeweiligen Pruefung.")
        _print_paper(state.path)
        return 0

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print("Was ist aus den geprueften Token geworden?")
    print("Gemessen ab dem ersten Urteil, Median ueber alle Token je Gruppe.\n")

    order = ["OK", "CAUTION", "RISKY", "AVOID", "UNKNOWN"]
    header = f"{'URTEIL':9}{'ZEITPUNKT':>10}{'ANZAHL':>8}{'MEDIAN':>10}{'>= 2x':>8}{'~ NULL':>8}{'BESTE':>9}"
    print(header)
    print("-" * len(header))

    for verdict in order:
        per_horizon = summary.get(verdict)
        if not per_horizon:
            continue
        for label in HORIZONS:
            row = per_horizon.get(label)
            if not row:
                continue
            print(
                f"{verdict:9}{label:>10}{row['count']:>8}"
                f"{row['median']:>9.2f}x{row['winners_pct']:>7.0f}%"
                f"{row['dead_pct']:>7.0f}%{row['best']:>8.1f}x"
            )
        print()

    # Gezaehlt werden Token, nicht Tabellenzeilen. Der erste Anlauf stand
    # hier als ``sum(len(v) for v in summary.values())`` - das summiert die
    # Zeitpunkte je Urteil, also fuenf Urteile mal drei Zeitpunkte = 15.
    # Die Fussnote riet damit zur Vorsicht ("15 ist noch wenig"), waehrend
    # in der Tabelle darueber achthundert Messungen standen.
    total = sum(
        1
        for e in entries
        if e.first_verdict and any(v is not None for v in (e.outcomes or {}).values())
    )
    print(f"Grundlage: {total} Token mit mindestens einer Messung.")

    broken = sum(
        row.get("broken", 0)
        for per_horizon in summary.values()
        for row in per_horizon.values()
    )
    if broken:
        print(
            f"{broken} Messungen aussortiert: Vielfaches ueber "
            f"{IMPLAUSIBLE_MULTIPLE:,.0f}x. Da war nicht der Kurs so hoch,\n"
            "sondern der Ausgangswert kaputt - bei sekundenalten Token liefert\n"
            "die Kursquelle manchmal fast null."
        )

    if "CONTROL" in summary:
        print(
            "\nCONTROL ist die Vergleichsgruppe: zufaellig gezogene Token, die der\n"
            "Vorfilter abgelehnt hat. Laufen sie aehnlich gut wie die\n"
            "durchgelassenen, filtert XENO nur Zufall. Laufen sie besser,\n"
            "filtert er in die falsche Richtung."
        )

    _print_paper(state.path)
    print(
        "\nMEDIAN heisst: die Haelfte lief besser, die Haelfte schlechter.\n"
        "Bewusst nicht der Durchschnitt - ein einzelner Hunderter wuerde\n"
        "sonst zwanzig Totalverluste daneben unsichtbar machen.\n"
        "'~ NULL' = auf ein Zehntel oder weniger gefallen."
    )
    if total < 50:
        print(
            f"\nAchtung: {total} Urteile sind noch wenig. Erst ab einigen hundert\n"
            "sind die Unterschiede zwischen den Gruppen belastbar."
        )
    return 0


def _print_paper(state_path=None) -> None:
    """Die Bilanz des Papierhandels - die eigentliche Antwort.

    Ein Median sagt, wie sich Token entwickelt haben. Diese Zahl sagt, was
    dabei herausgekommen waere. Das ist nicht dasselbe: sie enthaelt die
    Ausstiegsregel, und die entscheidet mit.

    Das Buch gehoert zur Zustandsdatei: wer mit ``--state-file`` eine
    zweite Messreihe fuehrt, bekommt hier deren Bilanz und nicht die der
    ersten.
    """
    from .paper import STOP_LOSS, TAKE_PROFIT, PaperBook, book_path_for

    book = PaperBook(book_path_for(state_path) if state_path else None)
    result = book.summary()
    if not result["closed"] and not result["open"]:
        print("\nPapierhandel: noch keine Calls. Es wird nur bei klarer Lage einer.")
        return

    print("\nPapierhandel - was mit den Calls herausgekommen waere")
    print(
        f"  Regel            100 USD je Call, raus bei {TAKE_PROFIT:.0f}x "
        f"oder {(1 - STOP_LOSS) * 100:.0f}% Verlust"
    )
    print(f"  Abgeschlossen    {result['closed']}  (offen: {result['open']})")
    if result["closed"]:
        print(
            f"  Ergebnis         {result['result_usd']:+.2f} USD auf "
            f"{result['invested_usd']:.0f} USD Einsatz"
        )
        print(f"  Davon im Plus    {result['wins']} ({result['win_rate']:.0f}%)")
        if result["median_multiple"] is not None:
            print(
                f"  Median           {result['median_multiple']:.2f}x  "
                f"| bester {result['best_multiple']:.2f}x"
            )
        # Die Kostenseite. Sie steckt schon im Ergebnis - hier steht sie
        # noch einmal einzeln, weil sonst niemand sehen kann, wieviel an der
        # Strategie liegt und wieviel am Handel selbst.
        if result["costs_usd"]:
            anteil = ""
            if result["invested_usd"]:
                anteil = (
                    f" ({result['costs_usd'] / result['invested_usd'] * 100:.1f}% "
                    "vom Einsatz)"
                )
            print(f"  Ohne Kosten      {result['gross_result_usd']:+.2f} USD")
            print(
                f"  Handelskosten    -{result['costs_usd']:.2f} USD{anteil}, "
                f"davon {result['fees_usd']:.2f} Gebuehren"
            )
            gemessen = result["measured"]
            gesamt = result["closed"] + result["open"]
            print(
                f"  Rueckweg         im Schnitt {result['avg_retention'] * 100:.1f}% "
                f"| {gemessen} von {gesamt} gemessen, Rest geschaetzt"
            )
        if result["reasons"]:
            grund = ", ".join(f"{k}: {v}" for k, v in sorted(result["reasons"].items()))
            print(f"  Ausstiege        {grund}")
    if result["open"]:
        print(f"  Unrealisiert     {result['unrealised_usd']:+.2f} USD")


def cmd_config(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    rpc = settings.rpc_url
    if "api-key=" in rpc:  # Key nie ausgeben
        rpc = rpc.split("api-key=")[0] + "api-key=***"
    profile = settings.profile
    print(f"XENO {__version__}")
    if profile is not None:
        print(f"  Profil           : {profile.name} - {profile.summary}")
        quellen = []
        if profile.include_new:
            quellen.append("neue Pools")
        if profile.include_trending:
            quellen.append("Trending")
        print(f"  Quellen          : {', '.join(quellen) or 'keine'}")
        print(f"  Suchbreite       : {profile.pages} Seiten (~{profile.pages * 20} Pools)")
        print(
            "  Reihenfolge      : "
            + ("Beteiligung (Kaeufer, Kaufdruck)" if profile.rank_by == "traction"
               else "Liquiditaet")
        )
    print(f"  RPC              : {rpc}")
    print(f"  RPC ist oeffentl.: {settings.uses_public_rpc}")
    print(f"  RPC-Rate         : {settings.rpc_rate_limit}/s")

    # Wo die eigenen Daten liegen. Steht bewusst hier und nicht nur in der
    # Anleitung: wer sie sichern oder mitnehmen will, muss sie finden koennen.
    from .credits import CreditMeter
    from .paths import describe

    meter = CreditMeter(settings.rpc_url)
    print("\n  Kontingent")
    if not meter.capped:
        print(f"    Anbieter         {meter.provider} (kein Kontingent)")
    else:
        snapshot = meter.snapshot()
        print(f"    Anbieter         {snapshot['provider']}")
        print(
            f"    Monat            {snapshot['month_spent']:,} von "
            f"{snapshot['monthly_cap']:,} Credits".replace(",", ".")
        )
        print(
            f"    Heute            {snapshot['day_spent']:,} von "
            f"{snapshot['daily_allowance']:,} Credits".replace(",", ".")
        )
        print(f"    Restliche Tage   {snapshot['days_left']}")

    print("\n  Ablage")
    for label, value in describe().items():
        print(f"    {label:16} {value}")
    print("\n  Vorfilter (Stage 1)")
    for key, value in vars(settings.screen).items():
        print(f"    {key:26} {value}")
    print("\n  Risikogrenzen (Stage 2)")
    for key, value in vars(settings.risk).items():
        print(f"    {key:26} {value}")
    if settings.uses_public_rpc:
        print(f"\n{_RPC_HINT}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xeno",
        description="Scanner und Risikoanalyse fuer Solana-Memecoins. "
        "Fuehrt keine Trades aus - es werden ausschliesslich Daten gelesen.",
    )
    parser.add_argument("--version", action="version", version=f"xeno {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--json", action="store_true", help="Ausgabe als JSON")
        target.add_argument("-v", "--verbose", action="store_true", help="alle Befunde zeigen")

    def add_live(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--live",
            action="store_true",
            help="neue Token per WebSocket sofort empfangen statt sie abzufragen "
            "(braucht HELIUS_API_KEY oder XENO_RPC_URL)",
        )

    def add_alerting(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--no-desktop", action="store_true", help="keine Systemmeldungen"
        )
        target.add_argument("--no-sound", action="store_true", help="kein Signalton")
        target.add_argument(
            "--only-important",
            action="store_true",
            help="nur bei Verschlechterung und neuen kritischen Befunden melden",
        )
        target.add_argument("--no-telegram", action="store_true", help="Telegram nicht nutzen")

    def add_discovery(target: argparse.ArgumentParser) -> None:
        from .profiles import DEFAULT_PROFILE, PROFILES

        target.add_argument(
            "--profile",
            choices=list(PROFILES),
            help="Suchstrategie. "
            + "  ".join(f"{n}: {p.summary}" for n, p in PROFILES.items())
            + f"  (Standard: {DEFAULT_PROFILE})",
        )
        target.add_argument("--pages", type=int, help="Seiten pro Quelle (je 20 Pools)")
        target.add_argument("--new-only", action="store_true", help="nur frisch erstellte Pools")
        target.add_argument("--trending-only", action="store_true", help="nur laufende Token")
        target.add_argument("--min-liquidity", type=float, help="Mindestliquiditaet in USD")
        target.add_argument("--min-volume", type=float, help="Mindestvolumen 1h in USD")
        target.add_argument(
            "--max-age-hours",
            type=float,
            help="maximales Alter in Stunden (hoeher setzen fuer etablierte Token)",
        )

    p_check = sub.add_parser("check", help="einen oder mehrere Token tief pruefen")
    p_check.add_argument("mint", nargs="+", help="Mint-Adresse(n)")
    p_check.add_argument(
        "--no-trade-test", action="store_true", help="Kauf-/Verkaufstest ueberspringen"
    )
    p_check.add_argument(
        "--no-trade-pattern",
        action="store_true",
        help="Musteranalyse der Transaktionen ueberspringen (spart eine Anfrage je Token)",
    )
    add_common(p_check)
    p_check.set_defaults(func=cmd_check)

    p_search = sub.add_parser(
        "search",
        help="Token nach Adresse, Ticker oder Namen finden",
        description=(
            "Sucht bei DexScreener und zeigt die Treffer mit Marktdaten. "
            "Kostet keine RPC-Credits - erst --check prueft tief."
        ),
    )
    p_search.add_argument("query", nargs="+", help="Mint-Adresse, Ticker oder Name")
    p_search.add_argument(
        "--check", action="store_true", help="besten Treffer gleich tief pruefen"
    )
    p_search.add_argument("--limit", type=int, default=8, help="max. Treffer (8)")
    add_common(p_search)
    p_search.set_defaults(func=cmd_search)

    p_screen = sub.add_parser("screen", help="nur Discovery und Vorfilter")
    add_discovery(p_screen)
    add_common(p_screen)
    p_screen.set_defaults(func=cmd_screen)

    p_scan = sub.add_parser("scan", help="suchen, filtern und tief pruefen")
    add_discovery(p_scan)
    p_scan.add_argument("--limit", type=int, default=10, help="max. Deep-Checks (Standard 10)")
    p_scan.add_argument(
        "--no-trade-test", action="store_true", help="Kauf-/Verkaufstest ueberspringen"
    )
    add_common(p_scan)
    p_scan.set_defaults(func=cmd_scan)

    p_watch = sub.add_parser(
        "watch",
        help="dauerhaft ueberwachen und bei Aenderungen melden",
        description="Sucht laufend neue Kandidaten und ueberwacht die Watchlist. "
        "Meldet nur, was neu oder anders ist.",
    )
    add_discovery(p_watch)
    p_watch.add_argument(
        "--interval", type=float, default=60.0, help="Sekunden zwischen Durchlaeufen (60)"
    )
    p_watch.add_argument(
        "--budget", type=int, default=8, help="max. Tiefpruefungen pro Durchlauf (8)"
    )
    p_watch.add_argument(
        "--cycles", type=int, help="nach so vielen Durchlaeufen beenden (Standard: endlos)"
    )
    p_watch.add_argument("--state-file", help="Pfad der Zustandsdatei")
    p_watch.add_argument("--log-file", help="jede Meldung als JSON-Zeile anhaengen")
    add_alerting(p_watch)
    add_live(p_watch)
    p_watch.add_argument(
        "--no-trade-test", action="store_true", help="Kauf-/Verkaufstest ueberspringen"
    )
    p_watch.add_argument(
        "--add", nargs="+", metavar="MINT", help="Token zur Watchlist hinzufuegen und beenden"
    )
    p_watch.add_argument(
        "--remove", nargs="+", metavar="MINT", help="Token von der Watchlist entfernen"
    )
    p_watch.add_argument("--list", action="store_true", help="Watchlist anzeigen")
    p_watch.set_defaults(func=cmd_watch)

    p_serve = sub.add_parser(
        "serve",
        help="Weboberflaeche starten (Watcher laeuft mit)",
        description="Startet Dashboard und Watcher im selben Prozess. "
        "Solange das Fenster offen ist, laeuft der Bot.",
    )
    add_discovery(p_serve)
    p_serve.add_argument("--port", type=int, default=8000, help="Port (8000)")
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Adresse. Standard nur dieser Rechner; 0.0.0.0 gibt das lokale "
        "Netz frei (dann wird ein Token verlangt)",
    )
    p_serve.add_argument(
        "--lan",
        action="store_const",
        const="0.0.0.0",
        dest="host",
        help="Kurzform fuer --host 0.0.0.0 (Zugriff vom Handy im WLAN)",
    )
    p_serve.add_argument("--interval", type=float, default=60.0, help="Sekunden je Durchlauf (60)")
    p_serve.add_argument("--budget", type=int, default=8, help="max. Tiefpruefungen je Durchlauf (8)")
    p_serve.add_argument("--state-file", help="Pfad der Zustandsdatei")
    p_serve.add_argument(
        "--token",
        help="eigenes Zugangswort statt eines zufaelligen (leichter am Handy "
        "einzutippen), mindestens 6 Zeichen",
    )
    add_alerting(p_serve)
    add_live(p_serve)
    p_serve.add_argument(
        "--no-autostart", action="store_true", help="Watcher nicht automatisch starten"
    )
    p_serve.set_defaults(func=cmd_serve)

    p_telegram = sub.add_parser("telegram-setup", help="Telegram einrichten und testen")
    p_telegram.add_argument("--token", help="Bot-Token (sonst aus TELEGRAM_BOT_TOKEN)")
    p_telegram.set_defaults(func=cmd_telegram_setup)

    p_stats = sub.add_parser(
        "stats",
        help="auswerten, was aus den geprueften Token geworden ist",
        description="Zeigt je Urteil, wie sich die Token danach entwickelt haben. "
        "Beantwortet die Frage, ob die Bewertungen ueberhaupt etwas taugen.",
    )
    p_stats.add_argument("--state-file", help="Pfad der Zustandsdatei")
    p_stats.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    p_stats.set_defaults(func=cmd_stats)

    p_config = sub.add_parser("config", help="aktive Konfiguration anzeigen")
    p_config.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
