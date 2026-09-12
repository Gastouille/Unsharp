"""Typed configuration: ``config.yaml`` for the strategy, ``.env`` for secrets.

Design rule: **no secret ever lives in the YAML file**.  Credentials are read
from the environment (optionally loaded from a ``.env`` file) so the YAML can
be committed and shared safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import time
from pathlib import Path
from typing import Any, Iterable, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

# --------------------------------------------------------------------------- #
# XTB endpoints (xStation5 / xAPI)
# --------------------------------------------------------------------------- #
XTB_ENDPOINTS: dict[str, dict[str, str]] = {
    "demo": {
        "main": "wss://ws.xtb.com/demo",
        "stream": "wss://ws.xtb.com/demoStream",
    },
    "real": {
        "main": "wss://ws.xtb.com/real",
        "stream": "wss://ws.xtb.com/realStream",
    },
}

#: XTB chart periods, in minutes.  Anything else is rejected at load time.
SUPPORTED_PERIODS: tuple[int, ...] = (1, 5, 15, 30, 60, 240, 1440, 10080, 43200)

#: Values shipped in ``.env.example``.  Detecting them turns a confusing XTB
#: login rejection into a clear "you have not entered your credentials yet".
PLACEHOLDER_CREDENTIALS = frozenset({
    "12345678",
    "your_xstation_password",
    "your_password",
    "votre_mot_de_passe",
    "changeme",
    "xxx",
})

#: Default location of the strategy configuration.  When the file is absent at
#: this exact path the built-in defaults are used instead of failing, so a fresh
#: clone can run ``unsharp-bot check`` with nothing but a ``.env``.
DEFAULT_CONFIG_PATH = "config/config.yaml"

_WEEKDAY_ALIASES: dict[str, int] = {
    "mon": 0, "monday": 0, "lun": 0, "lundi": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "mar": 1, "mardi": 1,
    "wed": 2, "wednesday": 2, "mer": 2, "mercredi": 2,
    "thu": 3, "thur": 3, "thursday": 3, "jeu": 3, "jeudi": 3,
    "fri": 4, "friday": 4, "ven": 4, "vendredi": 4,
    "sat": 5, "saturday": 5, "sam": 5, "samedi": 5,
    "sun": 6, "sunday": 6, "dim": 6, "dimanche": 6,
}


class ConfigError(ValueError):
    """Raised when the configuration is missing or inconsistent."""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BrokerConfig:
    """Broker selection and connection behaviour.

    ``name`` selects the adapter (``xtb`` or ``paper``), which keeps the rest of
    the bot independent from the actual broker implementation.
    """

    name: str = "xtb"
    mode: str = "demo"                     # demo | real  (overridden by XTB_MODE)
    user_id: str = ""                      # from XTB_USER_ID
    password: str = ""                     # from XTB_PASSWORD
    app_name: str = "unsharp-bot"
    #: Minimum delay between two commands (XTB rate-limits to ~1 req / 200 ms).
    min_request_interval: float = 0.25
    request_timeout: float = 20.0
    #: Main-socket ping period, in seconds (XTB drops idle sockets).
    ping_interval: float = 150.0
    reconnect_max_attempts: int = 8
    reconnect_base_delay: float = 2.0
    reconnect_max_delay: float = 60.0
    order_retry_attempts: int = 4
    order_retry_base_delay: float = 1.5
    use_streaming: bool = True

    @property
    def main_url(self) -> str:
        return XTB_ENDPOINTS[self.mode]["main"]

    @property
    def stream_url(self) -> str:
        return XTB_ENDPOINTS[self.mode]["stream"]

    def validate(self) -> None:
        if self.name not in ("xtb", "paper"):
            raise ConfigError(f"broker.name must be 'xtb' or 'paper', got {self.name!r}")
        if self.mode not in XTB_ENDPOINTS:
            raise ConfigError(f"broker.mode must be 'demo' or 'real', got {self.mode!r}")
        if self.name != "xtb":
            return
        if not (self.user_id and self.password):
            raise ConfigError(
                "Missing XTB credentials. Run 'unsharp-bot init', or set XTB_USER_ID "
                "and XTB_PASSWORD in your .env (see .env.example)."
            )
        placeholders = [
            name
            for name, value in (("XTB_USER_ID", self.user_id), ("XTB_PASSWORD", self.password))
            if value.strip().lower() in PLACEHOLDER_CREDENTIALS
        ]
        if placeholders:
            verb = "still hold" if len(placeholders) > 1 else "still holds"
            raise ConfigError(
                f"{' and '.join(placeholders)} {verb} the example value from "
                ".env.example. Run 'unsharp-bot init' to enter your real XTB demo "
                "credentials."
            )


@dataclass(slots=True)
class SessionWindow:
    """A daily trading window expressed in a named timezone."""

    name: str = "session"
    start: str = "09:30"
    end: str = "17:30"
    timezone: str = "Europe/Paris"
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    #: Restrict entries to the first N minutes after the open (0 = no restriction).
    opening_window_minutes: int = 0
    #: Flatten every position belonging to this session N minutes before the close.
    flatten_before_close_minutes: int = 5

    def __post_init__(self) -> None:
        self.start_time = _parse_hhmm(self.start, f"sessions.{self.name}.start")
        self.end_time = _parse_hhmm(self.end, f"sessions.{self.name}.end")
        try:
            self.tzinfo = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - env dependent
            raise ConfigError(f"Unknown timezone {self.timezone!r} in session {self.name!r}") from exc
        self.weekdays = frozenset(_parse_weekday(d, self.name) for d in self.days)

    # Attributes computed in __post_init__ (declared for ``slots=True``).
    start_time: time = field(init=False, default=time(9, 30))
    end_time: time = field(init=False, default=time(17, 30))
    tzinfo: Any = field(init=False, default=None)
    weekdays: frozenset[int] = field(init=False, default=frozenset())

    @property
    def crosses_midnight(self) -> bool:
        return self.end_time <= self.start_time


@dataclass(slots=True)
class TimingConfig:
    """'Trade when the market is active' filter from the Unsharp method."""

    enabled: bool = True
    #: Always allow entries during the first N minutes of a session.
    opening_minutes: int = 60
    #: Also allow entries around clock pivots every N minutes (30 or 60).
    pivot_period_minutes: int = 30
    #: Tolerance around each pivot, in minutes.
    pivot_window_minutes: int = 10
    #: Blackout windows "HH:MM-HH:MM" (session timezone), e.g. lunch time.
    blackout_windows: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MarketConfig:
    """What to scan and with which bars."""

    symbols: list[str] = field(default_factory=lambda: ["EURUSD", "US500", "GER40"])
    timeframe_minutes: int = 5
    #: How many closed candles to keep in memory per symbol.
    history_size: int = 400
    #: Bars pulled at startup to warm up indicators and levels.
    warmup_candles: int = 300
    #: Pull daily bars to build previous-day high/low/close levels.
    use_daily_levels: bool = True
    #: Seconds to wait after a candle boundary before polling (server-side lag).
    poll_offset_seconds: float = 4.0
    #: Skip symbols the broker reports as not tradable.
    skip_disabled_symbols: bool = True

    def validate(self) -> None:
        if self.timeframe_minutes not in SUPPORTED_PERIODS:
            raise ConfigError(
                f"market.timeframe_minutes={self.timeframe_minutes} is not an XTB period "
                f"(allowed: {', '.join(map(str, SUPPORTED_PERIODS))})"
            )
        if not self.symbols:
            raise ConfigError("market.symbols is empty: nothing to scan")


@dataclass(slots=True)
class LevelsConfig:
    """Key-level detection parameters."""

    #: Fractal window: a swing needs N lower highs on each side.
    swing_lookback: int = 3
    #: Number of candles scanned when building the level map.
    scan_window: int = 150
    #: Levels closer than ``cluster_atr * ATR`` are merged into a single zone.
    cluster_atr: float = 0.5
    #: Zone half-width, as a multiple of ATR.
    zone_width_atr: float = 0.25
    #: Keep at most N levels (strongest first) to bound the search.
    max_levels: int = 40
    include_recent_extremes: bool = True
    include_session_extremes: bool = True
    include_previous_day: bool = True
    include_round_numbers: bool = False
    #: Spacing of psychological levels, in price units (0 = auto from precision).
    round_number_step: float = 0.0
    #: A level must have been touched at least this many times to be kept.
    min_touches: int = 1


@dataclass(slots=True)
class UnsharpConfig:
    """Thresholds of the Lead / Confirmation / Execution detector."""

    # --- Lead Candle --------------------------------------------------------
    #: Body of the Lead >= this multiple of the ATR.
    lead_min_body_atr: float = 0.8
    #: Body of the Lead >= this multiple of the mean body of the lookback window.
    lead_min_body_ratio: float = 1.5
    #: Body/range of the Lead (a strong directional candle has little wick).
    lead_min_body_to_range: float = 0.5
    #: Window used for the "mean body" reference.
    body_average_window: int = 20

    # --- Confirmation zone --------------------------------------------------
    min_confirmation_candles: int = 1
    max_confirmation_candles: int = 5
    #: Mean body of the zone <= this fraction of the Lead body.
    confirmation_max_body_ratio: float = 0.55
    #: At least one zone candle must show a rejection wick >= this share of its range.
    confirmation_min_wick_ratio: float = 0.30
    #: Fraction of zone candles that must show that rejection wick.
    confirmation_min_wick_share: float = 0.34
    #: How far the zone may extend past the Lead extreme, in ATR (the sweep).
    confirmation_max_overshoot_atr: float = 0.35
    #: Total height of the zone <= this multiple of the Lead range ("mâchouillage").
    confirmation_max_zone_to_lead_range: float = 0.85

    # --- Execution Candle ---------------------------------------------------
    #: Body of the Execution >= this multiple of the ATR.
    execution_min_body_atr: float = 0.35
    #: Body/range of the Execution candle.
    execution_min_body_to_range: float = 0.45
    #: Close must sit in the top (long) / bottom (short) share of its own range.
    execution_min_close_position: float = 0.60
    #: Execution must close beyond the extreme of the confirmation zone.
    execution_must_break_zone: bool = True
    #: Execution close beyond the zone extreme by at least this much ATR.
    execution_min_breakout_atr: float = 0.0

    # --- Level anchoring ----------------------------------------------------
    #: Max distance between the zone extreme and the level, in ATR.
    level_tolerance_atr: float = 0.60
    #: If False, a setup without a level is still emitted (NOT recommended).
    require_level: bool = True

    # --- Trade construction -------------------------------------------------
    #: "confirmation_zone" (per the method) or "lead_and_zone" (more conservative).
    stop_reference: str = "confirmation_zone"
    #: Extra room below/above the wicks, in ATR.
    stop_buffer_atr: float = 0.15
    min_risk_reward: float = 2.0
    #: Target search: keep levels no further than this multiple of the risk.
    max_target_rr: float = 8.0
    #: If no level offers ``min_risk_reward``, use entry + min_rr * risk instead.
    allow_synthetic_target: bool = True
    #: Minimum stop distance in ATR (guards against a stop glued to the entry).
    min_stop_distance_atr: float = 0.15
    #: Entry convention: "execution_close" or "next_open".
    entry_mode: str = "execution_close"
    #: ATR period.
    atr_period: int = 14

    def validate(self) -> None:
        if self.min_confirmation_candles < 1:
            raise ConfigError("unsharp.min_confirmation_candles must be >= 1")
        if self.max_confirmation_candles < self.min_confirmation_candles:
            raise ConfigError(
                "unsharp.max_confirmation_candles must be >= min_confirmation_candles"
            )
        if self.stop_reference not in ("confirmation_zone", "lead_and_zone"):
            raise ConfigError(
                "unsharp.stop_reference must be 'confirmation_zone' or 'lead_and_zone'"
            )
        if self.entry_mode not in ("execution_close", "next_open"):
            raise ConfigError("unsharp.entry_mode must be 'execution_close' or 'next_open'")
        if self.min_risk_reward <= 0:
            raise ConfigError("unsharp.min_risk_reward must be > 0")
        if self.atr_period < 2:
            raise ConfigError("unsharp.atr_period must be >= 2")


@dataclass(slots=True)
class RiskConfig:
    """Progressive money management + hard risk guards."""

    #: Fraction of the *available capital* committed on each trade.
    risk_fraction_per_trade: float = 0.5
    #: Hard cap: money lost if the stop is hit, as a fraction of equity.
    max_loss_fraction_of_equity: float = 0.02
    #: Safety buffer kept away from the margin call, as a fraction of equity.
    buffer_fraction: float = 0.10
    #: What ``risk_fraction_per_trade`` applies to:
    #:   "margin"   -> the committed margin (default, conservative)
    #:   "exposure" -> the notional contract value
    notional_basis: str = "margin"
    #: Ask the broker for the exact margin of the computed volume.
    use_broker_margin_check: bool = True
    #: Never let the required margin exceed this share of the available capital.
    max_margin_fraction_of_capital: float = 1.0

    # --- Portfolio guards ---------------------------------------------------
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_trades_per_day: int = 10
    #: Stop trading for the day after this cumulated loss (fraction of start equity).
    daily_loss_limit_fraction: float = 0.06
    #: Stop trading for the day after this cumulated gain (0 = disabled).
    daily_profit_target_fraction: float = 0.0
    #: Minutes to wait after a losing trade on a symbol before re-entering it.
    cooldown_minutes_after_loss: int = 30
    #: Minutes to wait after any trade on a symbol (avoids stacking the same setup).
    cooldown_minutes_after_trade: int = 0
    #: Refuse to trade when equity falls below this fraction of the session start.
    min_equity_fraction: float = 0.5

    def validate(self) -> None:
        if not 0 < self.risk_fraction_per_trade <= 1:
            raise ConfigError("risk.risk_fraction_per_trade must be in ]0, 1]")
        if not 0 < self.max_loss_fraction_of_equity <= 1:
            raise ConfigError("risk.max_loss_fraction_of_equity must be in ]0, 1]")
        if not 0 <= self.buffer_fraction < 1:
            raise ConfigError("risk.buffer_fraction must be in [0, 1[")
        if self.notional_basis not in ("margin", "exposure"):
            raise ConfigError("risk.notional_basis must be 'margin' or 'exposure'")
        if self.max_open_positions < 1:
            raise ConfigError("risk.max_open_positions must be >= 1")


@dataclass(slots=True)
class ExecutionConfig:
    """How orders are sent and managed."""

    #: Never send an order: detect, size, journal.  Perfect for a first run.
    dry_run: bool = False
    #: Attach SL/TP to the order itself (recommended: survives a bot crash).
    attach_sl_tp: bool = True
    #: Max slippage tolerated between the planned entry and the live price, in ATR.
    max_entry_slippage_atr: float = 0.5
    #: Move the stop to break-even once price has travelled N * risk in our favour.
    breakeven_at_r: float = 0.0
    #: Trail the stop by N * ATR once break-even is reached (0 = disabled).
    trailing_atr: float = 0.0
    #: Close every position when the session ends.
    flatten_at_session_end: bool = True
    #: Comment attached to every order (helps reconcile in xStation).
    order_comment_prefix: str = "UNSHARP"


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    directory: str = "data/logs"
    filename: str = "unsharp-bot.log"
    #: Daily rotation, keeping N files.
    backup_count: int = 30
    console: bool = True
    #: Directory of the daily signal journals.
    signals_directory: str = "data/signals"
    #: Also mirror the journal as CSV.
    write_csv: bool = True
    #: Append every state change to a JSON-lines audit trail.
    write_jsonl_audit: bool = True


@dataclass(slots=True)
class BotConfig:
    """Root configuration object."""

    broker: BrokerConfig = field(default_factory=BrokerConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    sessions: list[SessionWindow] = field(default_factory=lambda: [SessionWindow()])
    timing: TimingConfig = field(default_factory=TimingConfig)
    levels: LevelsConfig = field(default_factory=LevelsConfig)
    unsharp: UnsharpConfig = field(default_factory=UnsharpConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    #: Root used to resolve every relative path in the config.
    base_path: Path = field(default_factory=Path.cwd)

    def validate(self) -> None:
        self.broker.validate()
        self.market.validate()
        self.unsharp.validate()
        self.risk.validate()
        if not self.sessions:
            raise ConfigError("At least one trading session must be configured")

    # -- path helpers ------------------------------------------------------- #
    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else self.base_path / path

    @property
    def log_path(self) -> Path:
        return self.resolve(self.logging.directory) / self.logging.filename

    @property
    def signals_path(self) -> Path:
        return self.resolve(self.logging.signals_directory)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _parse_hhmm(value: str, where: str) -> time:
    try:
        hours, minutes = str(value).strip().split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{where}: expected 'HH:MM', got {value!r}") from exc


def _parse_weekday(value: Any, where: str) -> int:
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ConfigError(f"sessions.{where}: weekday index must be in [0, 6]")
    key = str(value).strip().lower()
    if key not in _WEEKDAY_ALIASES:
        raise ConfigError(f"sessions.{where}: unknown weekday {value!r}")
    return _WEEKDAY_ALIASES[key]


def parse_time_window(window: str, where: str = "window") -> tuple[time, time]:
    """Parse a ``"HH:MM-HH:MM"`` string into a pair of :class:`datetime.time`."""
    parts = str(window).split("-")
    if len(parts) != 2:
        raise ConfigError(f"{where}: expected 'HH:MM-HH:MM', got {window!r}")
    return _parse_hhmm(parts[0], where), _parse_hhmm(parts[1], where)


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort coercion of a YAML scalar to the dataclass field type."""
    origin = get_origin(target_type)
    if origin is list:
        (item_type,) = get_args(target_type) or (Any,)
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
            value = [value]
        return [_coerce(v, item_type) for v in value]
    if target_type in (float, "float") and value is not None:
        return float(value)
    if target_type in (int, "int") and value is not None and not isinstance(value, bool):
        return int(value)
    if target_type in (bool, "bool"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if target_type in (str, "str") and value is not None:
        return str(value)
    return value


def _build_section(cls: type, data: Any, where: str) -> Any:
    """Instantiate dataclass ``cls`` from a YAML mapping, rejecting unknown keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    known = {f.name: f for f in fields(cls) if f.init}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{where}: unknown option(s) {sorted(unknown)}")
    kwargs = {name: _coerce(value, known[name].type) for name, value in data.items()}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_float(name: str) -> float | None:
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name}={raw!r} is not a number") from exc


def _env_bool(name: str) -> bool | None:
    raw = _env_str(name)
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes", "on")


def load_dotenv(path: str | Path = ".env", override: bool = False) -> None:
    """Minimal ``.env`` loader (keeps ``python-dotenv`` optional).

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments and quoted values.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def apply_env_overrides(config: BotConfig) -> BotConfig:
    """Overlay environment variables on top of the YAML configuration.

    Secrets are *only* read here; the YAML file never needs to contain them.
    """
    config.broker.user_id = _env_str("XTB_USER_ID", config.broker.user_id) or ""
    config.broker.password = _env_str("XTB_PASSWORD", config.broker.password) or ""
    config.broker.mode = (_env_str("XTB_MODE", config.broker.mode) or "demo").lower()
    config.broker.app_name = _env_str("XTB_APP_NAME", config.broker.app_name) or "unsharp-bot"

    broker_name = _env_str("UNSHARP_BROKER")
    if broker_name:
        config.broker.name = broker_name.lower()

    dry_run = _env_bool("UNSHARP_DRY_RUN")
    if dry_run is not None:
        config.execution.dry_run = dry_run

    risk_fraction = _env_float("UNSHARP_RISK_FRACTION")
    if risk_fraction is not None:
        config.risk.risk_fraction_per_trade = risk_fraction

    max_loss = _env_float("UNSHARP_MAX_LOSS_FRACTION")
    if max_loss is not None:
        config.risk.max_loss_fraction_of_equity = max_loss

    buffer_fraction = _env_float("UNSHARP_BUFFER_FRACTION")
    if buffer_fraction is not None:
        config.risk.buffer_fraction = buffer_fraction

    symbols = _env_str("UNSHARP_SYMBOLS")
    if symbols:
        config.market.symbols = [s.strip() for s in symbols.split(",") if s.strip()]

    log_level = _env_str("UNSHARP_LOG_LEVEL")
    if log_level:
        config.logging.level = log_level.upper()

    return config


def load_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    env_file: str | Path | None = ".env",
    overrides: dict[str, Any] | None = None,
    allow_missing: bool | None = None,
) -> BotConfig:
    """Load ``config.yaml``, overlay ``.env`` and CLI overrides, then validate.

    When ``path`` is the default location and the file does not exist, the
    built-in defaults are used: a fresh clone only needs credentials to run.
    An explicitly requested file that is missing is still an error, because
    that is almost always a typo.  ``allow_missing`` overrides the heuristic.
    """
    config_path = Path(path)
    if allow_missing is None:
        allow_missing = Path(path) == Path(DEFAULT_CONFIG_PATH)

    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{config_path}: the root of the YAML file must be a mapping")
        base_path = config_path.resolve().parent.parent
    elif allow_missing:
        raw = {}
        base_path = Path.cwd()
    else:
        raise ConfigError(
            f"Configuration file not found: {config_path}. "
            "Run 'unsharp-bot init' to create one, or copy "
            "config/config.example.yaml to config/config.yaml."
        )

    if env_file is not None:
        load_dotenv(env_file)

    known_sections = {
        "broker": BrokerConfig,
        "market": MarketConfig,
        "timing": TimingConfig,
        "levels": LevelsConfig,
        "unsharp": UnsharpConfig,
        "risk": RiskConfig,
        "execution": ExecutionConfig,
        "logging": LoggingConfig,
    }
    unknown = set(raw) - set(known_sections) - {"sessions", "base_path"}
    if unknown:
        raise ConfigError(f"Unknown configuration section(s): {sorted(unknown)}")

    sections: dict[str, Any] = {
        name: _build_section(cls, raw.get(name), name)
        for name, cls in known_sections.items()
    }

    raw_sessions = raw.get("sessions") or []
    if not isinstance(raw_sessions, list):
        raise ConfigError("sessions: expected a list of session windows")
    sessions = [
        _build_section(SessionWindow, item, f"sessions[{i}]")
        for i, item in enumerate(raw_sessions)
    ] or [SessionWindow()]

    config = BotConfig(
        sessions=sessions,
        base_path=base_path,
        **sections,
    )
    apply_env_overrides(config)

    for dotted, value in (overrides or {}).items():
        _apply_override(config, dotted, value)

    config.validate()
    return config


def _apply_override(config: BotConfig, dotted: str, value: Any) -> None:
    """Apply a ``section.option=value`` CLI override onto the config tree."""
    parts = dotted.split(".")
    target: Any = config
    for part in parts[:-1]:
        if not hasattr(target, part):
            raise ConfigError(f"--set {dotted}: unknown section {part!r}")
        target = getattr(target, part)
    leaf = parts[-1]
    if not is_dataclass(target) or not hasattr(target, leaf):
        raise ConfigError(f"--set {dotted}: unknown option {leaf!r}")
    field_types = {f.name: f.type for f in fields(target)}
    setattr(target, leaf, _coerce(value, field_types[leaf]))
