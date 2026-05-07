"""Dukascopy XAUUSD tick downloader with gap detection.

Pulls historical tick data from Dukascopy's free SWFX feed and normalises
into the CSV format that `src/backtesting/tick_engine.py` consumes:

    timestamp,bid,ask,last,volume

Output layout (gzipped daily files, one CSV per UTC day):
    data/ticks/dukascopy/xauusd/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz

Dukascopy bi5 format (per hour):
    URL: https://datafeed.dukascopy.com/datafeed/{INSTRUMENT}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5
    LZMA compressed binary, 20 bytes per tick:
        uint32  ms_from_hour_start  (big-endian)
        uint32  ask * scale         (big-endian)
        uint32  bid * scale         (big-endian)
        float32 ask_volume          (big-endian)
        float32 bid_volume          (big-endian)
    XAUUSD price scaling factor = 1000 (3 decimals on Dukascopy).

Empty hours (weekends, holidays, illiquid Asian sessions) return an empty
bi5 file (12-byte LZMA header). Gap detection logs these but does not
treat them as errors — Dukascopy intentionally serves no ticks when the
SWFX pool was idle.

Usage:
    python3 scripts/download_dukascopy_xauusd_ticks.py \
        --start 2026-01-01 --end 2026-03-31 \
        --output data/ticks/dukascopy/xauusd \
        --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import logging
import lzma
import struct
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger(__name__)

DUKA_URL_TMPL = (
    "https://datafeed.dukascopy.com/datafeed/{symbol}/"
    "{year:04d}/{month:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
)
TICK_STRUCT = struct.Struct(">IIIff")  # ms, ask*scale, bid*scale, ask_vol, bid_vol
XAUUSD_PRICE_SCALE = 1000.0


def parse_bi5(data: bytes, hour_start_ms: int, scale: float = XAUUSD_PRICE_SCALE) -> list[tuple[int, float, float, float, float]]:
    """Parse LZMA-compressed bi5 payload into a list of tick tuples.

    Returns list of (timestamp_ms_utc, bid, ask, ask_vol, bid_vol).
    Empty payload (idle hour) yields an empty list, not an error.
    """
    if not data:
        return []
    try:
        decoded = lzma.decompress(data)
    except lzma.LZMAError:
        logger.warning("LZMA decompress failed (likely truncated or empty hour)")
        return []

    ticks: list[tuple[int, float, float, float, float]] = []
    for chunk in TICK_STRUCT.iter_unpack(decoded):
        ms_in_hour, ask_scaled, bid_scaled, ask_vol, bid_vol = chunk
        ticks.append((
            hour_start_ms + ms_in_hour,
            bid_scaled / scale,
            ask_scaled / scale,
            ask_vol,
            bid_vol,
        ))
    return ticks


def hour_url(symbol: str, dt: datetime) -> str:
    """Build the Dukascopy bi5 URL for one specific hour. Month is 0-indexed."""
    return DUKA_URL_TMPL.format(
        symbol=symbol.upper(),
        year=dt.year,
        month=dt.month - 1,
        day=dt.day,
        hour=dt.hour,
    )


async def fetch_hour(
    client: httpx.AsyncClient,
    symbol: str,
    dt: datetime,
    sem: asyncio.Semaphore,
    retries: int = 3,
) -> list[tuple[int, float, float, float, float]]:
    """Fetch and parse one hour. Returns ticks (possibly empty).

    Retries on transient errors. Treats 404 as empty hour (some old
    instruments have gaps in coverage).
    """
    url = hour_url(symbol, dt)
    hour_start_ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

    async with sem:
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await client.get(url, timeout=30.0)
                if resp.status_code == 404:
                    return []
                if resp.status_code != 200:
                    raise httpx.HTTPStatusError(
                        f"status={resp.status_code}", request=resp.request, response=resp
                    )
                return parse_bi5(resp.content, hour_start_ms)
            except (httpx.HTTPError, httpx.ReadTimeout) as e:
                last_err = e
                # Exponential backoff
                await asyncio.sleep(2 ** attempt)
        logger.warning("Failed %s after %d retries: %s", url, retries, last_err)
        return []


def write_day_csv_gz(
    ticks: list[tuple[int, float, float, float, float]],
    out_path: Path,
) -> int:
    """Write a sorted list of ticks for one UTC day to a gzipped CSV.

    Returns the number of rows written. Empty input still creates a header-
    only file so gap detection knows the day was attempted.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with gzip.open(out_path, "wt", newline="") as fh:
        fh.write("timestamp,bid,ask,last,volume\n")
        for ts_ms, bid, ask, ask_vol, bid_vol in ticks:
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            # Dukascopy provides bid/ask; "last" is conventionally the mid
            # for symmetric reference, "volume" is bid+ask volume.
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask)
            vol = ask_vol + bid_vol
            fh.write(f"{ts.isoformat().replace('+00:00','Z')},{bid:.5f},{ask:.5f},{mid:.5f},{vol:.4f}\n")
            rows_written += 1
    return rows_written


async def download_day(
    client: httpx.AsyncClient,
    symbol: str,
    day: date,
    sem: asyncio.Semaphore,
) -> tuple[int, int]:
    """Download all 24 hours of one UTC day and write a single gzipped CSV.

    Returns (ticks_written, hours_with_data).
    """
    hour_dts = [
        datetime(day.year, day.month, day.day, h, 0, 0, tzinfo=timezone.utc)
        for h in range(24)
    ]
    coros = [fetch_hour(client, symbol, dt, sem) for dt in hour_dts]
    hour_results = await asyncio.gather(*coros)

    all_ticks: list[tuple[int, float, float, float, float]] = []
    hours_with_data = 0
    for hour_ticks in hour_results:
        if hour_ticks:
            hours_with_data += 1
            all_ticks.extend(hour_ticks)

    all_ticks.sort(key=lambda t: t[0])
    return all_ticks, hours_with_data


def out_path_for(out_root: Path, symbol: str, day: date) -> Path:
    return out_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.isoformat()}.csv.gz"


async def run(args: argparse.Namespace) -> int:
    start_d = datetime.fromisoformat(args.start).date()
    end_d = datetime.fromisoformat(args.end).date()
    if end_d < start_d:
        print("--end must be >= --start", file=sys.stderr)
        return 2

    out_root = Path(args.output) / args.symbol.lower()
    sem = asyncio.Semaphore(args.concurrency)

    days_attempted = 0
    days_with_data = 0
    days_skipped_existing = 0
    total_ticks = 0
    gap_days: list[date] = []

    t0 = time.monotonic()
    headers = {"User-Agent": "trading-bot-v2 dukascopy-fetch/1.0"}
    async with httpx.AsyncClient(http2=False, headers=headers) as client:
        cur = start_d
        while cur <= end_d:
            target = out_path_for(out_root, args.symbol, cur)
            days_attempted += 1

            if target.exists() and not args.force:
                days_skipped_existing += 1
                cur += timedelta(days=1)
                continue

            # Skip Saturdays — Dukascopy serves no XAUUSD ticks on weekends
            # (Saturday is fully empty; Sunday opens late at 22:00 UTC).
            if cur.weekday() == 5:  # Saturday
                gap_days.append(cur)
                cur += timedelta(days=1)
                continue

            day_ticks, hours_with_data = await download_day(client, args.symbol, cur, sem)
            written = write_day_csv_gz(day_ticks, target)
            total_ticks += written

            if hours_with_data == 0:
                gap_days.append(cur)
                logger.info("Gap day (no ticks any hour): %s", cur.isoformat())
            else:
                days_with_data += 1
                logger.info(
                    "%s: %d ticks across %d hours -> %s",
                    cur.isoformat(), written, hours_with_data, target,
                )

            cur += timedelta(days=1)

    elapsed = time.monotonic() - t0
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  days attempted:        {days_attempted}")
    print(f"  days with data:        {days_with_data}")
    print(f"  days skipped (exists): {days_skipped_existing}")
    print(f"  gap days (no ticks):   {len(gap_days)}")
    print(f"  total ticks written:   {total_ticks:,}")
    if gap_days and args.verbose_gaps:
        print(f"  gap day list: {[d.isoformat() for d in gap_days]}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download Dukascopy XAUUSD ticks to gzipped CSV.")
    p.add_argument("--symbol", default="XAUUSD", help="Instrument code (default: XAUUSD)")
    p.add_argument("--start", required=True, help="UTC start date (YYYY-MM-DD), inclusive")
    p.add_argument("--end", required=True, help="UTC end date (YYYY-MM-DD), inclusive")
    p.add_argument("--output", default="data/ticks/dukascopy", help="Output root directory")
    p.add_argument("--concurrency", type=int, default=8, help="Max concurrent hour fetches")
    p.add_argument("--force", action="store_true", help="Re-download even if output file exists")
    p.add_argument("--verbose-gaps", action="store_true", help="Print every gap-day at end")
    p.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARN/ERROR")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
