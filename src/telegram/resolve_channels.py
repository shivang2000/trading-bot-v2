"""Resolve Telegram channel usernames to numeric IDs.

Usage:
  python -m src.telegram.resolve_channels yoforexgold Tradelikemalikagroup
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient


async def resolve(usernames: list[str]) -> None:
    load_dotenv()

    api_id = os.getenv("TELEGRAM_API_ID", "")
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session_path = "data/telegram_session"

    if not api_id or not api_hash:
        print("ERROR: Set TELEGRAM_API_ID, TELEGRAM_API_HASH in .env")
        sys.exit(1)

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Not authenticated. Run: python -m src.telegram.auth --send-code")
        await client.disconnect()
        sys.exit(1)

    print("Resolving channel usernames...\n")

    for username in usernames:
        # Strip https://t.me/ prefix if present
        clean = username.replace("https://t.me/", "").replace("http://t.me/", "").strip("/")
        try:
            entity = await client.get_entity(clean)
            # Telegram channel IDs need -100 prefix for the API
            channel_id = f"-100{entity.id}"
            title = getattr(entity, "title", clean)
            print(f"  {clean}:")
            print(f"    id: \"{channel_id}\"")
            print(f"    title: \"{title}\"")
            print()
        except Exception as e:
            print(f"  {clean}: FAILED — {e}")
            print(f"    Make sure you've joined this channel first.")
            print()

    await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.telegram.resolve_channels <username1> [username2] ...")
        sys.exit(1)

    asyncio.run(resolve(sys.argv[1:]))
