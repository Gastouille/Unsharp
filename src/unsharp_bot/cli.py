"""Command-line entry point.

Sub-commands
------------
``init``      create ``.env`` and ``config/config.yaml``, interactively
``run``       start the live loop (demo or real, depending on ``XTB_MODE``)
``check``     validate the configuration and the XTB connection, then exit
``backtest``  replay historical candles from XTB or a CSV file
``scan``      one-shot scan of the current candles (no order), for a smoke test
``symbols``   list the XTB instruments matching a pattern
``endpoints`` probe the XTB gateways to find which ones answer (no credentials)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import getpass
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .broker.base import Broker, BrokerError
from .broker.paper import PaperBroker, PaperBrokerConfig
from .config import DEFAULT_CONFIG_PATH, BotConfig, ConfigError, load_config
from .engine.bot import UnsharpBot
from .journal import SignalJournal
from .logging_setup import setup_logging
from .models import Candle, SymbolSpec

LOGGER = logging.getLogger("unsharp_bot.cli")


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unsharp-bot",
        description="Unsharp Candles trading bot for XTB (xStation5 / xAPI).",
    )
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG_PATH,
        help="path to the YAML configuration (built-in defaults are used when absent)"
    )
    parser.add_argument("-e", "--env-file", default=".env", help="path to the .env file")
    parser.add_argument(
        "--set", dest="overrides", action="append", default=[], metavar="SECTION.OPTION=VALUE",
        help="override a configuration value (repeatable), e.g. --set risk.risk_fraction_per_trade=0.3",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="force DEBUG logging")

    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init", help="create .env and config/config.yaml (run this first)"
    )
    init.add_argument("--user-id", help="XTB account number (skips the prompt)")
    init.add_argument("--password", help="XTB password (prefer the prompt: this lands in your shell history)")
    init.add_argument("--mode", choices=["demo", "real"], help="demo (default) or real")
    init.add_argument(
        "--symbols", help="comma-separated symbols to scan, e.g. EURUSD,US500"
    )
    init.add_argument(
        "--non-interactive", action="store_true",
        help="never prompt; use the flags and the defaults only",
    )
    init.add_argument(
        "--force", action="store_true", help="overwrite existing files"
    )

    run = sub.add_parser("run", help="start the live trading loop")
    run.add_argument(
        "--dry-run", action="store_true",
        help="detect, size and journal everything, but never send an order",
    )

    sub.add_parser("check", help="validate the configuration and the broker connection")

    scan = sub.add_parser("scan", help="one-shot scan of the latest candles (never trades)")
    scan.add_argument("--symbol", action="append", default=[], help="restrict to these symbols")

    backtest = sub.add_parser("backtest", help="replay the strategy on historical candles")
    backtest.add_argument("--symbol", required=True, help="instrument to replay")
    backtest.add_argument("--days", type=int, default=30, help="history depth in days (XTB source)")
    backtest.add_argument("--csv", help="replay a CSV file instead of fetching from XTB")
    backtest.add_argument("--equity", type=float, default=10_000.0, help="starting equity")
    backtest.add_argument("--spread", type=float, default=0.0, help="simulated spread, in price units")
    backtest.add_argument("--slippage", type=float, default=0.0, help="simulated slippage per side")
    backtest.add_argument("--commission", type=float, default=0.0, help="commission per lot per side")
    backtest.add_argument("--no-session-filter", action="store_true",
                          help="ignore the session and timing filters")
    backtest.add_argument("--json", dest="json_out", help="write the trade list to this JSON file")

    # With --csv there is no broker to ask, so the instrument must be described.
    # Getting these wrong makes the sizing meaningless: copy them from
    # `unsharp-bot check` or `unsharp-bot symbols`.
    instrument = backtest.add_argument_group(
        "instrument specification (only used with --csv)"
    )
    instrument.add_argument("--contract-size", type=float, help="units per lot")
    instrument.add_argument("--tick-size", type=float, help="smallest price increment")
    instrument.add_argument("--tick-value", type=float,
                            help="money per tick-size move, per lot")
    instrument.add_argument("--lot-min", type=float, help="smallest tradable volume")
    instrument.add_argument("--lot-max", type=float, help="largest tradable volume")
    instrument.add_argument("--lot-step", type=float, help="volume increment")
    instrument.add_argument("--precision", type=int, help="number of price decimals")
    instrument.add_argument("--leverage", type=float,
                            help="XTB margin percentage (5 = 5%% margin = 20:1)")

    endpoints = sub.add_parser(
        "endpoints",
        help="probe the XTB gateways to see which ones answer (sends no credentials)",
    )
    endpoints.add_argument(
        "--timeout", type=float, default=10.0, help="per-probe timeout in seconds"
    )

    symbols = sub.add_parser("symbols", help="list XTB instruments")
    symbols.add_argument("--filter", default="", help="case-insensitive substring filter")
    symbols.add_argument("--limit", type=int, default=50)

    return parser


def parse_overrides(raw: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ConfigError(f"--set expects SECTION.OPTION=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip()
    return overrides


# --------------------------------------------------------------------------- #
# Broker factory
# --------------------------------------------------------------------------- #
def build_broker(config: BotConfig) -> Broker:
    """Instantiate the adapter selected by ``broker.name``."""
    if config.broker.name == "paper":
        return PaperBroker(config=PaperBrokerConfig())
    from .broker.xtb_broker import XtbBroker  # imported lazily: needs websocket-client

    return XtbBroker(config.broker)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def command_init(args: argparse.Namespace, config: BotConfig) -> int:
    """Interactive first-run setup: credentials and a strategy config file."""
    root = Path(args.config).resolve().parent.parent
    env_path = Path(args.env_file)
    config_path = Path(args.config)
    interactive = not args.non_interactive and sys.stdin.isatty()

    print("Unsharp bot - initial setup\n")

    # --- Strategy configuration ------------------------------------------- #
    template = root / "config" / "config.example.yaml"
    if not template.is_file():
        template = Path("config/config.example.yaml")
    if config_path.exists() and not args.force:
        print(f"  {config_path} already exists, left untouched (use --force to replace it)")
    elif template.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, config_path)
        print(f"  created {config_path}")
    else:
        print(f"  template {template} not found; the built-in defaults will be used")

    # --- Credentials -------------------------------------------------------- #
    existing = _read_env_file(env_path)
    explicit = bool(args.user_id or args.password or args.mode or args.symbols)
    if existing and not args.force and not interactive and not explicit:
        print(f"  {env_path} already exists, left untouched (use --force to replace it)")
        _print_init_next_steps()
        return 0

    user_id = args.user_id or existing.get("XTB_USER_ID", "")
    password = args.password or existing.get("XTB_PASSWORD", "")
    mode = args.mode or existing.get("XTB_MODE", "demo")
    symbols = args.symbols

    if interactive:
        print("\nXTB credentials. There is no separate API key: use your xStation")
        print("account number and password. Start with a DEMO account.\n")
        user_id = _ask("XTB account number (XTB_USER_ID)", user_id)
        password = _ask_secret("XTB password (XTB_PASSWORD)", password)
        mode = _ask_choice("Account type", ["demo", "real"], mode)
        if mode == "real":
            print("\n  WARNING: 'real' trades with real money.")
            print("  Test on demo for several weeks first, then start very small.\n")
        symbols = _ask(
            "Symbols to scan (comma-separated)", symbols or "EURUSD,US500,GER40"
        )

    if not user_id or not password:
        print(
            "\nNo credentials given, so .env was not written.\n"
            f"Edit {env_path} yourself, or re-run: unsharp-bot init",
            file=sys.stderr,
        )
        return 2

    values = dict(existing)
    values.update({"XTB_USER_ID": user_id, "XTB_PASSWORD": password, "XTB_MODE": mode})
    if symbols:
        values["UNSHARP_SYMBOLS"] = ",".join(
            part.strip() for part in symbols.split(",") if part.strip()
        )
    _write_env_file(env_path, values)
    print(f"\n  wrote {env_path} (readable by you only)")

    _print_init_next_steps()
    return 0


def _print_init_next_steps() -> None:
    print("\nNext steps:")
    print("  1. unsharp-bot check        verify the configuration and the connection")
    print("  2. unsharp-bot run --dry-run   detect and journal, send no order")
    print("  3. unsharp-bot run          trade the account set by XTB_MODE")
    print(
        "\nThis software is experimental and is not financial advice. "
        "Use a demo account first."
    )


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {label}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _ask_secret(label: str, default: str = "") -> str:
    suffix = " [unchanged]" if default else ""
    try:
        answer = getpass.getpass(f"  {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return answer or default


def _ask_choice(label: str, choices: list[str], default: str) -> str:
    options = "/".join(choices)
    while True:
        answer = _ask(f"{label} ({options})", default).lower()
        if answer in choices:
            return answer
        print(f"    please answer with one of: {options}")


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse an existing .env into a mapping, preserving unknown keys."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write .env with owner-only permissions (it holds a password)."""
    lines = [
        "# Unsharp bot credentials - never commit this file.",
        "# Regenerate at any time with: unsharp-bot init",
        "",
    ]
    for key in ("XTB_USER_ID", "XTB_PASSWORD", "XTB_MODE", "XTB_APP_NAME"):
        if values.get(key):
            lines.append(f"{key}={values[key]}")
    extras = {k: v for k, v in values.items() if k.startswith("UNSHARP_") and v}
    if extras:
        lines.append("")
        lines.append("# Optional overrides of config.yaml")
        lines += [f"{key}={value}" for key, value in sorted(extras.items())]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def command_run(args: argparse.Namespace, config: BotConfig) -> int:
    if args.dry_run:
        config.execution.dry_run = True
    broker = build_broker(config)
    journal = SignalJournal(config.logging, config.signals_path, config.market.timeframe_minutes)
    bot = UnsharpBot(config, broker, journal)
    try:
        bot.run()
    except KeyboardInterrupt:  # pragma: no cover
        LOGGER.info("Interrupted by the user")
    return 0


def command_check(args: argparse.Namespace, config: BotConfig) -> int:
    """Validate the config, connect, and print what the bot can see."""
    print("Configuration loaded successfully.")
    print(f"  broker           : {config.broker.name} ({config.broker.mode})")
    if config.broker.name == "xtb" and config.broker.mode == "real":
        print("  *** REAL ACCOUNT: orders here use real money. "
              "Set XTB_MODE=demo in .env for the demo account. ***")
    if config.broker.name == "xtb":
        print(f"  main socket      : {config.broker.main_endpoint}")
        print(f"  stream socket    : {config.broker.stream_endpoint}")
    print(f"  symbols          : {', '.join(config.market.symbols)}")
    print(f"  timeframe        : {config.market.timeframe_minutes} min")
    print(f"  risk fraction    : {config.risk.risk_fraction_per_trade:.0%} of available capital")
    print(f"  max loss / trade : {config.risk.max_loss_fraction_of_equity:.2%} of equity")
    print(f"  buffer           : {config.risk.buffer_fraction:.0%} of equity")
    print(f"  min R/R          : {config.unsharp.min_risk_reward}")
    print(f"  dry run          : {config.execution.dry_run}")
    for session in config.sessions:
        print(
            f"  session '{session.name}': {session.start}-{session.end} {session.timezone} "
            f"({', '.join(session.days)})"
        )

    broker = build_broker(config)
    try:
        broker.connect()
        account = broker.get_account_state()
        print("\nConnection OK.")
        print(
            f"  balance={account.balance:.2f} equity={account.equity:.2f} "
            f"margin={account.margin_used:.2f} free={account.margin_free:.2f} {account.currency}"
        )
        for symbol in config.market.symbols:
            try:
                spec = broker.get_symbol(symbol)
                quote = broker.get_quote(symbol)
                quote_text = f"bid={quote.bid} ask={quote.ask}" if quote else "no quote"
                print(
                    f"  {symbol:<10} lot[{spec.lot_min}..{spec.lot_max} step {spec.lot_step}] "
                    f"contract={spec.contract_size} tickValue={spec.tick_value} "
                    f"precision={spec.precision} leverage={spec.leverage}% {quote_text}"
                )
            except BrokerError as exc:
                print(f"  {symbol:<10} UNAVAILABLE: {exc}")
        return 0
    except BrokerError as exc:
        print(f"\nConnection FAILED: {exc}", file=sys.stderr)
        return 2
    finally:
        broker.disconnect()


def command_scan(args: argparse.Namespace, config: BotConfig) -> int:
    """Run one detection pass on the current data without trading."""
    from .strategy.indicators import atr as compute_atr
    from .strategy.levels import LevelDetector
    from .strategy.planner import TradePlanner
    from .strategy.unsharp import UnsharpDetector

    symbols = args.symbol or config.market.symbols
    broker = build_broker(config)
    broker.connect()
    level_detector = LevelDetector(config.levels)
    detector = UnsharpDetector(config.unsharp)
    planner = TradePlanner(config.unsharp)
    found = 0

    try:
        for symbol in symbols:
            try:
                spec = broker.get_symbol(symbol)
                candles = broker.get_candles(
                    symbol, config.market.timeframe_minutes,
                    count=max(config.market.warmup_candles, 200),
                )
            except BrokerError as exc:
                print(f"{symbol}: unavailable ({exc})")
                continue
            if len(candles) < 30:
                print(f"{symbol}: not enough history ({len(candles)} candles)")
                continue

            atr_value = compute_atr(candles, config.unsharp.atr_period)
            levels = level_detector.detect(candles, atr_value)
            result = detector.detect(symbol, candles, levels, atr_value)
            last = candles[-1]
            print(
                f"\n{symbol} | {len(candles)} candles | last {last.timestamp.isoformat()} "
                f"close={last.close} ATR={atr_value:.5f} | {len(levels)} levels"
            )
            for level in levels[:8]:
                print(f"    level {level.price:.5f} {level.type.value} (touches={level.touches})")
            if not result.found:
                reasons = ", ".join(sorted({r.reason for r in result.rejections})) or "no pattern"
                print(f"    -> no setup ({reasons})")
                continue

            found += 1
            setup = result.setup
            assert setup is not None
            geometry, why = planner.build(setup, levels, spec)
            print(f"    -> SETUP {setup.direction.value} on {setup.level.type.value} "
                  f"@ {setup.level.price:.5f}")
            if geometry is None:
                print(f"       not tradable: {why}")
            else:
                print(
                    f"       entry={geometry.entry_price} stop={geometry.stop_price} "
                    f"target={geometry.target_price} R/R={geometry.risk_reward:.2f}"
                    + (" (synthetic target)" if geometry.synthetic_target else "")
                )
        print(f"\n{found} setup(s) on {len(symbols)} symbol(s).")
        return 0
    finally:
        broker.disconnect()


def command_backtest(args: argparse.Namespace, config: BotConfig) -> int:
    from .backtest.runner import Backtester

    symbol = args.symbol
    if args.csv:
        candles = load_candles_csv(Path(args.csv))
        spec = build_spec_from_args(symbol, args)
        print(f"Loaded {len(candles)} candles from {args.csv}")
        print(
            f"Instrument: contract_size={spec.contract_size} tick_size={spec.tick_size} "
            f"tick_value={spec.tick_value} lot[{spec.lot_min}..{spec.lot_max} "
            f"step {spec.lot_step}] precision={spec.precision} leverage={spec.leverage}%"
        )
        if not _instrument_flags_given(args):
            print(
                "WARNING: no instrument flags given, so forex-like defaults are used.\n"
                "         Position sizing will be wrong for an index or a commodity.\n"
                "         Copy the real values from `unsharp-bot check` and pass them with\n"
                "         --contract-size --tick-size --tick-value --lot-min --lot-step "
                "--precision --leverage.",
                file=sys.stderr,
            )
    else:
        broker = build_broker(config)
        broker.connect()
        try:
            spec = broker.get_symbol(symbol)
            start = datetime.now(timezone.utc) - timedelta(days=args.days)
            candles = broker.get_candles(
                symbol, config.market.timeframe_minutes, start=start,
                end=datetime.now(timezone.utc),
            )
        finally:
            broker.disconnect()
        print(f"Fetched {len(candles)} candles for {symbol} over {args.days} day(s)")

    if len(candles) < 100:
        print("Not enough candles to backtest (need at least 100).", file=sys.stderr)
        return 2

    backtester = Backtester(
        config,
        starting_equity=args.equity,
        spread=args.spread,
        slippage=args.slippage,
        commission_per_lot=args.commission,
        apply_session_filter=not args.no_session_filter,
    )
    report = backtester.run(symbol, candles, spec)
    print(report.format_text())

    if args.json_out:
        payload = {
            "symbol": symbol,
            "summary": report.summary(),
            "trades": [trade.to_dict() for trade in report.trades],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nTrades written to {args.json_out}")

    print(
        "\nReminder: a backtest is an approximation. Spread, slippage, swaps and "
        "partial fills are simplified. Always validate on an XTB demo account before "
        "considering real money."
    )
    return 0


def command_endpoints(args: argparse.Namespace, config: BotConfig) -> int:
    """Probe every known XTB gateway and report which ones answer.

    The probe stops at the WebSocket handshake: **no credentials are ever sent**.
    Use it when a connection fails, to tell "XTB moved the endpoint" apart from
    "my network blocks it" and from "my password is wrong".
    """
    from .config import XTB_ENDPOINTS, XTB_TCP_ENDPOINTS

    timeout = max(1.0, args.timeout)
    print("Probing the XTB gateways. No credentials are sent.\n")

    # --- WebSocket gateways (what this bot uses) ------------------------- #
    print(f"{'GATEWAY':<34}{'URL':<36}RESULT")
    results: dict[str, bool] = {}
    targets: list[tuple[str, str]] = []
    for mode in ("demo", "real"):
        targets.append((f"{mode} main", XTB_ENDPOINTS[mode]["main"]))
        targets.append((f"{mode} streaming", XTB_ENDPOINTS[mode]["stream"]))
    if config.broker.uses_custom_endpoint:
        if config.broker.main_url:
            targets.append(("configured main", config.broker.main_endpoint))
        if config.broker.stream_url:
            targets.append(("configured streaming", config.broker.stream_endpoint))

    for label, url in targets:
        status = _probe_websocket(url, timeout)
        results[url] = status.startswith("OK")
        print(f"{label:<34}{url:<36}{status}")

    # --- Official TCP gateways (diagnostic only) ------------------------- #
    print(f"\n{'TCP GATEWAY (xAPI docs)':<34}{'ADDRESS':<36}RESULT")
    for mode, (host, main_port, stream_port) in XTB_TCP_ENDPOINTS.items():
        for label, port in ((f"{mode} main", main_port), (f"{mode} streaming", stream_port)):
            address = f"{host}:{port}"
            print(f"{label:<34}{address:<36}{_probe_tcp(host, port, timeout)}")

    # --- Verdict ---------------------------------------------------------- #
    working = [url for url, is_ok in results.items() if is_ok]
    print()
    if not working:
        print("No WebSocket gateway answered.")
        print("Either your network blocks them (VPN, corporate proxy, firewall),")
        print("or XTB is unreachable from here. If the TCP rows above succeeded,")
        print("the outage is specific to the WebSocket gateway.")
        return 1

    print(f"{len(working)} gateway(s) answered:")
    for url in working:
        print(f"  {url}")
    expected = (config.broker.main_endpoint, config.broker.stream_endpoint)
    broken = [url for url in expected if url in results and not results[url]]
    if broken:
        print("\nThe gateway your configuration points at did NOT answer:")
        for url in broken:
            print(f"  {url}")
        print("\nPoint the bot at a working one in config/config.yaml:")
        print("  broker:")
        print(f"    main_url:   {working[0]}")
        print("    stream_url: <the matching ...Stream URL>")
    else:
        print("\nYour configured gateways answered. A failure to connect is then")
        print("about credentials or account type, not about the endpoint.")
    return 0


def _probe_websocket(url: str, timeout: float) -> str:
    """Open and immediately close a WebSocket handshake.  Sends nothing."""
    import websocket

    try:
        connection = websocket.create_connection(url, timeout=timeout)
    except Exception as exc:
        from .broker.xtb_client import handshake_status

        status = handshake_status(exc)
        if status is not None:
            meaning = {
                404: "path not found on this host",
                403: "refused",
                401: "unauthorised",
                429: "rate limited",
            }.get(status, "")
            return f"HTTP {status}" + (f" ({meaning})" if meaning else "")
        return f"unreachable ({type(exc).__name__}: {exc})"[:70]
    try:
        connection.close()
    except Exception:
        pass
    return "OK (handshake accepted)"


def _probe_tcp(host: str, port: int, timeout: float) -> str:
    """Open a TLS connection to a raw xAPI port.  Sends nothing."""
    import socket
    import ssl

    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            context = ssl.create_default_context()
            with context.wrap_socket(raw, server_hostname=host):
                return "OK (TLS established)"
    except Exception as exc:
        return f"unreachable ({type(exc).__name__})"[:70]


def command_symbols(args: argparse.Namespace, config: BotConfig) -> int:
    broker = build_broker(config)
    broker.connect()
    try:
        specs = broker.get_all_symbols()
    finally:
        broker.disconnect()
    needle = args.filter.lower()
    matches = [
        spec for spec in specs
        if needle in spec.symbol.lower() or needle in spec.description.lower()
    ]
    matches.sort(key=lambda spec: spec.symbol)
    print(f"{len(matches)} instrument(s) matching {args.filter!r}:\n")
    print(f"{'SYMBOL':<14}{'CATEGORY':<10}{'LOT MIN':>9}{'STEP':>8}{'CONTRACT':>10}"
          f"{'LEV%':>7}  DESCRIPTION")
    for spec in matches[: args.limit]:
        print(
            f"{spec.symbol:<14}{spec.category:<10}{spec.lot_min:>9}{spec.lot_step:>8}"
            f"{spec.contract_size:>10}{spec.leverage:>7}  {spec.description[:40]}"
        )
    if len(matches) > args.limit:
        print(f"... and {len(matches) - args.limit} more (raise --limit)")
    return 0


#: Instrument fields settable from the command line, mapped to SymbolSpec.
_INSTRUMENT_FLAGS = {
    "contract_size": "contract_size",
    "tick_size": "tick_size",
    "tick_value": "tick_value",
    "lot_min": "lot_min",
    "lot_max": "lot_max",
    "lot_step": "lot_step",
    "precision": "precision",
    "leverage": "leverage",
}


def _instrument_flags_given(args: argparse.Namespace) -> bool:
    return any(getattr(args, name, None) is not None for name in _INSTRUMENT_FLAGS)


def build_spec_from_args(symbol: str, args: argparse.Namespace) -> SymbolSpec:
    """Build a :class:`SymbolSpec` from the ``--csv`` instrument flags.

    Anything not given keeps the :class:`SymbolSpec` default, which is shaped
    like a forex pair - hence the warning when nothing is specified.
    """
    overrides = {
        field: getattr(args, flag)
        for flag, field in _INSTRUMENT_FLAGS.items()
        if getattr(args, flag, None) is not None
    }
    return dataclasses.replace(SymbolSpec(symbol=symbol), **overrides)


# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #
def load_candles_csv(path: Path) -> list[Candle]:
    """Read OHLCV candles from a CSV file.

    Expected columns (case-insensitive): ``timestamp`` (ISO 8601 or epoch
    seconds), ``open``, ``high``, ``low``, ``close`` and optionally ``volume``.
    """
    if not path.is_file():
        raise ConfigError(f"CSV file not found: {path}")
    candles: list[Candle] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ConfigError(f"{path}: the CSV file has no header row")
        columns = {name.lower().strip(): name for name in reader.fieldnames}
        required = ("open", "high", "low", "close")
        missing = [name for name in required if name not in columns]
        if missing:
            raise ConfigError(f"{path}: missing column(s) {missing}")
        time_column = columns.get("timestamp") or columns.get("time") or columns.get("date")
        if time_column is None:
            raise ConfigError(f"{path}: a 'timestamp', 'time' or 'date' column is required")

        for row in reader:
            candles.append(
                Candle(
                    timestamp=_parse_timestamp(row[time_column]),
                    open=float(row[columns["open"]]),
                    high=float(row[columns["high"]]),
                    low=float(row[columns["low"]]),
                    close=float(row[columns["close"]]),
                    volume=float(row[columns["volume"]]) if "volume" in columns else 0.0,
                )
            )
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


def _parse_timestamp(raw: str) -> datetime:
    raw = raw.strip()
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        moment = datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, args.env_file, parse_overrides(args.overrides))
    except ConfigError as exc:
        # 'init' exists to create the missing configuration, and 'endpoints' is
        # the diagnostic you reach for while setting up, so neither may require a
        # complete configuration.  'endpoints' sends no credentials anyway.
        if args.command not in ("init", "endpoints"):
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        print(f"Note: {exc}\n", file=sys.stderr)
        config = BotConfig()

    if args.verbose:
        config.logging.level = "DEBUG"
    setup_logging(config.logging, config.log_path)

    handlers = {
        "init": command_init,
        "run": command_run,
        "check": command_check,
        "scan": command_scan,
        "backtest": command_backtest,
        "symbols": command_symbols,
        "endpoints": command_endpoints,
    }
    handler = handlers[args.command]
    try:
        return handler(args, config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except BrokerError as exc:
        LOGGER.error("Broker error: %s", exc)
        return 3
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
