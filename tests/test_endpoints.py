"""Gateway endpoints: overrides, failure classification and fail-fast behaviour."""

from __future__ import annotations


import pytest

from unsharp_bot.broker import xtb_client
from unsharp_bot.broker.xtb_client import (
    XtbClient,
    XtbEndpointError,
    explain_endpoint_failure,
    handshake_status,
    is_permanent_handshake_failure,
)
from unsharp_bot.config import (
    XTB_ENDPOINTS,
    XTB_TCP_ENDPOINTS,
    BrokerConfig,
    ConfigError,
)


class FakeHandshakeError(Exception):
    """Stands in for websocket.WebSocketBadStatusException, which carries a status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Handshake status {status_code}")
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_4xx_handshakes_are_permanent(status):
    assert is_permanent_handshake_failure(FakeHandshakeError(status))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_timeouts_rate_limits_and_5xx_are_transient(status):
    """These do clear up on their own, so they are worth retrying."""
    assert not is_permanent_handshake_failure(FakeHandshakeError(status))


def test_a_plain_network_error_is_transient():
    assert not is_permanent_handshake_failure(OSError("connection reset"))
    assert handshake_status(OSError("connection reset")) is None


def test_404_explanation_is_actionable():
    message = explain_endpoint_failure("wss://ws.xtb.com/real", FakeHandshakeError(404))
    assert "404" in message
    # It must say this is not a credentials problem, which is the usual guess.
    assert "not a credentials problem" in message
    assert "unsharp-bot endpoints" in message
    assert "XTB_MODE" in message
    assert "main_url" in message


def test_403_explanation_points_at_the_network():
    message = explain_endpoint_failure("wss://ws.xtb.com/real", FakeHandshakeError(403))
    assert "403" in message
    assert "firewall" in message


def test_transport_error_explanation():
    message = explain_endpoint_failure("wss://ws.xtb.com/demo", OSError("no route"))
    assert "no route" in message
    assert "unsharp-bot endpoints" in message


# --------------------------------------------------------------------------- #
# Fail fast
# --------------------------------------------------------------------------- #
def test_permanent_failure_is_not_retried(monkeypatch):
    """A 404 must fail on the first attempt, not burn the whole backoff budget."""
    attempts = 0

    def always_404(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise FakeHandshakeError(404)

    monkeypatch.setattr(xtb_client.websocket, "create_connection", always_404)
    monkeypatch.setattr(xtb_client.time, "sleep", lambda _seconds: None)

    client = XtbClient(
        url="wss://ws.xtb.com/real", user_id="1", password="x",
        reconnect_max_attempts=8, reconnect_base_delay=0.01,
    )
    with pytest.raises(XtbEndpointError, match="404"):
        client._open_socket()
    assert attempts == 1, "a 404 was retried; it can never succeed"


def test_transient_failure_is_retried_then_gives_up(monkeypatch):
    attempts = 0

    def always_refused(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("connection refused")

    monkeypatch.setattr(xtb_client.websocket, "create_connection", always_refused)
    monkeypatch.setattr(xtb_client.time, "sleep", lambda _seconds: None)

    client = XtbClient(
        url="wss://ws.xtb.com/demo", user_id="1", password="x",
        reconnect_max_attempts=4, reconnect_base_delay=0.01,
    )
    with pytest.raises(Exception, match="Cannot open"):
        client._open_socket()
    assert attempts == 4


def test_transient_failure_then_success(monkeypatch):
    """A blip must not take the bot down."""
    attempts = 0

    class FakeSocket:
        def settimeout(self, _value): pass

    def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary failure in name resolution")
        return FakeSocket()

    monkeypatch.setattr(xtb_client.websocket, "create_connection", flaky)
    monkeypatch.setattr(xtb_client.time, "sleep", lambda _seconds: None)

    client = XtbClient(
        url="wss://ws.xtb.com/demo", user_id="1", password="x",
        reconnect_max_attempts=5, reconnect_base_delay=0.01,
    )
    client._open_socket()
    assert attempts == 3


# --------------------------------------------------------------------------- #
# Endpoint configuration
# --------------------------------------------------------------------------- #
def test_default_endpoints_follow_the_mode():
    demo = BrokerConfig(mode="demo", user_id="1", password="x")
    assert demo.main_endpoint == XTB_ENDPOINTS["demo"]["main"]
    assert demo.stream_endpoint == XTB_ENDPOINTS["demo"]["stream"]

    real = BrokerConfig(mode="real", user_id="1", password="x")
    assert real.main_endpoint == XTB_ENDPOINTS["real"]["main"]
    assert real.stream_endpoint == XTB_ENDPOINTS["real"]["stream"]
    assert not real.uses_custom_endpoint


def test_endpoints_can_be_overridden():
    """XTB moving a URL must not require a new release of this bot."""
    config = BrokerConfig(
        mode="real", user_id="1", password="x",
        main_url="wss://example.invalid/gateway",
        stream_url="wss://example.invalid/gatewayStream",
    )
    config.validate()
    assert config.main_endpoint == "wss://example.invalid/gateway"
    assert config.stream_endpoint == "wss://example.invalid/gatewayStream"
    assert config.uses_custom_endpoint


def test_overriding_only_one_endpoint_keeps_the_other_default():
    config = BrokerConfig(
        mode="demo", user_id="1", password="x", main_url="wss://example.invalid/main"
    )
    assert config.main_endpoint == "wss://example.invalid/main"
    assert config.stream_endpoint == XTB_ENDPOINTS["demo"]["stream"]


def test_a_non_websocket_override_is_rejected():
    config = BrokerConfig(mode="demo", user_id="1", password="x",
                          main_url="https://ws.xtb.com/demo")
    with pytest.raises(ConfigError, match="WebSocket URL"):
        config.validate()


def test_tcp_endpoints_are_documented_for_diagnostics():
    """Kept for the 'endpoints' probe, to tell a gateway outage from an XTB outage."""
    assert set(XTB_TCP_ENDPOINTS) == {"demo", "real"}
    for host, main_port, stream_port in XTB_TCP_ENDPOINTS.values():
        assert host and main_port != stream_port
