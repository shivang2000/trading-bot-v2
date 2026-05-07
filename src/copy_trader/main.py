"""Entrypoint for the copy-trader process.

Run:
    python -m src.copy_trader.main

Reads `config/copy_trader.yaml` (override via $COPY_TRADER_CONFIG env var).
Connects to two MT5 RPyC endpoints (source + dest), starts CopyTrader,
runs forever.

This process is fully isolated from the existing trading-bot-v2 stack —
no shared imports of analysis/strategies/signal_generator. The only
shared modules are the read-only utilities (AsyncMT5Client, models,
enums).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from src.config.copy_trader_schema import CopyTraderAppConfig
from src.copy_trader.copy_trader import CopyTrader
from src.copy_trader.mirror_journal import MirrorJournal
from src.copy_trader.notifier import SlackNotifier
from src.mt5.client import AsyncMT5Client

logger = logging.getLogger(__name__)


_VAR_RE = __import__("re").compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _resolve_env(text: str) -> str:
    """Substitute ${VAR} and ${VAR:default} placeholders from os.environ."""
    def _replace(match):
        var, default = match.group(1), match.group(2)
        return os.environ.get(var, default if default is not None else "")
    return _VAR_RE.sub(_replace, text)


def _load_config(path: str) -> CopyTraderAppConfig:
    raw_text = Path(path).read_text()
    resolved = _resolve_env(raw_text)
    return CopyTraderAppConfig(**yaml.safe_load(resolved))


async def _connect(name: str, host: str, port: int) -> AsyncMT5Client:
    client = AsyncMT5Client(host=host, port=port)
    logger.info("[%s] connecting to MT5 RPyC %s:%d", name, host, port)
    await client.connect()
    logger.info("[%s] connected", name)
    return client


async def run() -> int:
    config_path = os.environ.get("COPY_TRADER_CONFIG", "config/copy_trader.yaml")
    config = _load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    )
    logger.info("=" * 60)
    logger.info("copy-trader-bot starting (config: %s)", config_path)
    logger.info("=" * 60)

    src = await _connect("source", config.source.rpyc_host, config.source.rpyc_port)
    dst = await _connect("dest", config.dest.rpyc_host, config.dest.rpyc_port)

    # Sanity guard — refuse to run if both endpoints look identical
    if config.source.expected_account_login and config.dest.expected_account_login:
        if config.source.expected_account_login == config.dest.expected_account_login:
            logger.error("FATAL: source and dest expected_account_login are equal — refusing to start.")
            return 2

    journal = MirrorJournal(db_path=config.journal.db_path)
    journal.prune_old_events(retention_days=config.journal.retention_days)

    notifier = SlackNotifier(
        webhook_url=config.slack.webhook_url,
        channel_label=config.slack.channel_label,
        enabled=config.slack.enabled,
    )

    copy_trader = CopyTrader(
        source_mt5=src,
        dest_mt5=dst,
        journal=journal,
        notifier=notifier,
        config=config.copy_trader,
    )
    await copy_trader.boot()
    await notifier.post(f"copy-trader-bot started (poll={config.copy_trader.poll_interval_ms}ms)")

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info("Signal %s received — shutting down", sig.name)
        copy_trader.stop()
        stop_event.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, lambda s=s: _on_signal(s))

    try:
        # Run forever in background, exit on stop_event
        run_task = asyncio.create_task(copy_trader.run_forever())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        await notifier.post("copy-trader-bot stopping")
        journal.close()
        await src.disconnect()
        await dst.disconnect()
        logger.info("copy-trader-bot stopped cleanly")

    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
