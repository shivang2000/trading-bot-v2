-- Multi-tenant schema migration — additive, idempotent.
--
-- Apply with:
--   sqlite3 data/trading_bot_v2.db < scripts/init_multi_tenant_schema.sql
--
-- Adds first-class dimensions for `account_id`, `system_id`, and `signal_source`
-- to support concurrent multi-account, multi-propfirm, multi-system trading.
-- See orchestration-plan-v2.md → "Multi-tenant schema".
--
-- Existing tables (raw_messages, parsed_signals, trades, channel_stats) are
-- left intact. New tables coexist; trades is augmented via ALTER ADD COLUMN
-- with NULL defaults so legacy rows continue to read.
--
-- Migration is one-way; the live DB has no data (lost with the $5k EC2),
-- so a clean rebuild is the seed. Applying to a populated DB is also safe
-- (CREATE IF NOT EXISTS / ALTER ADD COLUMN with default-NULL).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ── Reference tables ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS propfirms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    rulebook    TEXT,
    status      TEXT DEFAULT 'active' CHECK (status IN ('active','retired')),
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS brokers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    platform    TEXT NOT NULL CHECK (platform IN ('mt5','mt4','ctrader','match-trader','tradelocker')),
    server      TEXT NOT NULL,
    timezone    TEXT DEFAULT 'UTC',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS systems (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    config_hash TEXT,
    notes       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (name, version, config_hash)
);

-- ── Accounts (multi-tenant core) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('propfirm','personal','demo')),
    propfirm_id       INTEGER REFERENCES propfirms(id),
    broker_id         INTEGER NOT NULL REFERENCES brokers(id),
    size_usd          NUMERIC,
    variant           TEXT,
    phase             TEXT,
    status            TEXT DEFAULT 'pending' CHECK (status IN ('pending','active','passed','bust','paused','retired')),
    mt5_login         INTEGER,
    overlay           TEXT,
    creds_ssm_prefix  TEXT,
    next_on_pass      TEXT REFERENCES accounts(id),
    payouts_fund      TEXT REFERENCES accounts(id),
    activated_at      TIMESTAMP,
    passed_at         TIMESTAMP,
    busted_at         TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_propfirm ON accounts(propfirm_id);

CREATE TABLE IF NOT EXISTS account_phases_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    prev_phase  TEXT,
    new_phase   TEXT NOT NULL,
    prev_status TEXT,
    new_status  TEXT NOT NULL,
    reason      TEXT,
    changed_by  TEXT,
    changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_phases_acct ON account_phases_history(account_id, changed_at);

CREATE TABLE IF NOT EXISTS account_equity_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            TEXT NOT NULL REFERENCES accounts(id),
    balance               NUMERIC,
    equity                NUMERIC,
    margin                NUMERIC,
    free_margin           NUMERIC,
    margin_level          NUMERIC,
    open_position_count   INTEGER,
    daily_pnl             NUMERIC,
    drawdown_pct          NUMERIC,
    peak_equity           NUMERIC,
    captured_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_equity_acct_time ON account_equity_snapshots(account_id, captured_at);

CREATE TABLE IF NOT EXISTS account_daily (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        TEXT NOT NULL REFERENCES accounts(id),
    date              DATE NOT NULL,
    opening_equity    NUMERIC,
    closing_equity    NUMERIC,
    daily_pnl         NUMERIC,
    daily_pnl_pct     NUMERIC,
    trade_count       INTEGER,
    winning_count     INTEGER,
    losing_count      INTEGER,
    max_drawdown_pct  NUMERIC,
    biggest_win       NUMERIC,
    biggest_loss      NUMERIC,
    rules_violated    TEXT,
    UNIQUE (account_id, date)
);

-- ── Signal-decision audit (every decision per account, not just executions) ──
CREATE TABLE IF NOT EXISTS signal_executions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    parsed_signal_id    INTEGER REFERENCES parsed_signals(id),
    account_id          TEXT NOT NULL REFERENCES accounts(id),
    system_id           INTEGER REFERENCES systems(id),
    decision            TEXT NOT NULL CHECK (decision IN (
        'executed','rejected_risk','rejected_news','rejected_prop_firm',
        'rejected_confidence','skipped_foreign','dry_run','rejected_rr','rejected_other'
    )),
    decision_reason     TEXT,
    trade_id            INTEGER,
    evaluated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sigex_acct ON signal_executions(account_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_sigex_signal ON signal_executions(parsed_signal_id);

-- ── Risk events (per-account, queryable per phase / per firm) ──────────────
CREATE TABLE IF NOT EXISTS risk_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id          TEXT NOT NULL REFERENCES accounts(id),
    system_id           INTEGER REFERENCES systems(id),
    kind                TEXT NOT NULL CHECK (kind IN (
        'rule_violation','rule_warning','prop_firm_breach','news_blocked',
        'drawdown_tier_triggered','foreign_position_detected','pre_news_flat_executed'
    )),
    limit_name          TEXT,
    limit_value         NUMERIC,
    current_value       NUMERIC,
    message             TEXT,
    related_trade_id    INTEGER,
    related_signal_id   INTEGER REFERENCES parsed_signals(id),
    occurred_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_risk_acct_time ON risk_events(account_id, occurred_at);

-- ── Augment trades + channel_stats ─────────────────────────────────────────
-- These ALTER statements require SQLite ≥ 3.35 (CHECK on new columns is not
-- supported; constraints enforced at app layer). Run via the Python loader
-- which inspects PRAGMA table_info first.
--
-- trades.account_id, trades.system_id, trades.signal_source, trades.magic,
-- trades.commission, trades.swap, trades.strategy_name, trades.parsed_signal_id
--
-- We use an idempotent helper (the Python migration runner checks
-- PRAGMA table_info before issuing each ALTER). For documentation:

-- ALTER TABLE trades ADD COLUMN account_id TEXT REFERENCES accounts(id);
-- ALTER TABLE trades ADD COLUMN system_id INTEGER REFERENCES systems(id);
-- ALTER TABLE trades ADD COLUMN signal_source TEXT;
-- ALTER TABLE trades ADD COLUMN strategy_name TEXT;
-- ALTER TABLE trades ADD COLUMN parsed_signal_id INTEGER REFERENCES parsed_signals(id);
-- ALTER TABLE trades ADD COLUMN magic INTEGER;
-- ALTER TABLE trades ADD COLUMN commission NUMERIC DEFAULT 0;
-- ALTER TABLE trades ADD COLUMN swap NUMERIC DEFAULT 0;
-- (UNIQUE constraint on (account_id, mt5_ticket) added by Python loader as a
--  separate index since SQLite cannot add UNIQUE constraints to existing tables)

-- ALTER TABLE channel_stats ADD COLUMN account_id TEXT REFERENCES accounts(id);
-- ALTER TABLE channel_stats ADD COLUMN expectancy NUMERIC DEFAULT 0;
-- ALTER TABLE channel_stats ADD COLUMN sample_size INTEGER;

-- ── bot_state — per-account composite key ──────────────────────────────────
-- Replaces the single-tenant bot_state table. The Python loader migrates
-- existing rows to a "default" account_id during the first run.
CREATE TABLE IF NOT EXISTS bot_state_v2 (
    account_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, key)
);

-- ── Seed default rows so the bot can run before YAML configs are populated ─
INSERT OR IGNORE INTO propfirms (slug, name, rulebook) VALUES
    ('fundingpips', 'FundingPips', 'config/propfirms/fundingpips.yaml'),
    ('ftm',         'Funded Trader Markets', 'config/propfirms/ftm.yaml'),
    ('vantage',     'Vantage (personal/demo)', NULL);

INSERT OR IGNORE INTO brokers (name, platform, server, timezone) VALUES
    ('FundingPips-Demo', 'mt5', 'FundingPips-Demo', 'UTC'),
    ('FundingPips-Live', 'mt5', 'FundingPips-Live', 'UTC'),
    ('Vantage-Demo',     'mt5', 'VantageInternational-Demo', 'UTC'),
    ('Vantage-Live',     'mt5', 'VantageInternational-Live', 'UTC');
