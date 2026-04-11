# Open Questions

## trading-bot-v2-improvements - 2026-03-30

- [ ] Should Friday auto-close hour be configurable per instrument or is 21:00 UTC universal? -- Gold (XAUUSD) market closes at different times than forex pairs; using the wrong close hour could either leave positions open too long or close them prematurely.

- [ ] What is the actual peak equity history for the current prop firm account? -- If the account has already grown and the peak equity was never persisted, we need to manually set the correct peak before enabling the fix, otherwise the first restart after the fix will use an incorrect baseline.

- [ ] Should PropFirmGuard's periodic equity check (P0-1) close ALL positions on breach, or selectively close the worst performers first? -- Closing everything is simpler and safer, but selective closing could preserve winning trades. User preference on risk tolerance needed.

- [ ] Is the bot currently running on a step1, step2, or master (funded) FundingPips account? -- The prop firm phase affects profit targets, trade idea limits, and risk tolerances. The config defaults to "step1" but the actual account phase may have changed.

- [ ] What is the desired daily trade limit for the current account phase? -- Config defaults to `max_daily_trades: 10`, but with only 2 strategies active on M5 timeframe, this might be too conservative or too generous depending on the trading style.

- [ ] Are there any FundingPips-specific rules beyond daily loss and max DD that need to be enforced? -- E.g., minimum trading days, maximum lot sizes, restricted instruments, news trading restrictions. Some prop firms have rules not captured in the current PropFirmConfig.
