"""Claude AI signal filter — evaluates trade signals before execution.

Calls the Anthropic API with signal context and returns a confidence score.
Trades below the confidence threshold are skipped.

Usage in live bot:
    filter = ClaudeSignalFilter(api_key=os.environ["ANTHROPIC_API_KEY"])
    decision = await filter.evaluate(signal)
    if decision.confidence >= 0.65:
        place_trade(signal)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a professional forex/metals scalping analyst specialising in XAUUSD (Gold).
You evaluate trade signals and return a structured JSON decision.

Rules:
- Only approve signals that align with the higher-timeframe trend.
- Penalise signals taken during high-impact news windows or choppy regimes.
- Reward signals with tight SL relative to TP (R:R >= 1.5).
- Return ONLY valid JSON — no markdown, no explanation outside the JSON.
"""

_USER_TEMPLATE = """Evaluate this {strategy} trade signal on {symbol}:

Signal:
  Direction : {side}
  Entry     : {entry:.5f}
  Stop Loss : {sl:.5f}  ({sl_pips:.1f} pips)
  Take Profit: {tp:.5f}  ({tp_pips:.1f} pips)
  R:R ratio : {rr:.2f}

Market context:
  Regime    : {regime}
  Session   : {session}
  ATR(14)   : {atr:.5f}
  EMA200    : {ema200:.5f}  (price is {ema_relation} EMA200)
  RSI(14)   : {rsi:.1f}

Respond with JSON only:
{{
  "confidence": <float 0.0-1.0>,
  "verdict": "TAKE" | "SKIP",
  "reason": "<one sentence>"
}}"""


@dataclass
class SignalDecision:
    confidence: float
    verdict: str   # "TAKE" or "SKIP"
    reason: str

    @property
    def should_trade(self) -> bool:
        return self.verdict == "TAKE"


class ClaudeSignalFilter:
    """Evaluates trade signals using Claude claude-haiku-4-5-20251001 (fast + cheap)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-haiku-4-5-20251001",
        confidence_threshold: float = 0.65,
        timeout: float = 5.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model
        self._threshold = confidence_threshold
        self._timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError:
                raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
        return self._client

    def evaluate(self, signal: dict) -> SignalDecision:
        """Synchronous evaluation of a trade signal.

        Args:
            signal: dict with keys: strategy, symbol, side, entry, sl, tp,
                    regime, session, atr, ema200, rsi
        Returns:
            SignalDecision with confidence, verdict, reason
        """
        entry = signal["entry"]
        sl = signal["sl"]
        tp = signal.get("tp", entry)
        sl_pips = abs(entry - sl) / 0.01
        tp_pips = abs(tp - entry) / 0.01
        rr = tp_pips / sl_pips if sl_pips > 0 else 0.0
        ema200 = signal.get("ema200", entry)
        ema_relation = "above" if entry > ema200 else "below"

        prompt = _USER_TEMPLATE.format(
            strategy=signal.get("strategy", "unknown"),
            symbol=signal.get("symbol", "XAUUSD"),
            side=signal["side"],
            entry=entry,
            sl=sl,
            sl_pips=sl_pips,
            tp=tp,
            tp_pips=tp_pips,
            rr=rr,
            regime=signal.get("regime", "UNKNOWN"),
            session=signal.get("session", "UNKNOWN"),
            atr=signal.get("atr", 0.0),
            ema200=ema200,
            ema_relation=ema_relation,
            rsi=signal.get("rsi", 50.0),
        )

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self._model,
                max_tokens=128,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            data = json.loads(raw)
            return SignalDecision(
                confidence=float(data["confidence"]),
                verdict=data["verdict"],
                reason=data.get("reason", ""),
            )
        except Exception as exc:
            logger.warning("Claude filter error (%s) — defaulting to TAKE", exc)
            # Fail open: if Claude is unavailable, let the trade through
            return SignalDecision(confidence=0.5, verdict="TAKE", reason="filter unavailable")

    def should_take(self, signal: dict) -> bool:
        """Convenience wrapper — returns True if trade should be taken."""
        decision = self.evaluate(signal)
        logger.info(
            "Claude filter [%s] conf=%.2f %s — %s",
            signal.get("strategy", "?"), decision.confidence,
            decision.verdict, decision.reason,
        )
        return decision.confidence >= self._threshold and decision.should_trade
