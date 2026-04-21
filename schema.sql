-- =============================================================================
-- stk_fund Screener — Database Schema
-- Run this file directly in psql or your SQL client:
--   psql -U <user> -d stk_fund -f schema.sql
--
-- Schemas used:
--   classification  — ticker_symbol (existing)
--   technical       — ohlcv_daily, stock_indicators (new)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Alter existing classification.ticker_symbol
--    Adds the is_screener_active flag used to filter stocks for processing.
-- -----------------------------------------------------------------------------
ALTER TABLE classification.ticker_symbol
    ADD COLUMN IF NOT EXISTS is_screener_active BOOLEAN NOT NULL DEFAULT FALSE;


-- -----------------------------------------------------------------------------
-- 2. technical.ohlcv_daily
--    Stores daily OHLCV candles fetched from the Upstox API.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS technical.ohlcv_daily (
    id              BIGSERIAL       PRIMARY KEY,
    instrument_key  VARCHAR(50)     NOT NULL,
    trade_date      DATE            NOT NULL,
    open            NUMERIC(12, 4)  NOT NULL,
    high            NUMERIC(12, 4)  NOT NULL,
    low             NUMERIC(12, 4)  NOT NULL,
    close           NUMERIC(12, 4)  NOT NULL,
    volume          BIGINT          NOT NULL,
    created_at      TIMESTAMP       NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_ohlcv_daily UNIQUE (instrument_key, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_instrument_key ON technical.ohlcv_daily (instrument_key);
CREATE INDEX IF NOT EXISTS idx_ohlcv_trade_date     ON technical.ohlcv_daily (trade_date);


-- -----------------------------------------------------------------------------
-- 3. technical.stock_indicators
--    One row per (instrument_key, trade_date) with all computed indicator values.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS technical.stock_indicators (
    id                      BIGSERIAL       PRIMARY KEY,
    instrument_key          VARCHAR(50)     NOT NULL,
    trade_date              DATE            NOT NULL,
    computed_at             TIMESTAMP       NOT NULL DEFAULT NOW(),

    -- Price levels
    close                   NUMERIC(12, 4),
    high_52w                NUMERIC(12, 4),
    low_52w                 NUMERIC(12, 4),
    high_ytd                NUMERIC(12, 4),
    low_ytd                 NUMERIC(12, 4),
    pct_from_52w_high       NUMERIC(8,  4),
    pct_from_52w_low        NUMERIC(8,  4),

    -- Moving averages
    sma_20                  NUMERIC(12, 4),
    sma_50                  NUMERIC(12, 4),
    sma_100                 NUMERIC(12, 4),
    sma_200                 NUMERIC(12, 4),
    ema_9                   NUMERIC(12, 4),
    ema_21                  NUMERIC(12, 4),
    ema_50                  NUMERIC(12, 4),
    ema_200                 NUMERIC(12, 4),

    -- MACD
    macd_line               NUMERIC(12, 4),
    macd_signal             NUMERIC(12, 4),
    macd_histogram          NUMERIC(12, 4),

    -- Cross signals
    golden_cross_event      BOOLEAN,
    death_cross_event       BOOLEAN,
    golden_cross_state      VARCHAR(5),         -- 'above' | 'below'

    -- Trend
    adx_14                  NUMERIC(8,  4),

    -- Momentum / oscillators
    rsi_14                  NUMERIC(8,  4),
    stoch_k                 NUMERIC(8,  4),
    stoch_d                 NUMERIC(8,  4),
    cci_20                  NUMERIC(10, 4),
    williams_r_14           NUMERIC(8,  4),
    roc_10                  NUMERIC(10, 4),

    -- Volatility
    bb_upper                NUMERIC(12, 4),
    bb_middle               NUMERIC(12, 4),
    bb_lower                NUMERIC(12, 4),
    atr_14                  NUMERIC(12, 4),
    stddev_20               NUMERIC(12, 4),
    hist_volatility_20      NUMERIC(10, 6),

    -- Volume
    avg_volume_1m           NUMERIC(18, 2),
    avg_volume_1y           NUMERIC(18, 2),
    volume_ratio            NUMERIC(10, 4),
    obv                     BIGINT,
    vwap                    NUMERIC(12, 4),

    -- Pivot points (classic, based on prior day H/L/C)
    pivot_point             NUMERIC(12, 4),
    pivot_support_1         NUMERIC(12, 4),
    pivot_resistance_1      NUMERIC(12, 4),

    CONSTRAINT uq_stock_indicators UNIQUE (instrument_key, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_si_instrument_key ON technical.stock_indicators (instrument_key);
CREATE INDEX IF NOT EXISTS idx_si_trade_date     ON technical.stock_indicators (trade_date);


-- -----------------------------------------------------------------------------
-- 4. technical.screener_jobs
--    Persists recalculation job state across server restarts.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS technical.screener_jobs (
    job_id           UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    status           VARCHAR(10)     NOT NULL DEFAULT 'pending',  -- pending | running | completed | failed
    stocks_processed INT,
    error            TEXT,
    created_at       TIMESTAMP       NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMP
);
