"""Installation ergonomics: optional config file, placeholder guard, init wizard."""

from __future__ import annotations

import argparse
import os

import pytest

from unsharp_bot.cli import (
    _read_env_file,
    _write_env_file,
    build_parser,
    command_init,
    main,
)
from unsharp_bot.config import (
    DEFAULT_CONFIG_PATH,
    BrokerConfig,
    ConfigError,
    load_config,
)


# --------------------------------------------------------------------------- #
# The configuration file is optional
# --------------------------------------------------------------------------- #
def test_missing_config_at_the_default_path_uses_defaults(tmp_path, monkeypatch):
    """A fresh clone runs with credentials only: no YAML file required."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XTB_USER_ID", "4242")
    monkeypatch.setenv("XTB_PASSWORD", "a-real-password")
    config = load_config(env_file=None)
    assert config.market.symbols          # built-in defaults
    assert config.base_path == tmp_path


def test_missing_config_at_an_explicit_path_still_fails(tmp_path, monkeypatch):
    """An explicit -c that does not exist is a typo, not an invitation."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XTB_USER_ID", "4242")
    monkeypatch.setenv("XTB_PASSWORD", "a-real-password")
    with pytest.raises(ConfigError, match="not found"):
        load_config("somewhere/else.yaml", env_file=None)


def test_allow_missing_can_be_forced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XTB_USER_ID", "4242")
    monkeypatch.setenv("XTB_PASSWORD", "a-real-password")
    config = load_config("elsewhere.yaml", env_file=None, allow_missing=True)
    assert config.risk.risk_fraction_per_trade == 0.5


def test_default_config_path_constant_matches_the_cli_default():
    parser = build_parser()
    args = parser.parse_args(["check"])
    assert args.config == DEFAULT_CONFIG_PATH


# --------------------------------------------------------------------------- #
# Placeholder credentials
# --------------------------------------------------------------------------- #
def test_placeholder_credentials_are_rejected_with_a_clear_message():
    config = BrokerConfig(user_id="12345678", password="your_xstation_password")
    with pytest.raises(ConfigError, match="unsharp-bot init"):
        config.validate()


def test_a_single_placeholder_is_named():
    config = BrokerConfig(user_id="4242", password="your_xstation_password")
    with pytest.raises(ConfigError, match="XTB_PASSWORD still holds"):
        config.validate()


def test_real_credentials_pass():
    BrokerConfig(user_id="4242", password="a-real-password").validate()


def test_paper_broker_needs_no_credentials():
    BrokerConfig(name="paper", user_id="", password="").validate()


# --------------------------------------------------------------------------- #
# .env round trip
# --------------------------------------------------------------------------- #
def test_env_file_round_trip(tmp_path):
    path = tmp_path / ".env"
    _write_env_file(path, {
        "XTB_USER_ID": "4242",
        "XTB_PASSWORD": "secret",
        "XTB_MODE": "demo",
        "UNSHARP_SYMBOLS": "EURUSD,US500",
    })
    values = _read_env_file(path)
    assert values["XTB_USER_ID"] == "4242"
    assert values["XTB_PASSWORD"] == "secret"
    assert values["UNSHARP_SYMBOLS"] == "EURUSD,US500"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_env_file_is_owner_readable_only(tmp_path):
    """The file holds a trading password: it must not be world-readable."""
    path = tmp_path / ".env"
    _write_env_file(path, {"XTB_USER_ID": "1", "XTB_PASSWORD": "s"})
    assert path.stat().st_mode & 0o077 == 0


def test_read_env_file_ignores_comments_and_exports(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# note\nexport XTB_USER_ID=7\nXTB_PASSWORD="quoted"\nJUNK\n')
    values = _read_env_file(path)
    assert values == {"XTB_USER_ID": "7", "XTB_PASSWORD": "quoted"}


def test_read_env_file_on_a_missing_file(tmp_path):
    assert _read_env_file(tmp_path / "absent") == {}


# --------------------------------------------------------------------------- #
# The init command
# --------------------------------------------------------------------------- #
def init_args(tmp_path, **overrides) -> argparse.Namespace:
    defaults = dict(
        config=str(tmp_path / "config" / "config.yaml"),
        env_file=str(tmp_path / ".env"),
        user_id=None, password=None, mode=None, symbols=None,
        non_interactive=True, force=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_init_writes_credentials(tmp_path, capsys):
    args = init_args(tmp_path, user_id="4242", password="secret", mode="demo",
                     symbols="EURUSD, US500")
    assert command_init(args, None) == 0
    values = _read_env_file(tmp_path / ".env")
    assert values["XTB_USER_ID"] == "4242"
    assert values["XTB_MODE"] == "demo"
    # Whitespace in the symbol list is cleaned up.
    assert values["UNSHARP_SYMBOLS"] == "EURUSD,US500"


def test_init_refuses_to_write_without_credentials(tmp_path):
    assert command_init(init_args(tmp_path), None) == 2
    assert not (tmp_path / ".env").exists()


def test_init_keeps_an_existing_env_when_nothing_is_given(tmp_path):
    env_path = tmp_path / ".env"
    _write_env_file(env_path, {"XTB_USER_ID": "old", "XTB_PASSWORD": "old-secret"})
    assert command_init(init_args(tmp_path), None) == 0
    assert _read_env_file(env_path)["XTB_USER_ID"] == "old"


def test_init_explicit_flags_update_an_existing_env(tmp_path):
    """Passing a flag means "change this", even when the file already exists."""
    env_path = tmp_path / ".env"
    _write_env_file(env_path, {"XTB_USER_ID": "old", "XTB_PASSWORD": "old-secret"})
    assert command_init(init_args(tmp_path, user_id="new"), None) == 0
    values = _read_env_file(env_path)
    assert values["XTB_USER_ID"] == "new"
    # The password we did not touch is preserved.
    assert values["XTB_PASSWORD"] == "old-secret"


def test_init_runs_even_when_the_configuration_is_incomplete(tmp_path, monkeypatch, capsys):
    """`init` must work on a machine with no .env at all: that is its job."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XTB_USER_ID", raising=False)
    monkeypatch.delenv("XTB_PASSWORD", raising=False)
    code = main([
        "-e", str(tmp_path / ".env"),
        "init", "--non-interactive", "--user-id", "4242", "--password", "secret",
    ])
    assert code == 0
    assert _read_env_file(tmp_path / ".env")["XTB_USER_ID"] == "4242"


def test_init_does_not_overwrite_a_config_file(tmp_path):
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("market:\n  timeframe_minutes: 15\n")
    command_init(init_args(tmp_path, user_id="1", password="2"), None)
    assert "timeframe_minutes: 15" in config_path.read_text()
