"""Claude AI-powered signal parser for Telegram messages.

Parses any Telegram message format (text, images, mixed) into structured
trading signals. Handles signal amendments (follow-up SL/TP updates) by
correlating with recent signals from the same channel.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from typing import Any

import anthropic
import pandas_ta as ta

from src.config.schema import SignalParserConfig
from src.core.enums import OrderSide, SignalAction
from src.core.events import (
    EventBus,
    ModifyOrderEvent,
    SignalAmendmentEvent,
    SignalEvent,
)
from src.core.models import ModifyOrder, Signal
from src.mt5.client import AsyncMT5Client
from src.telegram.channel_config import ChannelRegistry
from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a trading signal parser. Your job is to extract trading signals from Telegram channel messages.

RULES:
1. Analyze the message and determine if it contains an actionable trading signal.
2. Messages that are just commentary, analysis, greetings, or market updates are NOT signals.
3. A signal must have at least an ACTION (buy/sell) and an INSTRUMENT (gold, silver, btc, eth, etc).
4. If the message updates SL/TP for a previously sent signal, mark it as an amendment.
5. Map instrument names to MT5 symbols: gold/xau→XAUUSD, silver/xag→XAGUSD, btc/bitcoin→BTCUSD, eth/ethereum→ETHUSD.
6. Extract entry price, stop loss (SL), and take profit (TP) if provided. Some signals don't include all values.
7. If multiple TPs are given (TP1, TP2, TP3), use the FIRST one as the primary take_profit.

RESPOND WITH JSON ONLY. No explanation, no markdown, just the JSON object:
{
  "is_signal": true/false,
  "is_amendment": true/false,
  "action": "BUY" | "SELL" | null,
  "symbol": "XAUUSD" | "XAGUSD" | "BTCUSD" | "ETHUSD" | null,
  "entry_price": float | null,
  "stop_loss": float | null,
  "take_profit": float | null,
  "confidence": 0.0-1.0,
  "reason": "brief extraction note"
}"""


class SignalParser:
    """Parses Telegram messages into trading signals using Claude AI."""

    def __init__(
        self,
        config: SignalParserConfig,
        event_bus: EventBus,
        mt5_client: AsyncMT5Client,
        channel_registry: ChannelRegistry,
        tracking_db: TrackingDB,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._mt5 = mt5_client
        self._registry = channel_registry
        self._db = tracking_db
        self._claude = anthropic.AsyncAnthropic()
        self._valid_symbols = {"XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD"}

    # ── Regex patterns for common Telegram signal formats ──

    _SYMBOL_MAP = {
        "gold": "XAUUSD", "xau": "XAUUSD", "xauusd": "XAUUSD",
        "silver": "XAGUSD", "xag": "XAGUSD", "xagusd": "XAGUSD",
        "btc": "BTCUSD", "bitcoin": "BTCUSD", "btcusd": "BTCUSD",
        "eth": "ETHUSD", "ethereum": "ETHUSD", "ethusd": "ETHUSD",
    }

    _ACTION_RE = re.compile(r"\b(buy|sell|long|short)\b", re.IGNORECASE)
    _SYMBOL_RE = re.compile(
        r"\b(gold|xau(?:usd)?|silver|xag(?:usd)?|btc(?:usd)?|bitcoin|eth(?:usd)?|ethereum)\b",
        re.IGNORECASE,
    )
    _PRICE_RE = re.compile(r"(?:@|price|entry|at)\s*[:=]?\s*(\d+\.?\d*)", re.IGNORECASE)
    _SL_RE = re.compile(r"(?:sl|stop\s*loss|stop)\s*[:=]?\s*(\d+\.?\d*)", re.IGNORECASE)
    _TP_RE = re.compile(r"(?:tp1?|take\s*profit|target)\s*[:=]?\s*(\d+\.?\d*)", re.IGNORECASE)

    def _parse_with_regex(self, message_text: str) -> dict[str, Any] | None:
        """Fallback regex parser for when Claude API is unavailable.

        Matches common signal formats like:
            BUY GOLD 2650 SL 2640 TP 2670
            SELL XAUUSD @ 2650 sl=2660 tp=2630
        Returns the same JSON structure as Claude for seamless integration.
        """
        action_match = self._ACTION_RE.search(message_text)
        symbol_match = self._SYMBOL_RE.search(message_text)

        if not action_match or not symbol_match:
            return None

        raw_action = action_match.group(1).upper()
        action = "BUY" if raw_action in ("BUY", "LONG") else "SELL"

        raw_symbol = symbol_match.group(1).lower()
        symbol = self._SYMBOL_MAP.get(raw_symbol)
        if symbol is None:
            return None

        price_match = self._PRICE_RE.search(message_text)
        sl_match = self._SL_RE.search(message_text)
        tp_match = self._TP_RE.search(message_text)

        entry_price = float(price_match.group(1)) if price_match else None
        stop_loss = float(sl_match.group(1)) if sl_match else None
        take_profit = float(tp_match.group(1)) if tp_match else None

        # If no explicit entry price, check for a standalone number near the action
        if entry_price is None:
            standalone = re.search(
                r"\b(buy|sell|long|short)\b\s+\S+\s+(\d{2,}\.?\d*)",
                message_text, re.IGNORECASE,
            )
            if standalone:
                entry_price = float(standalone.group(2))

        logger.info("Regex fallback parsed: %s %s entry=%s SL=%s TP=%s",
                     action, symbol, entry_price, stop_loss, take_profit)

        return {
            "is_signal": True,
            "is_amendment": False,
            "action": action,
            "symbol": symbol,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": 0.6,
            "reason": "regex fallback (Claude unavailable)",
        }

    async def process_message(
        self,
        raw_message_id: int,
        channel_id: str,
        message_text: str,
        image_bytes: bytes | None = None,
    ) -> None:
        """Parse a Telegram message and publish signal events if applicable."""
        try:
            # Try Claude first, fall back to regex
            parsed = await self._parse_with_claude(
                channel_id, message_text, image_bytes
            )

            if parsed is None and message_text:
                parsed = self._parse_with_regex(message_text)

            if parsed is None:
                await self._db.store_parsed_signal(
                    raw_message_id=raw_message_id, is_signal=False
                )
                return

            is_signal = parsed.get("is_signal", False)
            is_amendment = parsed.get("is_amendment", False)

            # Store parsed result
            signal_id = await self._db.store_parsed_signal(
                raw_message_id=raw_message_id,
                is_signal=is_signal,
                is_amendment=is_amendment,
                action=parsed.get("action"),
                symbol=parsed.get("symbol"),
                entry_price=parsed.get("entry_price"),
                stop_loss=parsed.get("stop_loss"),
                take_profit=parsed.get("take_profit"),
                parser_confidence=parsed.get("confidence"),
                parse_model=self._config.model,
            )

            if not is_signal and not is_amendment:
                logger.debug("Message not a signal, skipping")
                return

            symbol = parsed.get("symbol")
            if symbol not in self._valid_symbols:
                logger.warning("Unknown symbol: %s, skipping", symbol)
                return

            # Check if channel is allowed to trade this instrument
            if not self._registry.is_instrument_allowed(channel_id, symbol):
                logger.info(
                    "Symbol %s not allowed for channel %s, skipping",
                    symbol, channel_id,
                )
                return

            if is_amendment:
                await self._handle_amendment(parsed, channel_id, signal_id)
            else:
                await self._handle_new_signal(parsed, channel_id, signal_id)

        except Exception:
            logger.exception("Error parsing message from channel %s", channel_id)

    async def _parse_with_claude(
        self,
        channel_id: str,
        message_text: str,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any] | None:
        """Send message to Claude for parsing."""
        # Build context with recent signals from same channel
        recent = await self._db.get_recent_signals(
            channel_id, self._config.amendment_window_minutes
        )
        context = ""
        if recent:
            context = "\n\nRECENT SIGNALS FROM THIS CHANNEL (for amendment detection):\n"
            for sig in recent[:5]:
                context += (
                    f"- {sig['action']} {sig['symbol']} "
                    f"entry={sig['entry_price']} SL={sig['stop_loss']} TP={sig['take_profit']}\n"
                )

        user_content: list[dict] = []

        # Add image if present
        if image_bytes:
            b64_image = base64.b64encode(image_bytes).decode("utf-8")
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64_image,
                },
            })

        # Add text
        user_prompt = f"Parse this trading signal message:\n\n\"{message_text}\""
        if context:
            user_prompt += context
        user_content.append({"type": "text", "text": user_prompt})

        try:
            response = await self._claude.messages.create(
                model=self._config.model,
                max_tokens=500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )

            response_text = response.content[0].text.strip()
            return self._extract_json(response_text)

        except anthropic.APITimeoutError:
            logger.warning("Claude API timeout parsing message")
            return None
        except anthropic.APIError as e:
            logger.warning("Claude API error: %s", e)
            return None

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        """Extract JSON from Claude's response, handling various formats."""
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        if "```" in text:
            start = text.find("```")
            end = text.find("```", start + 3)
            if end > start:
                block = text[start + 3:end].strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                try:
                    return json.loads(block)
                except json.JSONDecodeError:
                    pass

        # Try finding JSON object in text
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning("Could not extract JSON from Claude response: %s", text[:200])
        return None

    async def _handle_new_signal(
        self, parsed: dict, channel_id: str, signal_id: int
    ) -> None:
        """Process a new trading signal."""
        action_str = parsed.get("action", "").upper()
        symbol = parsed["symbol"]

        if action_str not in ("BUY", "SELL"):
            logger.warning("Invalid action: %s", action_str)
            return

        entry_price = parsed.get("entry_price")
        stop_loss = parsed.get("stop_loss")
        take_profit = parsed.get("take_profit")

        # If SL/TP missing, calculate from ATR
        if stop_loss is None or take_profit is None:
            atr_sl, atr_tp = await self._calculate_atr_levels(
                symbol, action_str, entry_price
            )
            if stop_loss is None:
                stop_loss = atr_sl
            if take_profit is None:
                take_profit = atr_tp

        # Validate entry price against current market
        if entry_price is not None:
            is_valid = await self._validate_entry_price(symbol, entry_price)
            if not is_valid:
                logger.warning(
                    "Entry price %.5f too far from market for %s, skipping",
                    entry_price, symbol,
                )
                return

        # Get current price if no entry specified
        if entry_price is None:
            try:
                tick = await self._mt5.symbol_info_tick(symbol)
                entry_price = tick.ask if action_str == "BUY" else tick.bid
            except Exception:
                logger.warning("Cannot get current price for %s", symbol)
                return

        # Validate SL/TP geometry
        if not self._validate_geometry(action_str, entry_price, stop_loss, take_profit):
            logger.warning(
                "Invalid SL/TP geometry for %s %s: entry=%.5f SL=%.5f TP=%.5f",
                action_str, symbol, entry_price, stop_loss or 0, take_profit or 0,
            )
            return

        channel_name = self._registry.get_channel_name(channel_id)
        signal = Signal(
            source=f"telegram:{channel_name}",
            symbol=symbol,
            action=SignalAction.BUY if action_str == "BUY" else SignalAction.SELL,
            strength=parsed.get("confidence", 0.7),
            timestamp=datetime.utcnow(),
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            channel_id=channel_id,
            metadata={"signal_id": signal_id, "reason": parsed.get("reason", "")},
        )

        logger.info(
            "Signal: %s %s @ %.5f SL=%.5f TP=%.5f (from %s, confidence=%.2f)",
            action_str, symbol, entry_price,
            stop_loss or 0, take_profit or 0,
            channel_name, parsed.get("confidence", 0),
        )

        await self._event_bus.publish(
            SignalEvent(timestamp=datetime.utcnow(), signal=signal)
        )

    async def _handle_amendment(
        self, parsed: dict, channel_id: str, signal_id: int
    ) -> None:
        """Process a signal amendment (SL/TP update for existing position)."""
        symbol = parsed.get("symbol")
        stop_loss = parsed.get("stop_loss")
        take_profit = parsed.get("take_profit")

        if stop_loss is None and take_profit is None:
            logger.debug("Amendment has no SL/TP, skipping")
            return

        # Find the open trade for this symbol from this channel
        open_trades = await self._db.get_open_trades()
        matching_trade = None
        for trade in open_trades:
            if trade["symbol"] == symbol and trade["channel_id"] == channel_id:
                matching_trade = trade
                break

        if matching_trade is None:
            logger.info(
                "Amendment for %s from channel %s but no open trade found",
                symbol, channel_id,
            )
            return

        channel_name = self._registry.get_channel_name(channel_id)
        logger.info(
            "Amendment: %s SL=%.5f TP=%.5f (ticket %d, from %s)",
            symbol,
            stop_loss or matching_trade.get("stop_loss", 0),
            take_profit or matching_trade.get("take_profit", 0),
            matching_trade["mt5_ticket"],
            channel_name,
        )

        modify = ModifyOrder(
            ticket=matching_trade["mt5_ticket"],
            symbol=symbol,
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal_id=signal_id,
        )

        await self._event_bus.publish(
            SignalAmendmentEvent(
                timestamp=datetime.utcnow(),
                modify_order=modify,
                channel_id=channel_id,
                symbol=symbol,
            )
        )

    async def _calculate_atr_levels(
        self, symbol: str, action: str, entry_price: float | None
    ) -> tuple[float | None, float | None]:
        """Calculate SL/TP from ATR when the channel doesn't provide them."""
        try:
            bars = await self._mt5.get_bars(symbol, "H1", count=20)
            if bars.empty or len(bars) < 14:
                return None, None

            atr = ta.atr(bars["high"], bars["low"], bars["close"], length=14)
            if atr is None or atr.empty:
                return None, None

            current_atr = float(atr.iloc[-1])

            # Get current price if entry not specified
            if entry_price is None:
                tick = await self._mt5.symbol_info_tick(symbol)
                entry_price = tick.ask if action == "BUY" else tick.bid

            sl_distance = current_atr * self._config.atr_sl_multiplier
            tp_distance = current_atr * self._config.atr_tp_multiplier

            if action == "BUY":
                return entry_price - sl_distance, entry_price + tp_distance
            else:
                return entry_price + sl_distance, entry_price - tp_distance

        except Exception:
            logger.warning("ATR calculation failed for %s", symbol)
            return None, None

    async def _validate_entry_price(
        self, symbol: str, entry_price: float
    ) -> bool:
        """Check if entry price is within threshold of current market price."""
        try:
            tick = await self._mt5.symbol_info_tick(symbol)
            mid_price = (tick.bid + tick.ask) / 2
            deviation_pct = abs(entry_price - mid_price) / mid_price * 100
            return deviation_pct <= self._config.stale_price_threshold_pct
        except Exception:
            # If we can't get price, allow the signal through
            return True

    @staticmethod
    def _validate_geometry(
        action: str,
        entry: float,
        sl: float | None,
        tp: float | None,
    ) -> bool:
        """Validate that SL/TP are on the correct side of entry price."""
        if sl is not None:
            if action == "BUY" and sl >= entry:
                return False
            if action == "SELL" and sl <= entry:
                return False
        if tp is not None:
            if action == "BUY" and tp <= entry:
                return False
            if action == "SELL" and tp >= entry:
                return False
        return True
