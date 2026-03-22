"""Telegram channel listener using Telethon (user account API).

Connects to Telegram as a user account, listens to configured signal
channels, and forwards messages to the signal parser. Raw messages are
stored in the tracking database for future analysis.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import TelegramClient, events

from src.telegram.channel_config import ChannelRegistry
from src.tracking.database import TrackingDB

if TYPE_CHECKING:
    from src.telegram.parser import SignalParser

logger = logging.getLogger(__name__)


class TelegramListener:
    """Listens to Telegram channels and forwards messages to the signal parser."""

    def __init__(
        self,
        api_id: str,
        api_hash: str,
        phone: str,
        session_path: str,
        channel_registry: ChannelRegistry,
        signal_parser: SignalParser,
        tracking_db: TrackingDB,
    ) -> None:
        self._api_id = int(api_id) if api_id and api_id.isdigit() else 0
        self._api_hash = api_hash
        self._phone = phone
        self._session_path = session_path
        self._registry = channel_registry
        self._parser = signal_parser
        self._db = tracking_db
        self._client: TelegramClient | None = None
        self._running = False

    @property
    def is_configured(self) -> bool:
        """Check if Telegram credentials are set."""
        return bool(self._api_id and self._api_hash and self._phone)

    async def start(self) -> None:
        """Connect to Telegram and start listening to channels."""
        if not self.is_configured:
            logger.warning(
                "Telegram listener not configured (missing API_ID/HASH/PHONE) — "
                "skipping. Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE in .env"
            )
            return

        self._client = TelegramClient(
            self._session_path,
            self._api_id,
            self._api_hash,
        )

        # Connect without triggering interactive sign-in
        await self._client.connect()

        if not await self._client.is_user_authorized():
            logger.error(
                "Telegram session not authenticated. Run the auth script first:\n"
                "  python -m src.telegram.auth\n"
                "Or: docker compose run --rm trading-bot python -m src.telegram.auth"
            )
            await self._client.disconnect()
            self._client = None
            return

        self._running = True

        me = await self._client.get_me()
        logger.info("Telegram connected as: %s (id: %s)", me.username, me.id)

        # Register the message handler for configured channels
        channel_ids = self._registry.channel_ids_as_ints
        if not channel_ids:
            logger.warning("No channels configured — listener will not receive messages")
            return

        @self._client.on(events.NewMessage(chats=channel_ids))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle_message(event)

        logger.info(
            "Listening to %d channel(s): %s",
            len(channel_ids),
            [self._registry.get_channel_name(str(cid)) for cid in channel_ids],
        )

    async def stop(self) -> None:
        """Disconnect from Telegram."""
        self._running = False
        if self._client:
            await self._client.disconnect()
            self._client = None
        logger.info("Telegram listener stopped")

    async def run_until_disconnected(self) -> None:
        """Block until the client disconnects."""
        if self._client:
            await self._client.run_until_disconnected()

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Process a new message from a signal channel."""
        try:
            message = event.message
            chat_id = str(event.chat_id)
            channel_name = self._registry.get_channel_name(chat_id)

            # Extract text content
            text = message.text or message.message or ""

            # Check for image/photo
            has_image = message.photo is not None
            image_bytes: bytes | None = None
            if has_image:
                image_bytes = await self._client.download_media(message, bytes)

            logger.info(
                "Message from %s [%s]: %s%s",
                channel_name,
                chat_id,
                text[:100] + "..." if len(text) > 100 else text,
                " [+image]" if has_image else "",
            )

            # Store raw message in tracking DB
            raw_msg_id = await self._db.store_raw_message(
                channel_id=chat_id,
                channel_name=channel_name,
                message_id=message.id,
                message_text=text,
                has_image=has_image,
            )

            # Forward to signal parser
            await self._parser.process_message(
                raw_message_id=raw_msg_id,
                channel_id=chat_id,
                message_text=text,
                image_bytes=image_bytes,
            )

        except Exception:
            logger.exception("Error handling Telegram message from chat %s", event.chat_id)
