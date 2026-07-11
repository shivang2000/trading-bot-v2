"""Discord notifier — unit tests.

Verifies:
  1. Disabled when DISCORD_WEBHOOK_URL unset or malformed
  2. send() returns False when disabled (no network call)
  3. send() posts correct JSON shape when enabled
  4. 200 / 204 → True, 4xx/5xx → False (failure doesn't kill the bot)
  5. Truncation of >2000-char messages
  6. critical=True prepends the configured ping
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.monitoring.discord import DiscordConfig, DiscordNotifier


def test_disabled_when_url_unset(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    cfg = DiscordConfig()
    assert cfg.enabled is False
    notifier = DiscordNotifier(cfg)
    assert notifier.enabled is False


def test_disabled_when_url_malformed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://not-discord.example/hook")
    cfg = DiscordConfig()
    assert cfg.enabled is False
    notifier = DiscordNotifier(cfg)
    assert notifier.enabled is False


def test_enabled_with_valid_url(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/12345/abc")
    cfg = DiscordConfig()
    assert cfg.enabled is True
    assert cfg.username == "FundingPips Bot"  # default
    assert cfg.mode == "webhook"
    monkeypatch.setenv("DISCORD_BOT_USERNAME", "FP-5K")
    cfg2 = DiscordConfig()
    assert cfg2.username == "FP-5K"


def test_enabled_with_bot_token(monkeypatch):
    """Bot-token mode: DISCORD_BOT_TOKEN + DISCORD_ALERT_CHANNEL_ID."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "MTUxNjc4MjU2Mzc1NzEzMzk2NA.G-abc123")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "1525476646176690297")
    cfg = DiscordConfig()
    assert cfg.enabled is True
    assert cfg.mode == "bot_token"


def test_disabled_when_only_token_no_channel(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "MTUxNjc4MjU2Mzc1NzEzMzk2NA.G-abc123")
    monkeypatch.delenv("DISCORD_ALERT_CHANNEL_ID", raising=False)
    cfg = DiscordConfig()
    assert cfg.enabled is False
    assert cfg.mode == "disabled"


def test_webhook_takes_precedence_over_bot_token(monkeypatch):
    """If both are set, webhook mode wins (simpler, one-way)."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/12345/abc")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "MTUxNjc4MjU2Mzc1NzEzMzk2NA.G-abc123")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "1525476646176690297")
    cfg = DiscordConfig()
    assert cfg.mode == "webhook"


@pytest.mark.asyncio
async def test_send_returns_false_when_disabled(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    notifier = DiscordNotifier()
    assert await notifier.send("test") is False


@pytest.mark.asyncio
async def test_send_posts_correct_payload(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    notifier = DiscordNotifier()

    mock_response = AsyncMock()
    mock_response.status_code = 204
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        ok = await notifier.send("hello world")
    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://discord.com/api/webhooks/1/abc"
    assert kwargs["json"]["content"] == "hello world"
    assert kwargs["json"]["username"] == "FundingPips Bot"


@pytest.mark.asyncio
async def test_send_bot_token_mode(monkeypatch):
    """Bot-token mode: posts to Discord API v10 channel endpoint."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "MTUxNjc4MjU2Mzc1NzEzMzk2NA.G-abc123")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "1525476646176690297")
    notifier = DiscordNotifier()
    assert notifier.enabled is True

    mock_response = AsyncMock()
    mock_response.status_code = 200
    captured = {}

    async def fake_post(url, **kw):
        captured["url"] = url
        captured["headers"] = kw.get("headers", {})
        captured["json"] = kw.get("json", {})
        return mock_response

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        ok = await notifier.send("EMERGENCY STOP test")
    assert ok is True
    assert captured["url"] == (
        "https://discord.com/api/v10/channels/1525476646176690297/messages"
    )
    assert captured["headers"]["Authorization"] == "Bot MTUxNjc4MjU2Mzc1NzEzMzk2NA.G-abc123"
    assert captured["json"]["content"] == "EMERGENCY STOP test"


@pytest.mark.asyncio
async def test_send_handles_500_gracefully(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    notifier = DiscordNotifier()
    mock_response = AsyncMock()
    mock_response.status_code = 500
    mock_response.text = "internal error"
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        ok = await notifier.send("hello")
    assert ok is False  # failure swallowed, bot doesn't crash


@pytest.mark.asyncio
async def test_send_truncates_long_message(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    notifier = DiscordNotifier()
    huge = "x" * 3000
    mock_response = AsyncMock()
    mock_response.status_code = 204
    captured = {}
    async def fake_post(url, **kw):
        captured["payload"] = kw["json"]
        return mock_response
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        await notifier.send(huge)
    assert len(captured["payload"]["content"]) <= 2000
    assert "truncated" in captured["payload"]["content"]


@pytest.mark.asyncio
async def test_critical_prepends_ping(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    monkeypatch.setenv("DISCORD_CRITICAL_PING", "<@&1234567890>")
    notifier = DiscordNotifier()
    mock_response = AsyncMock()
    mock_response.status_code = 204
    captured = {}
    async def fake_post(url, **kw):
        captured["payload"] = kw["json"]
        return mock_response
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        await notifier.send("EMERGENCY", critical=True)
    assert captured["payload"]["content"].startswith("<@&1234567890>")


@pytest.mark.asyncio
async def test_send_emergency_stop_is_critical(monkeypatch):
    """The post-mortem's lesson #1: emergency stop MUST alert loudly."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    monkeypatch.setenv("DISCORD_CRITICAL_PING", "@here")
    notifier = DiscordNotifier()
    mock_response = AsyncMock()
    mock_response.status_code = 204
    captured = {}
    async def fake_post(url, **kw):
        captured["payload"] = kw["json"]
        return mock_response
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
        await notifier.send_emergency_stop("Daily loss breached 2%")
    assert captured["payload"]["content"].startswith("@here")
    assert "EMERGENCY STOP" in captured["payload"]["content"]
