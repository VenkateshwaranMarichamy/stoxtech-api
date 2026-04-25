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

### Workflow

```
1. POST /screener/fetch-ohlcv   → pull candles from Upstox into ohlcv_daily
2. POST /screener/recalculate   → compute ~35 indicators from ohlcv_daily
3. GET  /screener/stocks        → browse active stocks with latest indicators
4. GET  /screener/indicators/{ticker_id} → full indicator snapshot for one stock
```

### Job Polling

`/screener/fetch-ohlcv`, `/screener/sync`, and `/screener/recalculate` all
return a `job_id` immediately (HTTP 202). Poll the job status at:

```
GET /screener/recalculate/{job_id}
```

Status values: `pending` → `running` → `completed` | `failed`

### Rate Limits (Upstox)
- 50 requests/second, 2 000 requests/30 minutes
- Bulk fetches are automatically chunked and paced
""",
    version="1.0.0",
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
