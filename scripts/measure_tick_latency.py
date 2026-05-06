"""Measure MT5 tick fetch latency from the bot container.

Runs symbol_info_tick(symbol) in a tight loop and reports p50/p95/p99 over
the sample window. Used to verify Phase 0 SLO (p95 < 350ms) before
enabling the tick engine on a live account.

Usage:
    python3 scripts/measure_tick_latency.py --symbol XAUUSD --duration 600
    # writes a JSON summary to logs/tick_latency_<timestamp>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.loader import load_config  # noqa: E402
from src.mt5.client import AsyncMT5Client  # noqa: E402

logger = logging.getLogger(__name__)


async def measure(symbol: str, duration_seconds: int, interval_ms: int) -> dict:
    config = load_config()
    client = AsyncMT5Client(host=config.mt5.rpyc_host, port=config.mt5.rpyc_port)
    await client.connect()

    samples_ms: list[float] = []
    errors = 0
    end_at = time.monotonic() + duration_seconds
    interval_s = interval_ms / 1000.0

    print(
        f"Sampling {symbol} every {interval_ms}ms for {duration_seconds}s "
        f"({duration_seconds // interval_ms * 1000} target samples)..."
    )

    while time.monotonic() < end_at:
        t0 = time.monotonic()
        try:
            await client.symbol_info_tick(symbol)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            samples_ms.append(elapsed_ms)
        except Exception as e:
            errors += 1
            logger.debug("tick fetch error: %s", e)
        await asyncio.sleep(interval_s)

    await client.disconnect()

    if not samples_ms:
        raise RuntimeError(f"No samples collected — {errors} errors")

    samples_ms.sort()
    summary = {
        "symbol": symbol,
        "duration_seconds": duration_seconds,
        "interval_ms": interval_ms,
        "samples": len(samples_ms),
        "errors": errors,
        "p50_ms": round(statistics.median(samples_ms), 2),
        "p95_ms": round(samples_ms[int(len(samples_ms) * 0.95)], 2),
        "p99_ms": round(samples_ms[int(len(samples_ms) * 0.99)], 2),
        "min_ms": round(min(samples_ms), 2),
        "max_ms": round(max(samples_ms), 2),
        "mean_ms": round(statistics.mean(samples_ms), 2),
        "stdev_ms": round(statistics.stdev(samples_ms), 2) if len(samples_ms) > 1 else 0.0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "slo_p95_350ms": samples_ms[int(len(samples_ms) * 0.95)] < 350.0,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--duration", type=int, default=600, help="seconds")
    parser.add_argument("--interval-ms", type=int, default=200)
    parser.add_argument("--output-dir", default="logs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    summary = asyncio.run(measure(args.symbol, args.duration, args.interval_ms))

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output_dir) / f"tick_latency_{int(time.time())}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print(json.dumps(summary, indent=2))
    print(f"\nWrote {out_path}")
    if not summary["slo_p95_350ms"]:
        print("WARNING: p95 latency exceeds 350ms SLO — investigate before enabling tick engine")
        sys.exit(1)


if __name__ == "__main__":
    main()
