-- ============================================================================
-- Algo Trading Platform — Azure SQL Schema
-- Translated from Postgres (ariqt-algo-trading-db-001) to T-SQL for algodb
-- ============================================================================
--
-- KEY TRANSLATION DECISIONS (read before running):
--
-- 1. SEQUENCE + nextval() -> IDENTITY(1,1)
--    Postgres used separate SEQUENCE objects for auto-increment PKs.
--    T-SQL's IDENTITY does the same job in one line, no separate object needed.
--
-- 2. "timestamp with time zone" -> DATETIMEOFFSET
--    T-SQL has no direct equivalent name, but DATETIMEOFFSET stores the same
--    thing (date/time + UTC offset). now() -> SYSDATETIMEOFFSET().
--
-- 3. text -> NVARCHAR(n) or NVARCHAR(MAX)
--    Postgres TEXT is unbounded by default. T-SQL NVARCHAR needs a length.
--    Sizes below are reasonable GUESSES based on what the column holds
--    (stock symbols, timeframe labels, etc) — verify against your actual
--    data before trusting them blindly, especially instrument_key /
--    access_token which could run longer than expected (JWTs, Upstox keys).
--
-- 4. Reserved words: [key], [open], [close], [timestamp]
--    All four are reserved or special-meaning words in T-SQL and MUST stay
--    bracketed everywhere they're used — not just here, but in every query
--    the app code writes against these tables (Phase 4 concern).
--
-- 5. numeric with NO precision/scale (charges, net_pnl, peak_price,
--    initial_stop_distance in paper_positions) — Postgres allows unbounded
--    numeric; T-SQL requires a precision. Set to DECIMAL(18,4) as a
--    reasonable default for money/price fields — reconsider if you expect
--    values needing more precision.
--
-- ============================================================================

-- ----------------------------------------------------------------------------
-- alert_states
-- ----------------------------------------------------------------------------
CREATE TABLE alert_states (
    id BIGINT IDENTITY(1,1) NOT NULL,
    stock NVARCHAR(50) NOT NULL,
    timeframe NVARCHAR(20) NOT NULL,
    signal NVARCHAR(20) NOT NULL,
    updated_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET(),
    strategy NVARCHAR(50) NOT NULL DEFAULT N'RSI Reversal',
    CONSTRAINT alert_states_pkey PRIMARY KEY (id),
    CONSTRAINT alert_states_stock_timeframe_strategy_key UNIQUE (stock, timeframe, strategy)
);
GO

CREATE INDEX idx_alert_states_stock_tf_strat ON alert_states (stock, timeframe, strategy);
GO

-- ----------------------------------------------------------------------------
-- app_config
-- ----------------------------------------------------------------------------
CREATE TABLE app_config (
    id BIGINT IDENTITY(1,1) NOT NULL,
    [key] NVARCHAR(100) NOT NULL,
    value NVARCHAR(MAX) NULL,
    updated_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT app_config_pkey PRIMARY KEY (id),
    CONSTRAINT app_config_key_key UNIQUE ([key])
);
GO

-- ----------------------------------------------------------------------------
-- backtest_results
-- ----------------------------------------------------------------------------
CREATE TABLE backtest_results (
    id BIGINT IDENTITY(1,1) NOT NULL,
    symbol NVARCHAR(50) NOT NULL,
    name NVARCHAR(200) NULL,
    timeframe NVARCHAR(20) NOT NULL,
    category NVARCHAR(50) NULL,
    strategy NVARCHAR(50) DEFAULT N'RSI Reversal',
    trades INT DEFAULT 0,
    pnl DECIMAL(12,2) DEFAULT 0,
    pnl_pct DECIMAL(8,2) DEFAULT 0,
    win_rate DECIMAL(5,1) DEFAULT 0,
    wins INT DEFAULT 0,
    losses INT DEFAULT 0,
    period NVARCHAR(50) NULL,
    updated_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT backtest_results_pkey PRIMARY KEY (id),
    CONSTRAINT backtest_results_symbol_timeframe_strategy_key UNIQUE (symbol, timeframe, strategy)
);
GO

CREATE INDEX idx_backtest_symbol_tf ON backtest_results (symbol, timeframe);
GO

-- ----------------------------------------------------------------------------
-- live_candles_1min
-- No surrogate id — composite primary key, same as Postgres original.
-- ----------------------------------------------------------------------------
CREATE TABLE live_candles_1min (
    instrument_key NVARCHAR(50) NOT NULL,
    symbol NVARCHAR(50) NOT NULL,
    ts DATETIMEOFFSET NOT NULL,
    [open] DECIMAL(12,2) NOT NULL,
    high DECIMAL(12,2) NOT NULL,
    low DECIMAL(12,2) NOT NULL,
    [close] DECIMAL(12,2) NOT NULL,
    volume BIGINT DEFAULT 0,
    updated_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT live_candles_1min_pkey PRIMARY KEY (instrument_key, ts)
);
GO

CREATE INDEX idx_live_candles_symbol_ts ON live_candles_1min (symbol, ts DESC);
GO

-- ----------------------------------------------------------------------------
-- paper_positions
-- ----------------------------------------------------------------------------
CREATE TABLE paper_positions (
    id INT IDENTITY(1,1) NOT NULL,
    symbol NVARCHAR(50) NOT NULL,
    side NVARCHAR(10) NOT NULL,
    quantity INT NOT NULL,
    entry_price DECIMAL(12,2) NOT NULL,
    stop_loss DECIMAL(12,2) NOT NULL,
    target DECIMAL(12,2) NOT NULL,
    strategy NVARCHAR(50) NOT NULL,
    timeframe NVARCHAR(20) NOT NULL,
    risk_amount DECIMAL(12,2) DEFAULT 0,
    order_id NVARCHAR(100) DEFAULT N'',
    status NVARCHAR(20) NOT NULL DEFAULT N'OPEN',
    exit_price DECIMAL(12,2) NULL,
    exit_reason NVARCHAR(200) NULL,
    pnl DECIMAL(12,2) NULL,
    opened_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    closed_at DATETIMEOFFSET NULL,
    charges DECIMAL(18,4) DEFAULT 0,
    net_pnl DECIMAL(18,4) DEFAULT 0,
    peak_price DECIMAL(18,4) NULL,
    initial_stop_distance DECIMAL(18,4) NULL,
    CONSTRAINT paper_positions_pkey PRIMARY KEY (id)
);
GO

CREATE INDEX idx_paper_positions_status_closed ON paper_positions (status, closed_at);
GO
CREATE INDEX idx_paper_positions_status_symbol ON paper_positions (status, symbol);
GO

-- ----------------------------------------------------------------------------
-- pending_breakouts (Sep 2 — fast breakout watch)
-- A pattern strategy (e.g. "3 Bar Play") can find a valid setup on the
-- normal 5-min scan whose breakout hasn't happened yet. That gets upserted
-- here so a faster, per-symbol checker (core/marketdata/ws_listener.py) can
-- watch just this handful of symbols every ~60s instead of waiting up to 5
-- more minutes for the next full-market scan to notice the breakout.
-- ----------------------------------------------------------------------------
CREATE TABLE pending_breakouts (
    id BIGINT IDENTITY(1,1) NOT NULL,
    symbol NVARCHAR(50) NOT NULL,
    strategy NVARCHAR(50) NOT NULL,
    timeframe NVARCHAR(20) NOT NULL,
    side NVARCHAR(10) NOT NULL,
    trigger_price DECIMAL(12,2) NOT NULL,
    stop_loss DECIMAL(12,2) NOT NULL,
    target DECIMAL(12,2) NOT NULL,
    strength NVARCHAR(20) NULL,
    status NVARCHAR(20) NOT NULL DEFAULT N'PENDING',
    detected_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    expires_at DATETIMEOFFSET NOT NULL,
    updated_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT pending_breakouts_pkey PRIMARY KEY (id),
    CONSTRAINT pending_breakouts_symbol_strategy_timeframe_key UNIQUE (symbol, strategy, timeframe)
);
GO

CREATE INDEX idx_pending_breakouts_status_expires ON pending_breakouts (status, expires_at);
GO

-- ----------------------------------------------------------------------------
-- trade_anatomy (Sep 9) -- the flagpole/consolidation/breakout candles a
-- pattern strategy (currently only "3 Bar Play") actually used to compute a
-- trade's stop/target, persisted at open time so reports/charts can show
-- the exact candles involved without reconstructing them after the fact
-- from raw ticks. A child table (not columns on paper_positions) since this
-- is 1-to-many-candle data (up to 2 consolidation rows) that doesn't fit
-- flat columns cleanly, and generalizes if another pattern strategy is
-- added later.
-- ----------------------------------------------------------------------------
CREATE TABLE trade_anatomy (
    id BIGINT IDENTITY(1,1) NOT NULL,
    position_id INT NOT NULL,
    role NVARCHAR(20) NOT NULL,        -- 'FLAGPOLE' | 'CONSOLIDATION' | 'BREAKOUT'
    seq INT NOT NULL DEFAULT 0,        -- 0/1 for up to 2 consolidation bars, else 0
    candle_ts DATETIMEOFFSET NOT NULL,
    [open] DECIMAL(12,2) NOT NULL,
    high DECIMAL(12,2) NOT NULL,
    low DECIMAL(12,2) NOT NULL,
    [close] DECIMAL(12,2) NOT NULL,
    volume BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT trade_anatomy_pkey PRIMARY KEY (id),
    CONSTRAINT fk_trade_anatomy_position FOREIGN KEY (position_id) REFERENCES paper_positions(id)
);
GO

CREATE INDEX idx_trade_anatomy_position ON trade_anatomy (position_id);
GO

-- ----------------------------------------------------------------------------
-- signals
-- NOTE: [timestamp] must stay bracketed in every query that touches it.
-- ----------------------------------------------------------------------------
CREATE TABLE signals (
    id BIGINT IDENTITY(1,1) NOT NULL,
    [timestamp] DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    stock NVARCHAR(50) NOT NULL,
    timeframe NVARCHAR(20) NOT NULL,
    signal NVARCHAR(20) NOT NULL,
    rsi DECIMAL(6,2) NULL,
    price DECIMAL(12,2) NULL,
    strategy NVARCHAR(50) DEFAULT N'RSI Reversal',
    CONSTRAINT signals_pkey PRIMARY KEY (id)
);
GO

CREATE INDEX idx_signals_stock_tf ON signals (stock, timeframe);
GO
CREATE INDEX idx_signals_strategy ON signals (strategy);
GO
CREATE INDEX idx_signals_timestamp ON signals ([timestamp] DESC);
GO

-- ----------------------------------------------------------------------------
-- upstox_tokens
-- access_token as NVARCHAR(MAX) since OAuth tokens can be long / unpredictable length.
-- ----------------------------------------------------------------------------
CREATE TABLE upstox_tokens (
    id BIGINT IDENTITY(1,1) NOT NULL,
    access_token NVARCHAR(MAX) NOT NULL,
    created_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET(),
    expires_at DATETIMEOFFSET NULL,
    CONSTRAINT upstox_tokens_pkey PRIMARY KEY (id)
);
GO