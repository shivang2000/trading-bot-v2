#!/usr/bin/env python3
"""Backtest CLI for Trading Bot V2.

Usage:
    python scripts/backtest.py --symbol XAUUSD --start 2024-01-01 --end 2025-01-01
    python scripts/backtest.py --symbol XAUUSD --csv-m15 data/backtest_cache/XAUUSD_M15.csv
    python scripts/backtest.py --symbol XAUUSD --strategy ema_pullback --start 2024-06-01 --end 2025-01-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.data_loader import load_from_csv, load_or_download, resample_m15_to_h1
from src.backtesting.engine import BacktestEngine
from src.config.loader import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtest")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Trading Bot V2 strategies")
    parser.add_argument("--symbol", default="XAUUSD", help="Instrument symbol")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--strategy", default="both",
        choices=["ema_pullback", "london_breakout", "both"],
        help="Strategy to backtest",
    )
    parser.add_argument("--initial-capital", type=float, default=10000.0, help="Starting balance")
    parser.add_argument("--volume", type=float, default=0.01, help="Lot size per trade")
    parser.add_argument("--csv-m15", help="Path to M15 CSV (skip MT5 download)")
    parser.add_argument("--csv-h1", help="Path to H1 CSV (or resample from M15)")
    parser.add_argument("--mt5-host", default="localhost", help="MT5 RPyC host")
    parser.add_argument("--mt5-port", type=int, default=8001, help="MT5 RPyC port")
    parser.add_argument("--point-size", type=float, default=0.01, help="Symbol point size")
    parser.add_argument("--tick-value", type=float, default=None, help="Tick value (auto from config if not set)")
    # Strategy parameter overrides (override base.yaml values)
    parser.add_argument("--risk-pct", type=float, default=None, help="Risk %% per trade override")
    parser.add_argument("--sl-atr", type=float, default=None, help="SL ATR multiplier override")
    parser.add_argument("--tp-atr", type=float, default=None, help="TP ATR multiplier override")
    parser.add_argument("--trail-act", type=float, default=None, help="Trailing activation %% override")
    parser.add_argument("--trail-atr", type=float, default=None, help="Trailing ATR multiplier override")
    parser.add_argument("--label", default="", help="Label for this config (e.g. 'A-conservative')")

    args = parser.parse_args()

    # Load M15 data
    if args.csv_m15:
        logger.info("Loading M15 data from CSV: %s", args.csv_m15)
        m15_data = load_from_csv(args.csv_m15)
    else:
        if not args.start or not args.end:
            parser.error("--start and --end required when not using --csv-m15")
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        m15_data = load_or_download(
            args.symbol, "M15", start, end,
            mt5_host=args.mt5_host, mt5_port=args.mt5_port,
        )

    # Load or resample H1 data
    if args.csv_h1:
        logger.info("Loading H1 data from CSV: %s", args.csv_h1)
        h1_data = load_from_csv(args.csv_h1)
    else:
        # Try downloading H1, fall back to resampling
        if not args.csv_m15 and args.start and args.end:
            start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            try:
                h1_data = load_or_download(
                    args.symbol, "H1", start, end,
                    mt5_host=args.mt5_host, mt5_port=args.mt5_port,
                )
            except Exception:
                logger.info("H1 download failed, resampling from M15")
                h1_data = resample_m15_to_h1(m15_data)
        else:
            h1_data = resample_m15_to_h1(m15_data)

    logger.info("Data loaded: %d M15 bars, %d H1 bars", len(m15_data), len(h1_data))

    # Load config from base.yaml and apply CLI overrides
    config = load_config()
    if args.risk_pct is not None:
        config.account.risk_per_trade_pct = args.risk_pct
    if args.sl_atr is not None:
        config.strategies.ema_pullback.atr_sl_multiplier = args.sl_atr
    if args.tp_atr is not None:
        config.strategies.ema_pullback.atr_tp_multiplier = args.tp_atr
    if args.trail_act is not None:
        config.trailing_stop.activation_pct = args.trail_act
    if args.trail_atr is not None:
        config.trailing_stop.atr_multiplier = args.trail_atr

    label = args.label or "default"
    logger.info(
        "Config [%s]: risk=%.1f%%, SL=%.1fx ATR, TP=%.1fx ATR, trail_act=%.0f%%, trail_atr=%.1fx",
        label,
        config.account.risk_per_trade_pct,
        config.strategies.ema_pullback.atr_sl_multiplier,
        config.strategies.ema_pullback.atr_tp_multiplier,
        config.trailing_stop.activation_pct * 100,
        config.trailing_stop.atr_multiplier,
    )

    # Run backtest
    engine = BacktestEngine(
        symbol=args.symbol,
        strategy=args.strategy,
        initial_capital=args.initial_capital,
        volume=args.volume,
        point_size=args.point_size,
        config=config,
    )

    result = engine.run(m15_data, h1_data)

    # Print results
    print(result.summary())

    # Save results to JSON
    results_dir = Path("data/backtest_results")
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_file = results_dir / f"{args.symbol}_{label}_{timestamp}.json"

    result_data = {
        "strategy": result.strategy_name,
        "symbol": result.symbol,
        "start_date": str(result.start_date),
        "end_date": str(result.end_date),
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "total_return_pct": result.total_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "total_trades": result.total_trades,
        "avg_trade_pnl": result.avg_trade_pnl,
        "sharpe_ratio": result.sharpe_ratio,
        "sortino_ratio": result.sortino_ratio,
        "max_consecutive_losses": result.max_consecutive_losses,
        "max_consecutive_wins": result.max_consecutive_wins,
        "avg_trade_duration_hours": result.avg_trade_duration_hours,
        "best_trade_pnl": result.best_trade_pnl,
        "worst_trade_pnl": result.worst_trade_pnl,
        "expectancy": result.expectancy,
        "trades": [
            {
                "ticket": t.ticket,
                "side": t.side.value,
                "open_price": t.open_price,
                "close_price": t.close_price,
                "open_time": str(t.open_time),
                "close_time": str(t.close_time),
                "pnl": t.pnl,
                "close_reason": t.close_reason,
            }
            for t in result.trades
        ],
    }

    with open(result_file, "w") as f:
        json.dump(result_data, f, indent=2)

    logger.info("Results saved to %s", result_file)


if __name__ == "__main__":
    main()
