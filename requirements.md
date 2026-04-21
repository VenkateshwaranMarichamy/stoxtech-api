# Requirements Document

## Introduction

This feature adds a Stock Technical Analysis Screener to the existing `stk_fund` project. The screener pulls historical OHLCV (Open, High, Low, Close, Volume) data from the Upstox API for up to 5,000 NSE/BSE stocks, stores it in the local PostgreSQL database (`stk_fund`), computes a comprehensive set of technical indicators using Python, and exposes the results through a FastAPI BFF (Backend-for-Frontend) to a React frontend.

The system is designed for an algo trader who screens ~2,500 actively traded stocks daily. Low-volume/low-value stocks are filtered out to reduce noise. Phase 1 covers data ingestion, indicator calculation, and API exposure. Alerts and push notifications are deferred to Phase 2.

---

## Glossary

- **OHLCV**: Open, High, Low, Close, Volume — the standard daily price and volume data for a stock.
- **Screener**: The subsystem that computes and stores technical indicator values for all active stocks.
- **Active Stock**: A stock in the `ticker_symbol` table where `is_screener_active = true`.
- **Bootstrap Job**: A one-time batch process that pulls 1-year OHLCV data for all stocks and sets the `is_screener_active` flag based on traded value thresholds.
- **Daily Sync Job**: A scheduled process that pulls the latest OHLCV data for all active stocks after market close.
- **Indicator Engine**: The Python module (using pandas-ta) that computes all technical indicators from stored OHLCV data.
- **Indicators Table**: The PostgreSQL table that stores computed indicator values per stock per date.
- **BFF**: Backend-for-Frontend — a FastAPI Python service that sits between the React frontend and PostgreSQL.
- **Upstox API**: The market data provider. Uses `instrument_key` (e.g., `NSE_EQ|INE002A01018`) to identify stocks.
- **Traded Value**: Daily traded value approximated as `close_price × volume` for a given day.
- **SMA**: Simple Moving Average — arithmetic mean of closing prices over N periods.
- **EMA**: Exponential Moving Average — weighted moving average giving more weight to recent prices.
- **MACD**: Moving Average Convergence Divergence — difference between 12-period and 26-period EMA, with a 9-period signal line.
- **RSI**: Relative Strength Index — momentum oscillator measuring speed and magnitude of price changes (0–100 scale).
- **ADX**: Average Directional Index — measures trend strength (0–100 scale).
- **ATR**: Average True Range — measures market volatility.
- **OBV**: On-Balance Volume — cumulative volume indicator tracking buying/selling pressure.
- **VWAP**: Volume Weighted Average Price — average price weighted by volume.
- **Bollinger Bands**: Volatility bands placed above and below a 20-period SMA at ±2 standard deviations.
- **Golden Cross**: Event where the 50-period SMA crosses above the 200-period SMA.
- **Death Cross**: Event where the 50-period SMA crosses below the 200-period SMA.
- **52-Week High/Low**: The highest and lowest closing price over the past 252 trading days.
- **YTD High/Low**: The highest and lowest closing price since January 1 of the current calendar year.
- **Pivot Points**: Classic daily support and resistance levels calculated from the prior day's High, Low, and Close.
- **Historical Volatility**: Annualized standard deviation of daily log returns.
- **pandas-ta**: Python library providing one-line computation of 130+ technical indicators.
- **Rate Limiting**: Upstox API enforces request-per-second limits; the system must respect these to avoid bans.

---

## Requirements

### Requirement 1: Stock Universe Management

**User Story:** As an algo trader, I want to maintain a filtered universe of actively traded stocks, so that the screener only processes stocks with meaningful liquidity and avoids noise from illiquid instruments.

#### Acceptance Criteria

1. THE `ticker_symbol` Table SHALL contain a boolean column `is_screener_active` that controls whether a stock is included in screener processing.
2. WHEN the Bootstrap Job runs, THE `ticker_symbol` Table SHALL have `is_screener_active` set to `true` for all stocks whose average daily traded value (calculated as `avg(close × volume)` over the most recent 30 trading days) meets or exceeds the configured minimum threshold.
3. WHEN the Bootstrap Job runs, THE `ticker_symbol` Table SHALL have `is_screener_active` set to `false` for all stocks whose average daily traded value falls below the configured minimum threshold.
4. THE Screener Configuration SHALL store the minimum average daily traded value threshold as a configurable parameter (default: ₹1,00,00,000 — one crore rupees).
5. THE `ticker_symbol` Table SHALL retain the existing `instrument_key` column used to identify stocks in Upstox API calls.

---

### Requirement 2: Bootstrap Data Ingestion

**User Story:** As an algo trader, I want to perform a one-time bulk import of 1-year OHLCV data for all stocks, so that the system has enough historical data to compute all technical indicators from day one.

#### Acceptance Criteria

1. WHEN the Bootstrap Job is triggered, THE Data_Ingestion_Module SHALL fetch 1-year daily OHLCV data for every stock in the `ticker_symbol` table using the Upstox historical data API.
2. WHEN fetching data for each stock, THE Data_Ingestion_Module SHALL use the stock's `instrument_key` to construct the Upstox API request.
3. WHEN the Upstox API returns OHLCV data for a stock, THE Data_Ingestion_Module SHALL store each trading day's record in the `ohlcv_daily` table with columns: `instrument_key`, `trade_date`, `open`, `high`, `low`, `close`, `volume`.
4. WHEN the Upstox API returns a rate-limit error (HTTP 429), THE Data_Ingestion_Module SHALL pause for a configurable delay (default: 1 second) before retrying the request.
5. WHEN the Upstox API returns a non-retryable error for a stock, THE Data_Ingestion_Module SHALL log the error with the stock's `instrument_key` and continue processing the remaining stocks.
6. WHEN a stock's OHLCV record for a given `trade_date` already exists in the `ohlcv_daily` table, THE Data_Ingestion_Module SHALL skip insertion for that record (upsert behavior).
7. THE `ohlcv_daily` Table SHALL enforce a unique constraint on (`instrument_key`, `trade_date`).

---

### Requirement 3: Daily OHLCV Sync

**User Story:** As an algo trader, I want the system to automatically fetch the latest OHLCV data each day after market close, so that all technical indicators remain current without manual intervention.

#### Acceptance Criteria

1. WHEN the Daily Sync Job runs, THE Data_Ingestion_Module SHALL fetch the most recent trading day's OHLCV data only for stocks where `is_screener_active = true` in the `ticker_symbol` table.
2. WHEN the Daily Sync Job fetches data for a stock, THE Data_Ingestion_Module SHALL upsert the record into the `ohlcv_daily` table.
3. WHEN the Daily Sync Job completes, THE Data_Ingestion_Module SHALL log the total number of stocks updated, skipped, and failed.
4. THE Daily Sync Job SHALL be executable as a standalone Python script suitable for scheduling via cron (e.g., `python sync_daily.py`).
5. WHEN the Daily Sync Job encounters a rate-limit error, THE Data_Ingestion_Module SHALL apply the same retry-with-delay behavior defined in Requirement 2, Criterion 4.

---

### Requirement 4: Technical Indicator Calculation

**User Story:** As an algo trader, I want the system to compute a comprehensive set of technical indicators for each active stock, so that I can analyse trend, momentum, volatility, and volume signals in one place.

#### Acceptance Criteria

1. WHEN the Indicator Engine runs for a stock, THE Indicator_Engine SHALL compute all indicators listed in Criteria 2–5 using the stock's OHLCV data from the `ohlcv_daily` table.
2. THE Indicator_Engine SHALL compute the following **Price Level** indicators:
   - 52-week high (highest close over the past 252 trading days)
   - 52-week low (lowest close over the past 252 trading days)
   - YTD high (highest close from January 1 of the current year to the most recent trading day)
   - YTD low (lowest close from January 1 of the current year to the most recent trading day)
   - Percentage distance from 52-week high: `((close - 52w_high) / 52w_high) × 100`
   - Percentage distance from 52-week low: `((close - 52w_low) / 52w_low) × 100`
3. THE Indicator_Engine SHALL compute the following **Trend / Moving Average** indicators:
   - SMA 20, SMA 50, SMA 100, SMA 200 (simple moving averages of closing price)
   - EMA 9, EMA 21, EMA 50, EMA 200 (exponential moving averages of closing price)
   - MACD line (EMA 12 − EMA 26), Signal line (EMA 9 of MACD), MACD Histogram (MACD − Signal)
   - Golden Cross flag: `true` when SMA 50 crosses above SMA 200 on the most recent trading day
   - Death Cross flag: `true` when SMA 50 crosses below SMA 200 on the most recent trading day
   - Golden/Death Cross state: `"above"` when SMA 50 > SMA 200, `"below"` when SMA 50 < SMA 200
   - ADX (14-period Average Directional Index)
4. THE Indicator_Engine SHALL compute the following **Momentum / Oscillator** indicators:
   - RSI (14-period Relative Strength Index)
   - Stochastic %K and %D (14-period)
   - CCI (20-period Commodity Channel Index)
   - Williams %R (14-period)
   - ROC (10-period Rate of Change)
5. THE Indicator_Engine SHALL compute the following **Volatility** indicators:
   - Bollinger Band Upper, Middle (SMA 20), and Lower (±2 standard deviations)
   - ATR (14-period Average True Range)
   - Standard Deviation of closing price (20-period)
   - Historical Volatility (annualized standard deviation of daily log returns, 20-period)
6. THE Indicator_Engine SHALL compute the following **Volume** indicators:
   - Average 1-month volume (average daily volume over the past 21 trading days)
   - Average 1-year volume (average daily volume over the past 252 trading days)
   - Volume ratio: `current_volume / average_1_month_volume`
   - OBV (On-Balance Volume, cumulative)
   - VWAP (Volume Weighted Average Price for the most recent trading day)
   - Pivot Point (classic): `(high + low + close) / 3` using prior day's values
   - Pivot Support 1: `(2 × pivot) − high`
   - Pivot Resistance 1: `(2 × pivot) − low`
7. WHEN a stock has fewer than 200 trading days of data in `ohlcv_daily`, THE Indicator_Engine SHALL compute only the indicators for which sufficient data exists and store `null` for indicators requiring more data than is available.
8. WHEN the Indicator Engine completes calculation for a stock, THE Indicator_Engine SHALL write all computed indicator values to the `stock_indicators` table, keyed by (`instrument_key`, `trade_date`).
9. WHEN an indicator value already exists in `stock_indicators` for a given (`instrument_key`, `trade_date`), THE Indicator_Engine SHALL overwrite it with the newly computed value (upsert behavior).

---

### Requirement 5: Indicator Storage Schema

**User Story:** As an algo trader, I want all computed indicator values stored in a queryable PostgreSQL table, so that I can run ad-hoc SQL queries and the React frontend can retrieve them efficiently.

#### Acceptance Criteria

1. THE `stock_indicators` Table SHALL store one row per (`instrument_key`, `trade_date`) combination containing all computed indicator values from Requirement 4.
2. THE `stock_indicators` Table SHALL enforce a unique constraint on (`instrument_key`, `trade_date`).
3. THE `stock_indicators` Table SHALL include a `computed_at` timestamp column recording when the row was last written.
4. THE Database Schema SHALL include an index on `instrument_key` in the `stock_indicators` table to support fast per-stock lookups.
5. THE Database Schema SHALL include an index on `trade_date` in the `stock_indicators` table to support fast date-range queries.

---

### Requirement 6: Scheduled Automation

**User Story:** As an algo trader, I want the daily sync and indicator recalculation to run automatically after market close, so that fresh indicator values are available each morning without manual steps.

#### Acceptance Criteria

1. THE Daily Sync Script (`sync_daily.py`) SHALL be executable as a standalone command with no required arguments: `python sync_daily.py`.
2. WHEN `sync_daily.py` runs, THE Sync_Orchestrator SHALL execute the following steps in order: (1) fetch latest OHLCV for all active stocks, (2) run the Indicator Engine for all active stocks, (3) log a completion summary.
3. THE Daily Sync Script SHALL be compatible with macOS `cron` scheduling (no GUI dependencies, no interactive prompts).
4. WHEN the Daily Sync Script encounters a fatal error (e.g., database unreachable, Upstox API authentication failure), THE Sync_Orchestrator SHALL log the error with a timestamp and exit with a non-zero exit code.

---

### Requirement 7: Manual Recalculation API Endpoint

**User Story:** As an algo trader, I want to trigger a full recalculation of indicators for all active stocks on demand via an API call, so that I can refresh data outside the scheduled window when needed.

#### Acceptance Criteria

1. THE BFF_API SHALL expose a `POST /screener/recalculate` endpoint that triggers the Indicator Engine to recompute indicators for all active stocks.
2. WHEN `POST /screener/recalculate` is called, THE BFF_API SHALL return an immediate `202 Accepted` response with a job identifier, and run the recalculation asynchronously in the background.
3. WHEN the recalculation job completes, THE BFF_API SHALL update the job status to `"completed"` with a count of stocks processed.
4. WHEN the recalculation job fails, THE BFF_API SHALL update the job status to `"failed"` with an error message.
5. THE BFF_API SHALL expose a `GET /screener/recalculate/{job_id}` endpoint that returns the current status and result of a recalculation job.

---

### Requirement 8: Stock Indicator Query API

**User Story:** As an algo trader, I want the BFF API to expose indicator data for a selected stock, so that my React frontend can display all technical signals for that stock in one request.

#### Acceptance Criteria

1. THE BFF_API SHALL expose a `GET /screener/indicators/{instrument_key}` endpoint that returns the most recent computed indicator values for the specified stock.
2. WHEN a valid `instrument_key` is provided, THE BFF_API SHALL return a JSON response containing all indicator values from the `stock_indicators` table for the most recent `trade_date` for that stock.
3. WHEN the `instrument_key` does not exist in the `stock_indicators` table, THE BFF_API SHALL return HTTP 404 with a descriptive error message.
4. THE BFF_API SHALL expose a `GET /screener/indicators/{instrument_key}/history` endpoint that accepts optional `from_date` and `to_date` query parameters and returns indicator values for the specified date range.
5. WHEN `from_date` or `to_date` are omitted from the history endpoint, THE BFF_API SHALL default to returning the most recent 30 trading days of data.
6. THE BFF_API SHALL return all monetary and price values as decimal numbers with up to 4 decimal places.
7. THE BFF_API SHALL include CORS headers to allow requests from the React frontend origin.

---

### Requirement 9: Active Stocks List API

**User Story:** As an algo trader, I want the BFF API to return the list of all active stocks with their latest indicator snapshot, so that my React frontend can display a summary screener table.

#### Acceptance Criteria

1. THE BFF_API SHALL expose a `GET /screener/stocks` endpoint that returns a list of all stocks where `is_screener_active = true`, along with their most recent indicator values.
2. WHEN `GET /screener/stocks` is called, THE BFF_API SHALL return a JSON array where each element contains at minimum: `instrument_key`, `trade_date`, `close`, `rsi_14`, `sma_50`, `sma_200`, `golden_cross_state`, `52w_high`, `52w_low`, `pct_from_52w_high`, `volume_ratio`.
3. THE BFF_API SHALL support optional query parameters `page` and `page_size` (default: `page=1`, `page_size=50`) for pagination of the stocks list.
4. WHEN `page_size` exceeds 500, THE BFF_API SHALL return HTTP 400 with an error message indicating the maximum allowed page size.

---

### Requirement 10: Upstox API Authentication

**User Story:** As an algo trader, I want the system to manage Upstox API authentication securely, so that API calls succeed without exposing credentials in source code.

#### Acceptance Criteria

1. THE Data_Ingestion_Module SHALL read the Upstox API access token from an environment variable (`UPSTOX_ACCESS_TOKEN`) or a `.env` file, and SHALL NOT hardcode credentials in source files.
2. WHEN the Upstox API returns HTTP 401 (Unauthorized), THE Data_Ingestion_Module SHALL log an authentication error and halt the current job with a non-zero exit code.
3. THE BFF_API SHALL read its database connection string from an environment variable or `.env` file, consistent with the existing `config.py` pattern in the project.

---

### Requirement 11: Logging and Observability

**User Story:** As a developer, I want all jobs and API errors to produce structured logs, so that I can diagnose failures and monitor system health.

#### Acceptance Criteria

1. THE Data_Ingestion_Module SHALL log the start time, end time, total stocks processed, total records inserted, and total errors for each job run.
2. THE Indicator_Engine SHALL log the start time, end time, total stocks processed, and total errors for each calculation run.
3. WHEN any module encounters an unhandled exception, THE module SHALL log the exception type, message, and stack trace before exiting.
4. THE BFF_API SHALL log each incoming HTTP request with method, path, response status code, and response time in milliseconds.
5. THE Logging_Module SHALL write logs to both stdout and a rotating log file stored in a configurable `LOG_DIR` directory (default: `./logs`).

