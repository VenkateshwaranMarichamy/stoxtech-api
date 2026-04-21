# Design Document: Stock Technical Screener

## Overview

The Stock Technical Screener extends the existing `stk_fund` PostgreSQL project with a full pipeline for daily technical analysis of NSE/BSE stocks. The system ingests 1-year OHLCV data from the Upstox API, computes ~35 technical indicators per stock using `pandas-ta`, stores results in PostgreSQL, and exposes them through a FastAPI BFF consumed by a React frontend.

The design follows the same conventions already established in the `moneycontrol_scraper` module: a central `config.py` for all settings, `psycopg2` for database access, and standalone Python scripts suitable for `cron` scheduling.

### Key Design Decisions

- **Batch-first, not streaming**: All indicator computation happens in a nightly batch after market close. Real-time streaming is out of scope for Phase 1.
- **Compute-on-write, not on-read**: Indicators are pre-computed and stored; the API reads pre-computed values rather than computing on demand. This keeps API latency low for the screener table.
- **Single `stock_indicators` table**: All indicator columns live in one wide table keyed by `(instrument_key, trade_date)`. This avoids joins at query time and simplifies the API layer.
- **Upstox v2 historical candle API**: The deprecated v2 endpoint (`/v2/historical-candle/{instrument_key}/day/{to_date}`) is used because it returns up to 1 year of daily data in a single call — ideal for the bootstrap job. The v3 API is noted for future migration.
- **Rate limiting**: Upstox enforces 50 requests/second and 2,000 requests/30 minutes for historical data. The ingestion module uses a token-bucket approach with a configurable inter-request delay (default 0.1 s, giving ~10 req/s — well within limits) and exponential backoff on HTTP 429.
- **Background jobs via `asyncio`**: The FastAPI recalculation endpoint runs the indicator engine in a `BackgroundTasks` worker. Job state is held in an in-memory dict (sufficient for a single-user local deployment).
- **`python-dotenv`** for credential management, consistent with the `.env` pattern common in Python projects.

---

## Architecture

```mermaid
graph TD
    subgraph Ingestion["Data Ingestion (Python scripts)"]
        BS[bootstrap_data.py<br/>one-time bulk fetch]
        SD[sync_daily.py<br/>nightly cron job]
        DIM[data_ingestion.py<br/>UpstoxClient + DB writer]
    end

    subgraph Compute["Indicator Engine (Python)"]
        IE[indicator_engine.py<br/>pandas-ta calculations]
    end

    subgraph Storage["PostgreSQL — stk_fund"]
        TS[(ticker_symbol)]
        OD[(ohlcv_daily)]
        SI[(stock_indicators)]
    end

    subgraph API["FastAPI BFF (api/main.py)"]
        R1[GET /screener/stocks]
        R2[GET /screener/indicators/{key}]
        R3[GET /screener/indicators/{key}/history]
        R4[POST /screener/recalculate]
        R5[GET /screener/recalculate/{job_id}]
    end

    subgraph Frontend["React Frontend"]
        UI[Screener Table + Stock Detail]
    end

    BS --> DIM
    SD --> DIM
    DIM --> TS
    DIM --> OD
    SD --> IE
    BS --> IE
    IE --> OD
    IE --> SI
    TS --> IE
    API --> SI
    API --> TS
    R4 --> IE
    Frontend --> API
```

### Module Layout

```
stk_fund/
├── config.py                  # Central config (DB, Upstox, thresholds, paths)
├── db.py                      # Connection pool + shared DB helpers
├── data_ingestion.py          # UpstoxClient, OHLCV fetch + upsert
├── indicator_engine.py        # pandas-ta indicator computation + upsert
├── bootstrap_data.py          # One-time bulk import script
├── sync_daily.py              # Nightly cron script
├── api/
│   ├── main.py                # FastAPI app, CORS, logging middleware
│   ├── routers/
│   │   └── screener.py        # All /screener/* endpoints
│   └── schemas.py             # Pydantic response models
├── logs/                      # Rotating log files (gitignored)
├── .env                       # Credentials (gitignored)
└── requirements.txt
```

---

## Components and Interfaces

### 1. `config.py` — Central Configuration

Extends the existing pattern from `moneycontrol_scraper/config.py`. All tuneable parameters live here.

```python
# DB connection (reads from .env via python-dotenv)
DB_CONFIG = { "host": ..., "port": 5432, "dbname": "stk_fund", ... }

# Upstox
UPSTOX_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_ACCESS_TOKEN_ENV = "UPSTOX_ACCESS_TOKEN"

# Screener thresholds
MIN_AVG_DAILY_TRADED_VALUE = 10_000_000   # ₹1 crore
ACTIVE_STOCK_LOOKBACK_DAYS = 30

# Rate limiting
UPSTOX_REQUEST_DELAY_SECONDS = 0.1        # ~10 req/s, well under 50/s limit
UPSTOX_RETRY_DELAY_SECONDS = 1.0
UPSTOX_MAX_RETRIES = 3

# Logging
LOG_DIR = "./logs"
LOG_MAX_BYTES = 10 * 1024 * 1024          # 10 MB
LOG_BACKUP_COUNT = 5

# API
API_DEFAULT_PAGE_SIZE = 50
API_MAX_PAGE_SIZE = 500
API_DEFAULT_HISTORY_DAYS = 30
```

### 2. `data_ingestion.py` — UpstoxClient + DB Writer

**`UpstoxClient`** wraps the Upstox v2 REST API:

```python
class UpstoxClient:
    def __init__(self, access_token: str): ...

    def get_historical_daily(
        self,
        instrument_key: str,
        to_date: str,           # "YYYY-MM-DD"
        from_date: str = None,  # None → API returns up to 1 year
    ) -> list[dict]:
        """
        Returns list of dicts:
          { trade_date, open, high, low, close, volume }
        Handles HTTP 429 with exponential backoff.
        Raises AuthError on HTTP 401.
        """
```

**`OHLCVWriter`** handles DB upserts:

```python
class OHLCVWriter:
    def upsert_batch(self, records: list[dict]) -> tuple[int, int]:
        """
        Upserts records into ohlcv_daily.
        Returns (inserted_count, skipped_count).
        Uses executemany with ON CONFLICT DO NOTHING.
        """
```

**`ActiveStockUpdater`** computes and sets `is_screener_active`:

```python
class ActiveStockUpdater:
    def update_active_flags(self, threshold: float) -> tuple[int, int]:
        """
        Runs SQL to compute avg(close * volume) over last 30 days
        per instrument_key, then bulk-updates is_screener_active.
        Returns (activated_count, deactivated_count).
        """
```

### 3. `indicator_engine.py` — pandas-ta Computation

```python
class IndicatorEngine:
    def compute_for_stock(
        self,
        instrument_key: str,
        df: pd.DataFrame,       # OHLCV DataFrame, sorted ascending by trade_date
    ) -> dict:
        """
        Computes all indicators for the most recent trading day.
        Returns a flat dict of indicator_name → value.
        Returns None values for indicators with insufficient data.
        """

    def run_all_active(self) -> tuple[int, int]:
        """
        Fetches OHLCV from DB for all active stocks,
        calls compute_for_stock, upserts to stock_indicators.
        Returns (processed_count, error_count).
        """
```

Indicator computation uses `pandas-ta` exclusively. Example pattern:

```python
import pandas_ta as ta

df.ta.sma(length=20, append=True)          # adds SMA_20 column
df.ta.ema(length=9, append=True)           # adds EMA_9 column
df.ta.macd(fast=12, slow=26, signal=9, append=True)
df.ta.rsi(length=14, append=True)
df.ta.bbands(length=20, std=2, append=True)
df.ta.atr(length=14, append=True)
df.ta.obv(append=True)
df.ta.adx(length=14, append=True)
```

The engine reads the last row of the resulting DataFrame to extract the most recent indicator values.

### 4. `bootstrap_data.py` — One-Time Bulk Import

```
Usage: python bootstrap_data.py [--dry-run]

Steps:
  1. Load all instrument_keys from ticker_symbol
  2. For each stock: fetch 1-year daily OHLCV via UpstoxClient
  3. Upsert into ohlcv_daily
  4. After all stocks: call ActiveStockUpdater.update_active_flags()
  5. Run IndicatorEngine.run_all_active()
  6. Log summary
```

Estimated runtime for 2,500 stocks at 10 req/s: ~4–5 minutes for ingestion, ~2–3 minutes for indicator computation.

### 5. `sync_daily.py` — Nightly Cron Script

```
Usage: python sync_daily.py
Cron:  30 15 * * 1-5  /path/to/venv/bin/python /path/to/sync_daily.py

Steps:
  1. Fetch today's OHLCV for all active stocks
  2. Upsert into ohlcv_daily
  3. Run IndicatorEngine.run_all_active()
  4. Log completion summary (stocks updated / skipped / failed)
  5. Exit 0 on success, non-zero on fatal error
```

### 6. `api/main.py` — FastAPI BFF

```python
app = FastAPI(title="stk_fund Screener API")

# CORS — allow React dev server
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", ...])

# Request logging middleware (method, path, status, response_time_ms)

app.include_router(screener_router, prefix="/screener")
```

### 7. `api/routers/screener.py` — Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/screener/stocks` | Paginated list of active stocks with latest indicator snapshot |
| `GET` | `/screener/indicators/{instrument_key}` | Latest indicators for one stock |
| `GET` | `/screener/indicators/{instrument_key}/history` | Historical indicators with date range |
| `POST` | `/screener/recalculate` | Trigger async recalculation, returns job_id |
| `GET` | `/screener/recalculate/{job_id}` | Poll job status |

**Job state** (in-memory, sufficient for single-user local deployment):

```python
jobs: dict[str, JobStatus] = {}

class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    stocks_processed: int | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
```

---

## Data Models

### PostgreSQL Schema

#### `ticker_symbol` (existing table — altered)

```sql
-- Existing table; add new column:
ALTER TABLE ticker_symbol
    ADD COLUMN IF NOT EXISTS is_screener_active BOOLEAN NOT NULL DEFAULT FALSE;

-- instrument_key column already exists per requirements
```

#### `ohlcv_daily` (new table)

```sql
CREATE TABLE ohlcv_daily (
    id              BIGSERIAL PRIMARY KEY,
    instrument_key  VARCHAR(50)    NOT NULL,
    trade_date      DATE           NOT NULL,
    open            NUMERIC(12, 4) NOT NULL,
    high            NUMERIC(12, 4) NOT NULL,
    low             NUMERIC(12, 4) NOT NULL,
    close           NUMERIC(12, 4) NOT NULL,
    volume          BIGINT         NOT NULL,
    created_at      TIMESTAMP      NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_ohlcv_daily UNIQUE (instrument_key, trade_date)
);

CREATE INDEX idx_ohlcv_instrument_key ON ohlcv_daily (instrument_key);
CREATE INDEX idx_ohlcv_trade_date     ON ohlcv_daily (trade_date);
```

#### `stock_indicators` (new table)

```sql
CREATE TABLE stock_indicators (
    id                      BIGSERIAL PRIMARY KEY,
    instrument_key          VARCHAR(50)    NOT NULL,
    trade_date              DATE           NOT NULL,
    computed_at             TIMESTAMP      NOT NULL DEFAULT NOW(),

    -- Price levels
    close                   NUMERIC(12, 4),
    high_52w                NUMERIC(12, 4),
    low_52w                 NUMERIC(12, 4),
    high_ytd                NUMERIC(12, 4),
    low_ytd                 NUMERIC(12, 4),
    pct_from_52w_high       NUMERIC(8, 4),
    pct_from_52w_low        NUMERIC(8, 4),

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
    golden_cross_state      VARCHAR(5),    -- 'above' | 'below'

    -- Trend
    adx_14                  NUMERIC(8, 4),

    -- Momentum / oscillators
    rsi_14                  NUMERIC(8, 4),
    stoch_k                 NUMERIC(8, 4),
    stoch_d                 NUMERIC(8, 4),
    cci_20                  NUMERIC(10, 4),
    williams_r_14           NUMERIC(8, 4),
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

    -- Pivot points
    pivot_point             NUMERIC(12, 4),
    pivot_support_1         NUMERIC(12, 4),
    pivot_resistance_1      NUMERIC(12, 4),

    CONSTRAINT uq_stock_indicators UNIQUE (instrument_key, trade_date)
);

CREATE INDEX idx_si_instrument_key ON stock_indicators (instrument_key);
CREATE INDEX idx_si_trade_date     ON stock_indicators (trade_date);
```

### Pydantic API Schemas

```python
class IndicatorSnapshot(BaseModel):
    instrument_key: str
    trade_date: date
    computed_at: datetime
    close: Decimal | None
    high_52w: Decimal | None
    low_52w: Decimal | None
    pct_from_52w_high: Decimal | None
    rsi_14: Decimal | None
    sma_50: Decimal | None
    sma_200: Decimal | None
    golden_cross_state: str | None
    volume_ratio: Decimal | None
    # ... all other indicator fields

class StockListItem(BaseModel):
    """Subset returned by GET /screener/stocks"""
    instrument_key: str
    trade_date: date
    close: Decimal | None
    rsi_14: Decimal | None
    sma_50: Decimal | None
    sma_200: Decimal | None
    golden_cross_state: str | None
    high_52w: Decimal | None
    low_52w: Decimal | None
    pct_from_52w_high: Decimal | None
    volume_ratio: Decimal | None

class StocksListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    stocks: list[StockListItem]

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    stocks_processed: int | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
```

### Upstox API Response Mapping

The Upstox v2 daily candle response returns arrays in this order:

```
[timestamp, open, high, low, close, volume, open_interest]
```

Mapping to `ohlcv_daily`:

```python
{
    "instrument_key": instrument_key,
    "trade_date":     candle[0][:10],   # "2024-01-15T00:00:00+05:30" → "2024-01-15"
    "open":           candle[1],
    "high":           candle[2],
    "low":            candle[3],
    "close":          candle[4],
    "volume":         candle[5],
}
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Active stock flag consistency

*For any* set of stocks with associated average daily traded values and any configured threshold, after `update_active_flags(threshold)` runs, a stock's `is_screener_active` flag is `true` if and only if its average traded value is greater than or equal to the threshold — no stock above the threshold is inactive, and no stock below the threshold is active.

**Validates: Requirements 1.2, 1.3**

---

### Property 2: OHLCV upsert idempotence

*For any* set of OHLCV records, inserting the same records into `ohlcv_daily` a second time produces the same database state as inserting them once — the row count does not increase and no errors are raised.

**Validates: Requirements 2.6, 2.7, 3.2**

---

### Property 3: Indicator upsert idempotence

*For any* stock and trade date, running the Indicator Engine twice on the same OHLCV data produces exactly one row in `stock_indicators` — the second run overwrites the first with identical values and does not create a duplicate.

**Validates: Requirements 4.9, 5.1, 5.2**

---

### Property 4: Indicator null safety for insufficient data

*For any* OHLCV DataFrame with between 1 and 199 rows, the Indicator Engine completes without raising an exception, stores `null` for every indicator whose minimum data requirement exceeds the available row count, and stores a valid numeric value for every indicator whose requirement is satisfied.

**Validates: Requirements 4.7**

---

### Property 5: Price level formula correctness

*For any* OHLCV series with at least 252 trading days, `high_52w` equals `max(close[-252:])`, `low_52w` equals `min(close[-252:])`, `pct_from_52w_high` equals `((close - high_52w) / high_52w) × 100`, and `pct_from_52w_low` equals `((close - low_52w) / low_52w) × 100`.

**Validates: Requirements 4.2**

---

### Property 6: Bollinger Band ordering invariant

*For any* OHLCV DataFrame with at least 20 rows, the computed Bollinger Bands satisfy `bb_lower <= bb_middle <= bb_upper`.

**Validates: Requirements 4.5**

---

### Property 7: RSI bounds invariant

*For any* OHLCV DataFrame with at least 14 rows, the computed `rsi_14` value is in the range `[0, 100]`.

**Validates: Requirements 4.4**

---

### Property 8: Golden cross state consistency

*For any* OHLCV DataFrame where both `sma_50` and `sma_200` are non-null, `golden_cross_state` is `"above"` if and only if `sma_50 > sma_200`, and `"below"` if and only if `sma_50 < sma_200`.

**Validates: Requirements 4.3**

---

### Property 9: Pagination bounds

*For any* call to `GET /screener/stocks` with a valid `page` and `page_size` (1–500), the number of items in the response is at most `page_size`, and `response.total` equals the actual count of active stocks in the database.

**Validates: Requirements 9.3**

---

### Property 10: History endpoint date range filtering

*For any* call to `GET /screener/indicators/{instrument_key}/history` with explicit `from_date` and `to_date`, every row in the response satisfies `from_date <= trade_date <= to_date` — no out-of-range rows are returned.

**Validates: Requirements 8.4**

---

### Property 11: Stocks list response field completeness

*For any* active stock with a computed indicator row, the corresponding item in `GET /screener/stocks` contains all required fields: `instrument_key`, `trade_date`, `close`, `rsi_14`, `sma_50`, `sma_200`, `golden_cross_state`, `high_52w`, `low_52w`, `pct_from_52w_high`, and `volume_ratio`.

**Validates: Requirements 9.2**

---

## Error Handling

### Upstox API Errors

| HTTP Status | Handling |
|-------------|----------|
| 200 | Parse and process normally |
| 429 Too Many Requests | Exponential backoff: wait `retry_delay × 2^attempt` seconds, up to `MAX_RETRIES` attempts |
| 401 Unauthorized | Log auth error, halt job with exit code 1 |
| 404 Not Found | Log warning with `instrument_key`, skip stock, continue |
| 5xx Server Error | Retry up to `MAX_RETRIES` times with delay, then log and skip |
| Network timeout | Retry up to `MAX_RETRIES` times, then log and skip |

### Database Errors

- Connection failure at startup: log error, exit with code 1 (fatal — no point continuing)
- Upsert failure for a single record: log with `instrument_key` + `trade_date`, continue with remaining records
- Schema mismatch (column not found): log error, exit with code 1

### Indicator Computation Errors

- `pandas-ta` raises an exception for a stock: log with `instrument_key` and stack trace, skip that stock, continue
- All-NaN result from insufficient data: store `null` per Requirement 4.7 — not an error condition

### FastAPI Error Responses

```python
# 404 — stock not found
{"detail": "No indicator data found for instrument_key: NSE_EQ|INE002A01018"}

# 400 — page_size too large
{"detail": "page_size cannot exceed 500"}

# 422 — invalid date format (FastAPI/Pydantic automatic)
{"detail": [{"loc": ["query", "from_date"], "msg": "invalid date format"}]}

# 500 — unexpected server error (logged with stack trace)
{"detail": "Internal server error"}
```

### Logging Strategy

All modules use Python's standard `logging` with a shared setup function:

```python
def setup_logging(name: str) -> logging.Logger:
    """
    Returns a logger writing to:
      - stdout (StreamHandler)
      - logs/{name}.log (RotatingFileHandler, 10 MB, 5 backups)
    Format: %(asctime)s | %(levelname)-8s | %(name)s | %(message)s
    """
```

Structured log fields for job runs:

```
2024-01-15 15:32:01 | INFO     | sync_daily | Job started
2024-01-15 15:32:01 | INFO     | data_ingestion | Fetching OHLCV for 2487 active stocks
2024-01-15 15:36:22 | INFO     | data_ingestion | Done: 2481 updated, 6 failed, 0 skipped
2024-01-15 15:36:22 | INFO     | indicator_engine | Computing indicators for 2481 stocks
2024-01-15 15:38:45 | INFO     | indicator_engine | Done: 2479 processed, 2 errors
2024-01-15 15:38:45 | INFO     | sync_daily | Job completed in 6m44s
```

---

## Testing Strategy

### Unit Tests (`pytest`)

Focus on pure logic that can be tested without a live database or Upstox API:

- **`test_indicator_engine.py`**: Feed known OHLCV DataFrames and assert indicator values match hand-calculated expectations. Cover the null-safety path (< 200 rows of data).
- **`test_data_ingestion.py`**: Mock `requests.get` to return fixture JSON; assert correct parsing of Upstox candle arrays into `ohlcv_daily` dicts. Test retry logic by simulating HTTP 429 responses.
- **`test_api_schemas.py`**: Validate Pydantic schema serialization, decimal precision, and field presence.
- **`test_config.py`**: Assert that all required config keys are present and have sensible defaults.

### Property-Based Tests (`pytest` + `hypothesis`)

Property-based testing is appropriate here because the indicator engine and API filtering logic are pure functions over structured data with large input spaces. Each property test runs a minimum of 100 iterations.

```python
# Tag format: Feature: stock-technical-screener, Property N: <property_text>
```

- **Property 1 — Active stock flag consistency**: Generate random `(instrument_key, avg_traded_value)` pairs and a random threshold, run `update_active_flags`, assert every stock above threshold is active and every stock below is inactive.
- **Property 2 — OHLCV upsert idempotence**: Generate random lists of OHLCV records (varying instrument_keys, dates, prices), insert twice into a test DB, assert row count equals first-insert count.
- **Property 3 — Indicator upsert idempotence**: Generate random OHLCV DataFrames (≥ 200 rows), run engine twice, assert `stock_indicators` row is identical.
- **Property 4 — Null safety**: Generate DataFrames with 1–199 rows; assert no exception is raised and all indicators requiring > available rows are `null`.
- **Property 5 — Price level formula correctness**: Generate random close price series (≥ 252 values), assert `high_52w == max(close[-252:])`, `low_52w == min(close[-252:])`, and both pct distance formulas hold.
- **Property 6 — Bollinger Band ordering**: Generate random OHLCV DataFrames (≥ 20 rows), assert `bb_lower <= bb_middle <= bb_upper`.
- **Property 7 — RSI bounds**: Generate random OHLCV DataFrames (≥ 14 rows), assert `0 <= rsi_14 <= 100`.
- **Property 8 — Golden cross state consistency**: Generate OHLCV data with known SMA50/SMA200 relationship, assert `golden_cross_state` matches.
- **Property 9 — Pagination bounds**: Generate random `(total_stocks, page, page_size)` combinations (page_size 1–500), assert `len(response.stocks) <= page_size` and `response.total == total_stocks`.
- **Property 10 — Date range filtering**: Generate random date ranges and indicator history data, assert all returned rows fall within the requested range.
- **Property 11 — Stocks list field completeness**: Generate random active stocks with indicator data, assert each response item contains all 11 required fields.

### Integration Tests

Run against a local test PostgreSQL database (`stk_fund_test`):

- Bootstrap job end-to-end with a small fixture set (10 stocks, 30 days of OHLCV)
- Daily sync job with mocked Upstox responses
- FastAPI endpoints via `httpx.AsyncClient` + `TestClient`

### Test Configuration

```
# pytest.ini
[pytest]
testpaths = tests
addopts = --tb=short -q

# Hypothesis settings
from hypothesis import settings
settings.register_profile("ci", max_examples=100)
settings.load_profile("ci")
```

### What Is Not Property-Tested

- **Upstox API integration**: External service behavior — use integration tests with mocked HTTP responses (1–3 examples per error scenario).
- **Database schema creation**: One-time DDL — use a smoke test that runs migrations and checks table existence.
- **FastAPI startup and CORS configuration**: Use a single smoke test with `TestClient`.
- **Cron scheduling**: Not testable programmatically — document the cron expression and verify manually.
