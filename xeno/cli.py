"""Kommandozeile.

    xeno check <mint>    einen Token tief pruefen
    xeno scan            neue und laufende Token suchen und pruefen
    xeno screen          nur Discovery und Vorfilter zeigen (ohne Deep-Check)
    xeno config          zeigen, welche Konfiguration aktiv ist
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .analyzer import TokenAnalyzer
from .config import PUBLIC_RPC, Settings
from .discovery import Discovery
from .models import Verdict
from .net import HttpError
from .pipeline import Scanner
from .report import (
    format_report,
    format_report_line,
    format_screen_line,
    to_json,
)
from .screen import screen_all

_RPC_HINT = (
    "Hinweis: Es laeuft der oeffentliche Solana-RPC. Der beantwortet\n"
    "         getTokenLargestAccounts nicht, die Holder-Daten kommen deshalb\n"
    "         von RugCheck. Fuer eigene On-Chain-Pruefung einen Key setzen:\n"
    "         HELIUS_API_KEY=... (kostenlos) oder XENO_RPC_URL=..."
)


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


def cmd_check(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    analyzer = TokenAnalyzer(settings)

    reports = []
    for mint in args.mint:
        report = analyzer.analyze(mint, test_trade=not args.no_trade_test)
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


def cmd_screen(args: argparse.Namespace) -> int:
    settings = _apply_overrides(Settings.from_env(), args)
    candidates = Discovery().collect(
        include_new=not args.trending_only,
        include_trending=not args.new_only,
        pages=args.pages,
    )
    results = screen_all(candidates, settings.screen)
    results.sort(key=lambda r: (not r.passed, -(r.candidate.liquidity_usd or 0.0)))

    for result in results:
        if result.passed or args.verbose:
            print(format_screen_line(result))

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed} von {len(results)} Kandidaten bestehen den Vorfilter")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    settings = _apply_overrides(Settings.from_env(), args)
    scanner = Scanner(settings)

    def progress(message: str) -> None:
        if not args.json:
            print(f"  {message}", file=sys.stderr)

    result = scanner.run(
        include_new=not args.trending_only,
        include_trending=not args.new_only,
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
    from .notify import ConsoleNotifier, JsonlNotifier, MultiNotifier, TelegramNotifier

    channels = [ConsoleNotifier()]

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


def cmd_watch(args: argparse.Namespace) -> int:
    from .watcher import Watcher
    from .watchstate import WatchState

    state = WatchState(args.state_file)

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

    settings = _apply_overrides(Settings.from_env(), args)
    watcher = Watcher(settings, state=state, notifier=_build_notifier(args))
    watcher.run(
        interval=args.interval,
        budget=args.budget,
        test_trade=not args.no_trade_test,
        max_cycles=args.cycles,
    )
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


def cmd_config(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    rpc = settings.rpc_url
    if "api-key=" in rpc:  # Key nie ausgeben
        rpc = rpc.split("api-key=")[0] + "api-key=***"
    print(f"XENO {__version__}")
    print(f"  RPC              : {rpc}")
    print(f"  RPC ist oeffentl.: {settings.uses_public_rpc}")
    print(f"  RPC-Rate         : {settings.rpc_rate_limit}/s")
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

    def add_discovery(target: argparse.ArgumentParser) -> None:
        target.add_argument("--pages", type=int, default=1, help="Seiten pro Quelle (Standard 1)")
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
    add_common(p_check)
    p_check.set_defaults(func=cmd_check)

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
    p_watch.add_argument("--no-telegram", action="store_true", help="Telegram nicht nutzen")
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

    p_telegram = sub.add_parser("telegram-setup", help="Telegram einrichten und testen")
    p_telegram.add_argument("--token", help="Bot-Token (sonst aus TELEGRAM_BOT_TOKEN)")
    p_telegram.set_defaults(func=cmd_telegram_setup)

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
