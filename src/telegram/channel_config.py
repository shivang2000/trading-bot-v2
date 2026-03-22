"""Channel registry and symbol mapping for Telegram signal channels."""

from __future__ import annotations

import logging

from src.config.schema import ChannelConfig

logger = logging.getLogger(__name__)

# Maps informal instrument names from Telegram messages to MT5 symbols
SYMBOL_ALIASES: dict[str, str] = {
    # Gold
    "gold": "XAUUSD",
    "xau": "XAUUSD",
    "xauusd": "XAUUSD",
    # Silver
    "silver": "XAGUSD",
    "xag": "XAGUSD",
    "xagusd": "XAGUSD",
    # Bitcoin
    "btc": "BTCUSD",
    "bitcoin": "BTCUSD",
    "btcusd": "BTCUSD",
    "btc/usd": "BTCUSD",
    # Ethereum
    "eth": "ETHUSD",
    "ethereum": "ETHUSD",
    "ethusd": "ETHUSD",
    "eth/usd": "ETHUSD",
}


class ChannelRegistry:
    """Manages the set of Telegram channels the bot listens to."""

    def __init__(self, channels: list[ChannelConfig]) -> None:
        self._channels: dict[str, ChannelConfig] = {}
        for ch in channels:
            if ch.enabled:
                self._channels[ch.id] = ch
                logger.info("Registered channel: %s (%s)", ch.name or ch.id, ch.id)

    @property
    def channel_ids(self) -> list[str]:
        """Return list of channel IDs to listen to."""
        return list(self._channels.keys())

    @property
    def channel_ids_as_ints(self) -> list[int]:
        """Return channel IDs as integers (Telethon uses int IDs)."""
        return [int(cid) for cid in self._channels.keys()]

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        """Look up a channel by ID."""
        return self._channels.get(str(channel_id))

    def get_channel_name(self, channel_id: str) -> str:
        """Get the display name for a channel."""
        ch = self._channels.get(str(channel_id))
        return ch.name if ch else str(channel_id)

    def is_instrument_allowed(self, channel_id: str, symbol: str) -> bool:
        """Check if a channel is configured to trade a given instrument."""
        ch = self._channels.get(str(channel_id))
        if ch is None:
            return False
        # If no instruments specified, allow all
        if not ch.instruments:
            return True
        return symbol in ch.instruments

    @staticmethod
    def resolve_symbol(raw_name: str) -> str | None:
        """Map an informal instrument name to an MT5 symbol.

        Returns None if the name doesn't match any known instrument.
        """
        normalized = raw_name.strip().lower().replace(" ", "")
        return SYMBOL_ALIASES.get(normalized)
