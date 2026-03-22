"""Channel performance analytics.

Queries the tracking database to compute per-channel statistics:
win rate, total P&L, average P&L, signal count, etc. This data
drives future decisions about which channels to trust and weight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)


@dataclass
class ChannelPerformance:
    """Performance metrics for a single signal channel."""

    channel_id: str
    channel_name: str
    total_signals: int
    executed_signals: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    win_rate: float
    avg_pnl: float
    best_trade_pnl: float
    worst_trade_pnl: float


class PerformanceTracker:
    """Computes and caches channel performance from the tracking DB."""

    def __init__(self, tracking_db: TrackingDB) -> None:
        self._db = tracking_db

    async def get_channel_performance(
        self, channel_id: str | None = None
    ) -> list[ChannelPerformance]:
        """Get performance metrics, optionally filtered by channel."""
        try:
            rows = await self._db.get_channel_stats(channel_id)
        except Exception:
            logger.warning("Failed to fetch channel stats")
            return []

        results: list[ChannelPerformance] = []
        for row in rows:
            total_trades = row.get("winning_trades", 0) + row.get("losing_trades", 0)
            win_rate = (
                row["winning_trades"] / total_trades * 100 if total_trades > 0 else 0.0
            )
            avg_pnl = row.get("total_pnl", 0.0) / total_trades if total_trades > 0 else 0.0

            results.append(
                ChannelPerformance(
                    channel_id=row.get("channel_id", ""),
                    channel_name=row.get("channel_name", ""),
                    total_signals=row.get("total_signals", 0),
                    executed_signals=row.get("executed_signals", 0),
                    winning_trades=row.get("winning_trades", 0),
                    losing_trades=row.get("losing_trades", 0),
                    total_pnl=row.get("total_pnl", 0.0),
                    win_rate=win_rate,
                    avg_pnl=avg_pnl,
                    best_trade_pnl=row.get("best_trade_pnl", 0.0),
                    worst_trade_pnl=row.get("worst_trade_pnl", 0.0),
                )
            )

        return sorted(results, key=lambda c: c.total_pnl, reverse=True)

    async def update_all_channel_stats(self) -> None:
        """Recompute stats for all channels from trade history."""
        try:
            await self._db.update_channel_stats()
        except Exception:
            logger.exception("Failed to update channel stats")

    async def get_summary(self) -> dict[str, Any]:
        """Get a high-level summary across all channels."""
        channels = await self.get_channel_performance()

        if not channels:
            return {
                "total_channels": 0,
                "total_trades": 0,
                "total_pnl": 0.0,
                "best_channel": None,
                "worst_channel": None,
            }

        total_trades = sum(c.winning_trades + c.losing_trades for c in channels)
        total_pnl = sum(c.total_pnl for c in channels)

        return {
            "total_channels": len(channels),
            "total_trades": total_trades,
            "total_pnl": total_pnl,
            "best_channel": channels[0].channel_name if channels else None,
            "worst_channel": channels[-1].channel_name if channels else None,
        }
