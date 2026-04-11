#!/usr/bin/env python3
"""Run complete backtesting matrix: 8 strategies × 5 accounts × 4 risks = 160 runs."""

import subprocess
import sys
import time
import glob
from concurrent.futures import ProcessPoolExecutor, as_completed

STRATEGIES = [
    "m5_mtf_momentum", "m5_keltner_squeeze", "m5_dual_supertrend",
    "m5_box_theory", "m5_amd_cycle", "m5_stochrsi_adx",
    "ema_pullback", "london_breakout",
]

ACCOUNTS = [
    {"size": 50,     "label": "50",   "max_lot": "0.50", "buffer": "0.50"},
    {"size": 100,    "label": "100",  "max_lot": "0.50", "buffer": "0.50"},
    {"size": 5000,   "label": "5k",   "max_lot": "0.50", "buffer": "7.0"},
    {"size": 10000,  "label": "10k",  "max_lot": "1.0",  "buffer": "7.0"},
    {"size": 100000, "label": "100k", "max_lot": "5.0",  "buffer": "7.0"},
]

RISKS = [0.25, 0.5, 1.0, 2.0]

MAX_PARALLEL = 6


def run_backtest(args: dict) -> dict:
    risk_label = str(args["risk"]).replace(".", "")
    label = f'fullmatrix_{args["acct_label"]}_{risk_label}_{args["strategy"]}'
    cmd = [
        sys.executable, "scripts/backtest_scalping.py",
        "--symbol", "XAUUSD", "--timeframe", "M5",
        "--start", "2025-01-01", "--end", "2026-03-27",
        "--prop-firm", "--phase", "master",
        "--account-size", str(args["acct_size"]),
        "--risk-pct", str(args["risk"]),
        "--max-lot", args["max_lot"],
        "--enable-costs",
        "--safety-buffer-daily-usd", args["buffer"],
        "--safety-buffer-dd-usd", args["buffer"],
        "--strategy", args["strategy"],
        "--label", label,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        files = glob.glob(f"data/backtest_results/scalp_XAUUSD_{label}_*.json")
        return {"label": label, "ok": len(files) > 0, "error": None}
    except Exception as e:
        return {"label": label, "ok": False, "error": str(e)}


def main():
    # Build all 160 jobs
    jobs = []
    for acct in ACCOUNTS:
        for risk in RISKS:
            for strat in STRATEGIES:
                jobs.append({
                    "acct_size": acct["size"],
                    "acct_label": acct["label"],
                    "max_lot": acct["max_lot"],
                    "buffer": acct["buffer"],
                    "risk": risk,
                    "strategy": strat,
                })

    total = len(jobs)
    print(f"Running {total} backtests ({len(STRATEGIES)} strategies × {len(ACCOUNTS)} accounts × {len(RISKS)} risks)")
    print(f"Max parallel: {MAX_PARALLEL}")
    print()

    done = 0
    failed = 0
    start_time = time.time()

    with ProcessPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        futures = {executor.submit(run_backtest, job): job for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            if result["ok"]:
                elapsed = time.time() - start_time
                rate = done / elapsed * 60 if elapsed > 0 else 0
                eta = (total - done) / (rate / 60) if rate > 0 else 0
                print(f"  [{done}/{total}] ✓ {result['label']}  ({rate:.0f}/min, ETA {eta:.0f}s)")
            else:
                failed += 1
                print(f"  [{done}/{total}] ✗ {result['label']} — {result['error']}")

    elapsed = time.time() - start_time
    final_count = len(glob.glob("data/backtest_results/scalp_XAUUSD_fullmatrix_*.json"))
    print()
    print(f"═══════════════════════════════════════")
    print(f"  COMPLETE: {final_count}/{total} files in {elapsed:.0f}s")
    print(f"  Failed: {failed}")
    print(f"═══════════════════════════════════════")


if __name__ == "__main__":
    main()
