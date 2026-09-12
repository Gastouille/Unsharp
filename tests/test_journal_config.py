"""Signal journal persistence and configuration loading."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from unsharp_bot.config import (
    ConfigError,
    LoggingConfig,
    UnsharpConfig,
    apply_env_overrides,
    load_config,
    load_dotenv,
    BotConfig,
)
from unsharp_bot.journal import SignalJournal
from unsharp_bot.models import Direction, SignalStatus, TradePlan
from unsharp_bot.strategy.indicators import atr
from unsharp_bot.strategy.levels import LevelDetector
from unsharp_bot.strategy.unsharp import UnsharpDetector
from unsharp_bot.config import LevelsConfig

from .conftest import long_setup_series


def make_setup():
    candles = long_setup_series()
    atr_value = atr(candles, 14)
    levels = LevelDetector(LevelsConfig()).detect(candles, atr_value)
    result = UnsharpDetector(UnsharpConfig()).detect("US500", candles, levels, atr_value)
    assert result.found
    return result.setup


@pytest.fixture
def journal(tmp_path) -> SignalJournal:
    return SignalJournal(LoggingConfig(), tmp_path, timeframe_minutes=5)


def test_journal_writes_json_and_csv(journal, tmp_path):
    setup = make_setup()
    record = journal.record_detection(setup, session="us")
    day = setup.timestamp.date()

    json_path = tmp_path / f"signals-{day.isoformat()}.json"
    csv_path = tmp_path / f"signals-{day.isoformat()}.csv"
    assert json_path.is_file() and csv_path.is_file()

    payload = json.loads(json_path.read_text())
    assert payload["count"] == 1
    entry = payload["signals"][0]
    assert entry["symbol"] == "US500"
    assert entry["direction"] == "LONG"
    assert entry["status"] == "DETECTED"
    assert entry["level_used"]["type"]
    assert entry["session"] == "us"
    assert entry["timeframe_minutes"] == 5
    assert "US500" in csv_path.read_text()
    assert record.signal_id.startswith("US500-")


def test_journal_full_lifecycle(journal, tmp_path):
    setup = make_setup()
    record = journal.record_detection(setup)
    plan = TradePlan(
        setup=setup,
        entry_price=99.05,
        stop_price=97.78,
        target_price=101.59,
        volume=0.15,
        risk_reward=2.0,
        risk_amount=190.5,
        notional_engaged=5_000.0,
        capital_available=10_000.0,
        risk_fraction_used=0.5,
        margin_required=742.0,
    )
    journal.attach_plan(record, plan)
    journal.update_status(record, SignalStatus.OPEN, position_id=42)
    journal.update_status(
        record, SignalStatus.CLOSED, reason="take_profit", pnl=381.0, close_price=101.59
    )

    payload = json.loads((tmp_path / f"signals-{setup.timestamp.date()}.json").read_text())
    entry = payload["signals"][0]
    assert entry["status"] == "CLOSED"
    assert entry["pnl"] == 381.0
    assert entry["close_reason"] == "take_profit"
    assert entry["volume"] == 0.15
    assert entry["risk_fraction_used"] == 0.5
    assert entry["capital_available_before_trade"] == 10_000.0
    assert entry["notional_engaged"] == 5_000.0

    # The audit trail records every state change.
    audit = (tmp_path / f"audit-{setup.timestamp.date()}.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in audit] == [
        "detected", "planned", "open", "closed",
    ]


def test_journal_reloads_an_existing_day(tmp_path):
    setup = make_setup()
    first = SignalJournal(LoggingConfig(), tmp_path, 5)
    record = first.record_detection(setup)
    first.update_status(record, SignalStatus.OPEN, position_id=7)

    # A restart must recover the day's records (and find the open position).
    second = SignalJournal(LoggingConfig(), tmp_path, 5)
    second.record_detection(setup)          # triggers the day load
    assert second.find_by_position(7) is not None
    assert len(second.open_records()) >= 1


def test_journal_rejection_is_recorded(journal, tmp_path):
    moment = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
    journal.record_rejection("EURUSD", moment, Direction.SHORT, "cooldown_after_loss")
    payload = json.loads((tmp_path / "signals-2026-09-14.json").read_text())
    entry = payload["signals"][0]
    assert entry["status"] == "REJECTED"
    assert entry["rejection_reason"] == "cooldown_after_loss"


def test_daily_summary(journal):
    setup = make_setup()
    record = journal.record_detection(setup)
    journal.update_status(record, SignalStatus.OPEN, position_id=1)
    journal.update_status(record, SignalStatus.CLOSED, reason="take_profit", pnl=120.0)
    summary = journal.daily_summary()
    assert summary["closed"] == 1
    assert summary["wins"] == 1
    assert summary["realised_pnl"] == 120.0


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_load_example_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XTB_USER_ID", "999")
    monkeypatch.setenv("XTB_PASSWORD", "secret")
    config = load_config("config/config.example.yaml", env_file=None)
    assert config.broker.user_id == "999"
    assert config.broker.mode == "demo"
    assert config.broker.main_url == "wss://ws.xtb.com/demo"
    assert config.risk.risk_fraction_per_trade == 0.5
    assert len(config.sessions) == 2


def test_real_mode_switches_the_endpoints(monkeypatch):
    monkeypatch.setenv("XTB_USER_ID", "999")
    monkeypatch.setenv("XTB_PASSWORD", "secret")
    monkeypatch.setenv("XTB_MODE", "real")
    config = load_config("config/config.example.yaml", env_file=None)
    assert config.broker.main_url == "wss://ws.xtb.com/real"
    assert config.broker.stream_url == "wss://ws.xtb.com/realStream"


def test_missing_credentials_are_rejected(monkeypatch):
    monkeypatch.delenv("XTB_USER_ID", raising=False)
    monkeypatch.delenv("XTB_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="credentials"):
        load_config("config/config.example.yaml", env_file=None)


def test_cli_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("XTB_USER_ID", "999")
    monkeypatch.setenv("XTB_PASSWORD", "secret")
    config = load_config(
        "config/config.example.yaml",
        env_file=None,
        overrides={"risk.risk_fraction_per_trade": "0.25", "execution.dry_run": "true"},
    )
    assert config.risk.risk_fraction_per_trade == 0.25
    assert config.execution.dry_run is True


def test_unknown_option_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XTB_USER_ID", "999")
    monkeypatch.setenv("XTB_PASSWORD", "secret")
    bad = tmp_path / "config" / "bad.yaml"
    bad.parent.mkdir()
    bad.write_text("risk:\n  typo_option: 1\n")
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(bad, env_file=None)


def test_invalid_timeframe_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XTB_USER_ID", "999")
    monkeypatch.setenv("XTB_PASSWORD", "secret")
    bad = tmp_path / "config" / "bad.yaml"
    bad.parent.mkdir()
    bad.write_text("market:\n  timeframe_minutes: 7\n")
    with pytest.raises(ConfigError, match="XTB period"):
        load_config(bad, env_file=None)


def test_invalid_risk_fraction_is_rejected():
    config = BotConfig()
    config.broker.name = "paper"
    config.risk.risk_fraction_per_trade = 1.5
    with pytest.raises(ConfigError, match="risk_fraction_per_trade"):
        config.validate()


def test_dotenv_loader(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# a comment\nexport XTB_USER_ID=4242\nXTB_PASSWORD="quoted secret"\n\nGARBAGE\n'
    )
    monkeypatch.delenv("XTB_USER_ID", raising=False)
    monkeypatch.delenv("XTB_PASSWORD", raising=False)
    load_dotenv(env_file)
    assert os.environ["XTB_USER_ID"] == "4242"
    assert os.environ["XTB_PASSWORD"] == "quoted secret"


def test_env_overrides_symbols_and_risk(monkeypatch):
    monkeypatch.setenv("XTB_USER_ID", "1")
    monkeypatch.setenv("XTB_PASSWORD", "2")
    monkeypatch.setenv("UNSHARP_SYMBOLS", "EURUSD, GER40")
    monkeypatch.setenv("UNSHARP_RISK_FRACTION", "0.3")
    monkeypatch.setenv("UNSHARP_DRY_RUN", "true")
    config = apply_env_overrides(BotConfig())
    assert config.market.symbols == ["EURUSD", "GER40"]
    assert config.risk.risk_fraction_per_trade == 0.3
    assert config.execution.dry_run is True
