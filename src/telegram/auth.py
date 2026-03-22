"""Telegram authentication script — supports both interactive and 2-step modes.

Interactive (default):
  python -m src.telegram.auth

Non-interactive (for automation via SSH):
  Step 1: python -m src.telegram.auth --send-code
  Step 2: python -m src.telegram.auth --sign-in 12345
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

HASH_FILE = "data/.phone_code_hash"


def _get_config() -> tuple[str, str, str, str]:
    load_dotenv()
    api_id = os.getenv("TELEGRAM_API_ID", "")
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    phone = os.getenv("TELEGRAM_PHONE", "")
    session_path = "data/telegram_session"

    if not api_id or not api_hash or not phone:
        print("ERROR: Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE in .env")
        sys.exit(1)

    return api_id, api_hash, phone, session_path


async def send_code() -> None:
    """Step 1: Send the OTP code to the user's phone."""
    api_id, api_hash, phone, session_path = _get_config()

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authenticated as: {me.first_name} (@{me.username}, id: {me.id})")
        await client.disconnect()
        return

    print(f"Sending code to {phone}...")
    result = await client.send_code_request(phone)

    # Save hash for step 2
    Path(HASH_FILE).write_text(json.dumps({
        "phone_code_hash": result.phone_code_hash,
        "phone": phone,
    }))

    print(f"Code sent to {phone}.")
    print(f"Now run: python -m src.telegram.auth --sign-in <CODE>")

    await client.disconnect()


async def sign_in(code: str) -> None:
    """Step 2: Sign in with the OTP code."""
    api_id, api_hash, phone, session_path = _get_config()

    hash_path = Path(HASH_FILE)
    if not hash_path.exists():
        print("ERROR: No phone_code_hash found. Run --send-code first.")
        sys.exit(1)

    data = json.loads(hash_path.read_text())
    phone_code_hash = data["phone_code_hash"]

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authenticated as: {me.first_name} (@{me.username}, id: {me.id})")
        hash_path.unlink(missing_ok=True)
        await client.disconnect()
        return

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        print(f"Sign-in failed: {e}")
        print("The code may have expired. Run --send-code again.")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print(f"Authenticated as: {me.first_name} (@{me.username}, id: {me.id})")
    print("Session saved. The bot can now connect non-interactively.")

    hash_path.unlink(missing_ok=True)
    await client.disconnect()


async def interactive() -> None:
    """Interactive mode — prompts for code via stdin."""
    api_id, api_hash, phone, session_path = _get_config()

    print(f"Authenticating Telegram for phone: {phone}")
    print(f"Session will be saved to: {session_path}.session")
    print()

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start(phone=phone)

    me = await client.get_me()
    print(f"\nAuthenticated as: {me.first_name} (@{me.username}, id: {me.id})")
    print("Session saved. The bot can now connect non-interactively.")

    await client.disconnect()


def main() -> None:
    if len(sys.argv) > 1:
        if sys.argv[1] == "--send-code":
            asyncio.run(send_code())
        elif sys.argv[1] == "--sign-in":
            if len(sys.argv) < 3:
                print("Usage: python -m src.telegram.auth --sign-in <CODE>")
                sys.exit(1)
            asyncio.run(sign_in(sys.argv[2]))
        else:
            print("Usage:")
            print("  python -m src.telegram.auth              # Interactive")
            print("  python -m src.telegram.auth --send-code   # Step 1: send OTP")
            print("  python -m src.telegram.auth --sign-in X   # Step 2: sign in")
            sys.exit(1)
    else:
        asyncio.run(interactive())


if __name__ == "__main__":
    main()
