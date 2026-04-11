# Trading Strategy Research — Video Analysis (27 Videos)

## NEW IMPLEMENTABLE STRATEGIES

### 1. Waka Waka Grid Recovery (from nO6kao0Od40, aDWDJrACs7s, dV6-h17m6Ag)
**Type:** Grid/Martingale with RSI entry
**Automatable:** YES — fully codeable
**Risk Level:** HIGH (martingale can blow accounts)

**Core Logic:**
- Timeframe: M15
- Entry: RSI(20) crosses above 65 → SELL, below 35 → BUY (first trade)
- Grid: After first trade, place additional orders every 35 pips in losing direction
- Smart distance: Grid spacing = 35 * (ATR(96) / ATR(672)), clamped to [1.0, 1.5] multiplier
- Lot sizing: Martingale multipliers — Level 1: 1x, Level 2: 1x, Level 3-5: 2x each, Level 6+: 1.6x
- Take profit: Weighted average price of all grid positions + 10 pips
- Bollinger Band smart TP: TP = percentage of BB width from weighted average
- News filter: Disable trading during high-impact news
- Rollover filter: No trading 23:45-00:15 (spread spikes)
- Max drawdown control: Close all if floating DD exceeds threshold

**Lot sizing methods:**
- Low risk: 0.25% deposit load
- Medium risk: 0.5% deposit load  
- Significant: 1.0% deposit load
- Dynamic: 0.01 lot per $10,000 balance

**VERDICT:** DO NOT implement for prop firm — martingale violates 5% daily loss / 10% max DD rules. 
One bad grid = instant account breach. Good for personal accounts with withdrawal strategy only.

---

### 2. Tight Stop-Loss Scalping Robot (from 9DIjI-ZxUMU, av9uak6H9Ck, 9zoeRuDK5ec)
**Type:** Momentum scalper with aggressive trailing SL
**Automatable:** YES — fully codeable, very similar to what we already have
**Risk Level:** MEDIUM

**Core Logic:**
- Timeframe: M5
- Entry: Place stop orders at last candle high (BUY STOP) and last candle low (SELL STOP)
- TP: 20 pips (Forex), percentage-based for Gold/BTC/indices
- SL: 20 pips (Forex), percentage-based for others
- Trailing activation: After 1.5 pips profit, start trailing
- Trailing distance: 1 pip (extremely tight)
- Session: 7:00 - 21:00 UTC
- Risk per trade: 2-3%

**Gold-specific parameters:**
- TP: percentage of current price (e.g., 0.5% of price)
- SL: percentage of current price
- TSL: percentage of TP (e.g., 10% of TP distance)
- Best during London-NY overlap

**US30/Indices parameters:**
- Same percentage-based approach
- Best during US session only

**Results claimed:** 90-93% win rate, 10-11% max DD on backtests

**Filters (from Part 3 - 9zoeRuDK5ec):**
- Moving average filter: Don't trade if price is too far from MA
- RSI filter: Don't trade if RSI > 75 or RSI < 25 (overbought/oversold)
- News filter: Close all pending orders and stop trading before major news

**VERDICT:** HIGH PRIORITY — This is very similar to our existing trailing stop approach but with tighter parameters. The key insight is the EXTREMELY tight trailing (1 pip after 1.5 pip profit). We should test these parameters.

**Implementation notes:**
- Use last high/low for stop order placement (we currently use indicator-based entries)
- Percentage-based SL/TP for non-forex instruments (we just implemented live tick values)
- The 1-pip aggressive trail is what drives the 90%+ win rate

---

### 3. Opening Range Breakout + Orderflow (from cUTsoU-15Tc)
**Type:** Session breakout with volume profile confirmation
**Automatable:** PARTIALLY — breakout is codeable, orderflow needs tick data

**Core Logic (Model 1 — Volume Profile):**
- Wait for first 15-30 minutes of NY session (9:30-10:00 EST)
- Define range: High and Low of that period = IVB Top and IVB Bottom
- Calculate volume profile POC (Point of Control) within the range
- On breakout above IVB Top: BUY, entry at retrace to VAH-POC zone
- On breakout below IVB Bottom: SELL, entry at retrace to VAL-POC zone
- SL: Below Value Area Low (for longs) / Above Value Area High (for shorts)
- TP1: Statistical high probability target (65-70% hit rate)
- TP2: Extended target

**Core Logic (Model 2 — Orderflow Confirmation):**
- Same as Model 1 but wait for absorption/exhaustion confirmation
- Aggressive sellers hitting bid with zero result = absorption = BUY signal
- Aggressive buyers with momentum = momentum entry with tight SL

**VERDICT:** MEDIUM PRIORITY — The basic breakout logic (first 30 min range of NY session) is easy to implement and statistically profitable. Volume profile POC requires tick data we don't currently have. We CAN implement the basic version (similar to our London Breakout but for NY session).

**Implementation as "NY Opening Range Breakout":**
```
Session: NY open (14:30-15:00 UTC)
Range: High/Low of first 30 minutes
Entry: Breakout of range + retrace to 50% of range
SL: Opposite side of range
TP: 1.5x-2x range width
```

---

### 4. 50 EMA + No-Gap Candles Multi-Timeframe (from A8ncoQCPjF8)
**Type:** Trend-following with EMA confluence
**Automatable:** PARTIALLY — EMA logic yes, no-gap candles hard to replicate

**Core Logic:**
- Trend: Weekly → Daily → 4H must agree (2/3 minimum)
- Area of Interest: Support/resistance with 3+ touches (look left)
- Entry signal: Bullish/bearish engulfing candle at area of interest
- EMA confluence: Price must be above 50 EMA for buys, below for sells
- Multi-TF EMA check: More timeframes above/below EMA = stronger trade
- Key timeframes: Weekly EMA tap > Daily EMA tap > 4H EMA tap (in order of importance)

**VERDICT:** LOW PRIORITY for bot — this is primarily a manual discretionary strategy. The EMA(50) as trend filter is already in our strategies. The "area of interest" detection (3+ touches at a level) would require complex support/resistance detection.

---

### 5. Power of Three / AMD Cycle (from xW4JRisKBkQ)
**Type:** ICT-based Accumulation-Manipulation-Distribution
**Automatable:** YES — we already have m5_amd_cycle strategy!

**Core Logic (from video, confirming our implementation):**
- Accumulation: Asian session consolidation range
- Manipulation: London open fake breakout (sweep of Asian high/low)
- Distribution: True directional move during NY session
- Entry: After manipulation sweep, enter in opposite direction
- SL: Beyond manipulation wick
- TP: Opposite side of accumulation range + extension

**VERDICT:** ALREADY IMPLEMENTED as m5_amd_cycle — our best US30 strategy (82% WR). Video confirms our approach is correct.

---

### 6. Claude AI Trading Bot Architecture (from 870mvc3ZeEQ, vfjRUBcz-48, scj3NbqzYds)
**Type:** AI-powered strategy development
**Automatable:** N/A — architecture/methodology insights

**Key insights:**
- **Self-improving backtesting** (scj3NbqzYds): Use VectorBT for vectorized backtesting (100x faster than bar-by-bar). Run optimization loops where AI analyzes results and adjusts parameters.
- **Multi-bot architecture** (vfjRUBcz-48): Run 4 bots in parallel with different strategies, monitor aggregate performance
- **Stress testing** (870mvc3ZeEQ): Test strategy on extreme market conditions, flash crashes, and high-volatility periods before deploying

**VERDICT:** METHODOLOGY — Not a strategy, but useful techniques for our backtesting framework. We could add VectorBT for faster backtesting.

---

## STRATEGIES TO ADD TO OUR BOT (Priority Order)

### Priority 1: Tight SL Scalping Robot
- **Why:** 90%+ win rate claimed, very similar to our existing framework
- **What to implement:** New entry method (last high/low stop orders), 1-pip aggressive trailing after 1.5-pip profit, percentage-based SL/TP for US30/Gold
- **Estimated effort:** 1 new strategy file (~200 lines)
- **Instruments:** XAUUSD, US30, USDJPY, GBPUSD, EURUSD

### Priority 2: NY Opening Range Breakout  
- **Why:** Statistically proven since 1990, 65-70% probability of hitting TP1
- **What to implement:** First 30-min range of NY session, breakout entry with retrace, 1.5-2x range TP
- **Estimated effort:** 1 new strategy file (~150 lines)  
- **Instruments:** US30, XAUUSD, ES (S&P futures if available)

### Priority 3: RSI Filter Enhancement
- **Why:** Multiple videos confirm RSI(14) extremes (>75, <25) should BLOCK entries, not trigger them
- **What to implement:** Add RSI overbought/oversold filter to existing strategies
- **Estimated effort:** ~20 lines per strategy

### NOT Recommended:
- **Waka Waka Grid** — Martingale kills prop firm accounts. Skip entirely.
- **No-Gap Candles** — TradingView specific, can't replicate on MT5 data
- **Orderflow Model 2** — Needs tick-by-tick data we don't have

## INSTRUMENT-SPECIFIC NOTES

### XAUUSD (Gold)
- Best during London-NY overlap
- EMA(50) as dynamic support/resistance
- Evening star / engulfing at resistance = high probability sells
- Percentage-based SL/TP works better than fixed pips due to price volatility

### US30 (Dow Jones)
- AMD Cycle is the best strategy (confirmed by both backtests and video research)
- Best during US session (14:30-21:00 UTC)
- Opening range breakout of first 30 min is highly effective
- Tight SL scalping works well during trending periods
- Struggles in sideways/choppy markets

### Forex (USDJPY, GBPUSD, EURUSD)
- Tight SL scalping robot designed specifically for these pairs
- 20 pip TP/SL, 1.5 pip trail trigger, 1 pip trail distance
- Best during London + NY sessions (7:00-21:00)
