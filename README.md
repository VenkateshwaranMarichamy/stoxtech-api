# stk_fund Screener API

Stock Technical Screener — FastAPI backend that ingests daily OHLCV data from
Upstox, computes ~35 technical indicators per stock using `pandas-ta`, stores
results in PostgreSQL, and exposes them through a REST API.

---

## Project Structure

```
app/
├── main.py               # FastAPI app, CORS, middleware, router registration
├── config.py             # All settings (DB, Upstox, thresholds, pagination)
├── db.py                 # psycopg2 connection helper
├── logging_setup.py      # Rotating file + stdout logging
├── data_ingestion.py     # UpstoxClient, OHLCVWriter, ActiveStockUpdater
├── indicator_engine.py   # pandas-ta indicator computation + DB upsert
├── bootstrap_data.py     # One-time bulk import script
├── sync_daily.py         # Nightly cron script
├── schemas.py            # Pydantic response models
└── routers/
    └── screener.py       # All /screener/* endpoints
schema.sql                # PostgreSQL DDL (run once to set up tables + indexes)
requirements.txt          # Python dependencies
.env.example              # Environment variable template
```

---

## Setup

### 1. Clone and create virtual environment

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in DB credentials and Upstox access token
```

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=stk_fund
DB_USER=postgres
DB_PASSWORD=your_password

UPSTOX_ACCESS_TOKEN=your_upstox_token
```

### 3. Apply database schema

```bash
psql -U postgres -d stk_fund -f schema.sql
```

### 4. Bootstrap historical data (one-time)

Fetches 1 year of daily OHLCV for all NSE stocks, sets active flags, and
computes indicators. Handles Upstox's 2,000 req/30-min limit automatically
with checkpointing — safe to interrupt and resume.

```bash
# NSE only (recommended — skips BSE duplicates, ~50% fewer API calls)
python -m app.bootstrap_data --exchange NSE_EQ

# Resume an interrupted run
python -m app.bootstrap_data --exchange NSE_EQ --resume

# Dry run — estimate only, no API calls
python -m app.bootstrap_data --dry-run
```

### 5. Start the API server

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8004
```

Swagger UI: http://localhost:8004/docs  
ReDoc: http://localhost:8004/redoc

---

## Daily Automation

Run after market close (3:30 PM IST, weekdays):

```bash
python -m app.sync_daily
```

Cron entry:
```
30 15 * * 1-5  /path/to/.venv/bin/python -m app.sync_daily
```

Or trigger via API:
```bash
curl -X POST http://localhost:8004/screener/sync
```

---

## Makefile Commands

```bash
make dev        # start API with --reload
make run        # start API without reload (production-like)
make bootstrap  # run bootstrap_data for NSE_EQ
make sync       # run sync_daily
```

---

## API Reference

Base URL: `http://localhost:8004`  
Swagger: `/docs` · ReDoc: `/redoc` · Health: `/health`

### Screener

#### `GET /screener/stocks`
Paginated list of all active stocks with their latest indicator snapshot.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `page` | int | 1 | Page number (1-based) |
| `page_size` | int | 50 | Results per page (max 500) |

**Response fields per stock:** `ticker_id`, `trade_date`, `close`, `rsi_14`,
`sma_50`, `sma_200`, `golden_cross_state`, `high_52w`, `low_52w`,
`pct_from_52w_high`, `volume_ratio`

---

#### `GET /screener/indicators/{ticker_id}`
Full indicator snapshot for a single stock (latest trade date).

**404** if no indicator data exists for the given `ticker_id`.

---

#### `GET /screener/indicators/{ticker_id}/history`
Historical indicators for a stock within a date range.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `from_date` | date | today − 30d | Start date (YYYY-MM-DD, inclusive) |
| `to_date` | date | today | End date (YYYY-MM-DD, inclusive) |

Returns an empty list (not 404) if no data exists for the range.

---

#### `POST /screener/indicators/query`
Bulk latest indicators for a list of `ticker_ids` in one request.

```json
{ "ticker_ids": [64, 5319, 42] }
```

**Response:**
```json
{
  "requested": 3,
  "found": 3,
  "not_found": [],
  "results": [ { "ticker_id": 42, "trade_date": "...", ... } ]
}
```

Stocks with no data appear in `not_found` — no 404 raised.

---

#### `GET /screener/industry/{basic_ind_code}/indicators`
Latest indicators for all stocks in a CMIE basic industry, paginated.

```
GET /screener/industry/IN090103001/indicators?page=1&page_size=20
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `basic_ind_code` | string | — | CMIE basic industry code (path param) |
| `page` | int | 1 | Page number |
| `page_size` | int | 50 | Results per page (max 500) |

**Response fields per stock:** `ticker_id`, `name`, `trade_date`, and all ~35
indicator values.

**404** if no stocks with indicator data exist for the given code.

---

### Data Pipeline

All pipeline endpoints return **HTTP 202** immediately with a `job_id`.
Poll for completion at `GET /screener/recalculate/{job_id}`.

Job status values: `pending` → `running` → `completed` | `failed`

---

#### `POST /screener/sync`
Full pipeline: fetch OHLCV from Upstox + recompute all indicators.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 365 | Calendar days of history to fetch (max 365) |

Equivalent to running `python -m app.sync_daily`.

---

#### `POST /screener/fetch-ohlcv`
Fetch OHLCV candles from Upstox only (does not recompute indicators).

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `from_date` | date | yes | Start date (YYYY-MM-DD) |
| `to_date` | date | yes | End date (YYYY-MM-DD) |
| `ticker_id` | int | no | Single stock; omit for all active stocks |

---

#### `POST /screener/recalculate`
Recompute all indicators from OHLCV already in the database.
Does **not** fetch new data from Upstox.

---

#### `GET /screener/recalculate/{job_id}`
Poll the status of any background job.

```json
{
  "job_id": "uuid",
  "status": "completed",
  "stocks_processed": 2481,
  "error": null,
  "created_at": "2026-04-27T15:30:00",
  "completed_at": "2026-04-27T15:36:44"
}
```

---

## Indicators Reference

| Category | Indicators |
|----------|-----------|
| Price levels | `close`, `high_52w`, `low_52w`, `high_ytd`, `low_ytd`, `pct_from_52w_high`, `pct_from_52w_low` |
| Moving averages | `sma_20`, `sma_50`, `sma_100`, `sma_200`, `ema_9`, `ema_21`, `ema_50`, `ema_200` |
| MACD | `macd_line`, `macd_signal`, `macd_histogram` |
| Cross signals | `golden_cross_event`, `death_cross_event`, `golden_cross_state` (`above`/`below`) |
| Trend | `adx_14` |
| Momentum | `rsi_14`, `stoch_k`, `stoch_d`, `cci_20`, `williams_r_14`, `roc_10` |
| Volatility | `bb_upper`, `bb_middle`, `bb_lower`, `atr_14`, `stddev_20`, `hist_volatility_20` |
| Volume | `avg_volume_1m`, `avg_volume_1y`, `volume_ratio`, `obv`, `vwap` |
| Pivot points | `pivot_point`, `pivot_support_1`, `pivot_resistance_1` |

---

## Database Schema

| Schema | Table | Description |
|--------|-------|-------------|
| `classification` | `ticker_symbol` | Stock master — `id`, `name`, `instrument_key`, `is_screener_active` |
| `classification` | `company_classification` | Industry mapping — `company_id`, `basic_ind_code` |
| `technical` | `ohlcv_daily` | Daily OHLCV candles keyed by `(ticker_id, trade_date)` |
| `technical` | `stock_indicators` | Computed indicators keyed by `(ticker_id, trade_date)` |
| `technical` | `screener_jobs` | Background job state — persisted across server restarts |

Key indexes:
- `technical.stock_indicators (ticker_id)` — fast per-stock lookups
- `technical.stock_indicators (trade_date)` — fast date-range queries
- `classification.company_classification (basic_ind_code)` — fast industry filter
- `classification.company_classification (company_id)` — fast join to ticker_symbol

---

## Configuration Reference (`config.py`)

| Key | Default | Description |
|-----|---------|-------------|
| `MIN_AVG_DAILY_TRADED_VALUE` | `10_000_000` | ₹1 crore — minimum avg daily traded value for `is_screener_active` |
| `ACTIVE_STOCK_LOOKBACK_DAYS` | `30` | Days used to compute avg traded value |
| `UPSTOX_REQUEST_DELAY_SECONDS` | `0.5` | Delay between API calls (~2 req/s) |
| `UPSTOX_MAX_RETRIES` | `3` | Retries on 429 / 5xx |
| `UPSTOX_WINDOW_MAX_REQUESTS` | `1800` | Max requests per 30-min window (Upstox limit: 2000) |
| `API_DEFAULT_PAGE_SIZE` | `50` | Default pagination size |
| `API_MAX_PAGE_SIZE` | `500` | Maximum allowed page size |
| `API_DEFAULT_HISTORY_DAYS` | `30` | Default history window for `/history` endpoint |
| `LOG_DIR` | `./logs` | Log file directory |
