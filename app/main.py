"""
FastAPI BFF — stk_fund Screener API.

Run with:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8004
"""

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.logging_setup import setup_logging
from app.routers.screener import router as screener_router

logger = setup_logging("api")

app = FastAPI(
    title="stk_fund Screener API",
    description="""
## Stock Technical Screener API

Backend-for-Frontend (BFF) that serves pre-computed technical indicators
for NSE/BSE stocks stored in PostgreSQL.

---

### Typical Workflow

```
1. POST /screener/sync              → fetch OHLCV from Upstox + recompute all indicators
   — or separately —
   POST /screener/fetch-ohlcv       → pull candles only
   POST /screener/recalculate       → recompute indicators from stored candles

2. GET  /screener/stocks            → paginated list of all active stocks + key indicators
3. GET  /screener/indicators/{id}   → full indicator snapshot for one stock
4. GET  /screener/indicators/{id}/history → historical indicators with date range
5. POST /screener/indicators/query  → bulk lookup for a list of ticker_ids
6. GET  /screener/industry/{code}/indicators → all stocks in a basic industry
```

---

### Screener Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/screener/stocks` | Paginated active stocks with latest indicator snapshot |
| `GET` | `/screener/indicators/{ticker_id}` | Latest indicators for one stock |
| `GET` | `/screener/indicators/{ticker_id}/history` | Historical indicators with optional date range |
| `POST` | `/screener/indicators/query` | Bulk latest indicators for a list of `ticker_ids` |
| `GET` | `/screener/industry/{basic_ind_code}/indicators` | Latest indicators for all stocks in a basic industry |

### Data Pipeline Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/screener/sync` | Full pipeline: fetch OHLCV + recompute indicators |
| `POST` | `/screener/fetch-ohlcv` | Fetch OHLCV candles from Upstox only |
| `POST` | `/screener/recalculate` | Recompute indicators from stored OHLCV only |
| `GET` | `/screener/recalculate/{job_id}` | Poll status of any background job |

---

### Job Polling

`/screener/sync`, `/screener/fetch-ohlcv`, and `/screener/recalculate` all
return HTTP **202** immediately with a `job_id`. Poll for completion at:

```
GET /screener/recalculate/{job_id}
```

Status flow: `pending` → `running` → `completed` | `failed`

---

### Industry Lookup

Filter stocks by CMIE basic industry code:

```
GET /screener/industry/IN090103001/indicators?page=1&page_size=20
```

Returns each stock's name, `ticker_id`, and all ~35 indicators for the latest
trade date. Uses a single optimised CTE query — no N+1 lookups.

---

### Indicators Computed (~35 per stock)

| Category | Indicators |
|----------|-----------|
| Price levels | close, 52w high/low, YTD high/low, % from 52w high/low |
| Moving averages | SMA 20/50/100/200, EMA 9/21/50/200 |
| MACD | line, signal, histogram |
| Cross signals | golden cross / death cross event + state (above/below) |
| Trend | ADX 14 |
| Momentum | RSI 14, Stochastic %K/%D, CCI 20, Williams %R 14, ROC 10 |
| Volatility | Bollinger Bands, ATR 14, StdDev 20, Historical Volatility 20 |
| Volume | avg 1m/1y, volume ratio, OBV, VWAP |
| Pivot points | pivot, support 1, resistance 1 |

---

### Rate Limits (Upstox API)
- 50 requests/second, 2 000 requests/30 minutes
- Bulk fetches are automatically chunked into 30-minute windows and checkpointed
""",
    version="1.1.0",
    contact={"name": "stk_fund"},
    license_info={"name": "Private"},
)

# ---------------------------------------------------------------------------
# CORS — allow React dev server and common local origins
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s → %d (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(screener_router, prefix="/screener")


@app.get("/health")
def health():
    return {"status": "ok"}
