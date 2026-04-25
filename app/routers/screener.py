"""
Screener router — all /screener/* endpoints.
"""

import os
import uuid
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg2.extras
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

import app.config as config
from app.db import get_connection
from app.indicator_engine import IndicatorEngine
from app.data_ingestion import UpstoxClient, OHLCVWriter, fetch_and_store
from app.schemas import (
    IndicatorSnapshot,
    IndicatorQueryRequest,
    IndicatorQueryResponse,
    IndicatorQueryItem,
    JobStatusResponse,
    StockListItem,
    StocksListResponse,
)

router = APIRouter()
log = logging.getLogger("screener")


# ---------------------------------------------------------------------------
# Job persistence helpers  (technical.screener_jobs)
# ---------------------------------------------------------------------------

def _create_job(job_id: str) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO technical.screener_jobs (job_id, status, created_at) "
                    "VALUES (%s, 'pending', NOW());",
                    (job_id,),
                )
    finally:
        conn.close()


def _update_job(
    job_id: str,
    status: str,
    stocks_processed: int | None = None,
    error: str | None = None,
) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE technical.screener_jobs
                    SET status           = %s,
                        stocks_processed = %s,
                        error            = %s,
                        completed_at     = CASE WHEN %s IN ('completed', 'failed')
                                                THEN NOW() ELSE completed_at END
                    WHERE job_id = %s;
                    """,
                    (status, stocks_processed, error, status, job_id),
                )
    finally:
        conn.close()


def _get_job(job_id: str) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT job_id, status, stocks_processed, error, created_at, completed_at "
                "FROM technical.screener_jobs WHERE job_id = %s;",
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /screener/stocks
# ---------------------------------------------------------------------------

@router.get(
    "/stocks",
    response_model=StocksListResponse,
    summary="List active stocks with latest indicators",
    tags=["Screener"],
)
def list_stocks(
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(
        default=config.API_DEFAULT_PAGE_SIZE, ge=1,
        description=f"Results per page (max {config.API_MAX_PAGE_SIZE})",
    ),
):
    """
    Return a paginated list of all stocks where `is_screener_active = true`,
    each with their most recent indicator snapshot.

    **Fields returned per stock:**
    `ticker_id`, `trade_date`, `close`, `rsi_14`, `sma_50`, `sma_200`,
    `golden_cross_state`, `high_52w`, `low_52w`, `pct_from_52w_high`, `volume_ratio`

    **Pagination:** use `page` and `page_size`. Response includes `total` count.

    **Error:** HTTP 400 if `page_size` exceeds the maximum allowed value.
    """
    if page_size > config.API_MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"page_size cannot exceed {config.API_MAX_PAGE_SIZE}",
        )

    offset = (page - 1) * page_size

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM classification.ticker_symbol ts
                JOIN technical.stock_indicators si ON ts.id = si.ticker_id
                WHERE ts.is_screener_active = TRUE
                  AND si.trade_date = (
                      SELECT MAX(trade_date) FROM technical.stock_indicators si2
                      WHERE si2.ticker_id = ts.id
                  );
                """
            )
            total = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT
                    si.ticker_id,
                    si.trade_date,
                    si.close,
                    si.rsi_14,
                    si.sma_50,
                    si.sma_200,
                    si.golden_cross_state,
                    si.high_52w,
                    si.low_52w,
                    si.pct_from_52w_high,
                    si.volume_ratio
                FROM classification.ticker_symbol ts
                JOIN technical.stock_indicators si ON ts.id = si.ticker_id
                WHERE ts.is_screener_active = TRUE
                  AND si.trade_date = (
                      SELECT MAX(trade_date) FROM technical.stock_indicators si2
                      WHERE si2.ticker_id = ts.id
                  )
                ORDER BY si.ticker_id
                LIMIT %s OFFSET %s;
                """,
                (page_size, offset),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    stocks = [StockListItem(**dict(row)) for row in rows]
    return StocksListResponse(page=page, page_size=page_size, total=total, stocks=stocks)


# ---------------------------------------------------------------------------
# GET /screener/indicators/{ticker_id}
# ---------------------------------------------------------------------------

@router.get(
    "/indicators/{ticker_id}",
    response_model=IndicatorSnapshot,
    summary="Latest indicator snapshot for a stock",
    tags=["Indicators"],
)
def get_latest_indicators(ticker_id: int):
    """
    Return the most recently computed indicator snapshot for a single stock.

    **Path parameter:**
    - `ticker_id` — integer PK from `classification.ticker_symbol.id`

    **Returns:** all ~35 computed indicators for the latest `trade_date`.

    **Error:** HTTP 404 if no indicator data exists for the given `ticker_id`.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT ticker_id, trade_date,
                       computed_at::date AS computed_date,
                       last_close, ohlcv_start_date, ohlcv_end_date,
                       close, high_52w, low_52w, high_ytd, low_ytd,
                       pct_from_52w_high, pct_from_52w_low,
                       sma_20, sma_50, sma_100, sma_200,
                       ema_9, ema_21, ema_50, ema_200,
                       macd_line, macd_signal, macd_histogram,
                       golden_cross_event, death_cross_event, golden_cross_state,
                       adx_14, rsi_14, stoch_k, stoch_d, cci_20, williams_r_14, roc_10,
                       bb_upper, bb_middle, bb_lower, atr_14, stddev_20, hist_volatility_20,
                       avg_volume_1m, avg_volume_1y, volume_ratio, obv, vwap,
                       pivot_point, pivot_support_1, pivot_resistance_1
                FROM technical.stock_indicators
                WHERE ticker_id = %s
                ORDER BY trade_date DESC
                LIMIT 1;
                """,
                (ticker_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No indicator data found for ticker_id: {ticker_id}",
        )
    return IndicatorSnapshot(**dict(row))


# ---------------------------------------------------------------------------
# GET /screener/indicators/{ticker_id}/history
# ---------------------------------------------------------------------------

@router.get(
    "/indicators/{ticker_id}/history",
    response_model=list[IndicatorSnapshot],
    summary="Historical indicators for a stock",
    tags=["Indicators"],
)
def get_indicator_history(
    ticker_id: int,
    from_date: Optional[date] = Query(
        default=None,
        description=f"Start date (YYYY-MM-DD). Defaults to {config.API_DEFAULT_HISTORY_DAYS} days before to_date.",
    ),
    to_date: Optional[date] = Query(
        default=None,
        description="End date (YYYY-MM-DD). Defaults to today.",
    ),
):
    """
    Return indicator rows for a stock within a date range, ordered newest first.

    **Path parameter:**
    - `ticker_id` — integer PK from `classification.ticker_symbol.id`

    **Query parameters:**
    - `from_date` — start of range (inclusive). Defaults to 30 days before `to_date`.
    - `to_date` — end of range (inclusive). Defaults to today.

    **Returns:** list of indicator snapshots, one per trading day in the range.
    Returns an empty list if no data exists for the range (no 404).
    """
    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=config.API_DEFAULT_HISTORY_DAYS)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT ticker_id, trade_date,
                       computed_at::date AS computed_date,
                       last_close, ohlcv_start_date, ohlcv_end_date,
                       close, high_52w, low_52w, high_ytd, low_ytd,
                       pct_from_52w_high, pct_from_52w_low,
                       sma_20, sma_50, sma_100, sma_200,
                       ema_9, ema_21, ema_50, ema_200,
                       macd_line, macd_signal, macd_histogram,
                       golden_cross_event, death_cross_event, golden_cross_state,
                       adx_14, rsi_14, stoch_k, stoch_d, cci_20, williams_r_14, roc_10,
                       bb_upper, bb_middle, bb_lower, atr_14, stddev_20, hist_volatility_20,
                       avg_volume_1m, avg_volume_1y, volume_ratio, obv, vwap,
                       pivot_point, pivot_support_1, pivot_resistance_1
                FROM technical.stock_indicators
                WHERE ticker_id = %s
                  AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date DESC;
                """,
                (ticker_id, from_date, to_date),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [IndicatorSnapshot(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /screener/indicators/query
# ---------------------------------------------------------------------------

@router.post(
    "/indicators/query",
    response_model=IndicatorQueryResponse,
    summary="Bulk indicator lookup for one or more stocks",
    tags=["Indicators"],
)
def query_indicators(body: IndicatorQueryRequest):
    """
    Return the latest indicator snapshot for one or more stocks in a single request.

    **Request body:**
    ```json
    { "ticker_ids": [64, 5319, 42] }
    ```
    Pass a single-element list for one stock: `{ "ticker_ids": [5319] }`

    **Response includes:**
    - `requested` — number of ticker_ids sent
    - `found` — number that had indicator data
    - `not_found` — list of ticker_ids with no data
    - `results` — full indicator snapshot per stock, including:
      - `ticker_id`, `trade_date`, `computed_at`
      - All ~35 indicator values

    **Notes:**
    - Returns the most recent `trade_date` row per stock.
    - Stocks with no data appear in `not_found` — no HTTP 404 is raised.
    - Results are ordered by `ticker_id` ascending.
    - Duplicate `ticker_ids` in the request are deduplicated automatically.
    """
    if not body.ticker_ids:
        raise HTTPException(status_code=400, detail="ticker_ids list cannot be empty.")

    unique_ids = sorted(set(body.ticker_ids))

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch the latest indicator row per ticker_id in one query
            cur.execute(
                """
                SELECT DISTINCT ON (ticker_id)
                    ticker_id, trade_date,
                    computed_at::date AS computed_date,
                    last_close, ohlcv_start_date, ohlcv_end_date,
                    close, high_52w, low_52w, high_ytd, low_ytd,
                    pct_from_52w_high, pct_from_52w_low,
                    sma_20, sma_50, sma_100, sma_200,
                    ema_9, ema_21, ema_50, ema_200,
                    macd_line, macd_signal, macd_histogram,
                    golden_cross_event, death_cross_event, golden_cross_state,
                    adx_14,
                    rsi_14, stoch_k, stoch_d, cci_20, williams_r_14, roc_10,
                    bb_upper, bb_middle, bb_lower, atr_14, stddev_20, hist_volatility_20,
                    avg_volume_1m, avg_volume_1y, volume_ratio, obv, vwap,
                    pivot_point, pivot_support_1, pivot_resistance_1
                FROM technical.stock_indicators
                WHERE ticker_id = ANY(%s)
                ORDER BY ticker_id ASC, trade_date DESC;
                """,
                (unique_ids,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    found_ids = {row["ticker_id"] for row in rows}
    not_found = [tid for tid in unique_ids if tid not in found_ids]
    results   = [IndicatorQueryItem(**dict(row)) for row in rows]

    return IndicatorQueryResponse(
        requested=len(unique_ids),
        found=len(results),
        not_found=not_found,
        results=results,
    )


# ---------------------------------------------------------------------------
# POST /screener/fetch-ohlcv
# ---------------------------------------------------------------------------

@router.post(
    "/fetch-ohlcv",
    status_code=202,
    summary="Fetch OHLCV candles from Upstox",
    tags=["Data Pipeline"],
)
def fetch_ohlcv(
    background_tasks: BackgroundTasks,
    from_date: date = Query(description="Start date (YYYY-MM-DD)"),
    to_date: date   = Query(description="End date (YYYY-MM-DD). Use the same date as from_date to fetch a single day."),
    ticker_id: Optional[int] = Query(
        default=None,
        description="Fetch a single stock by ticker_id. Omit to fetch all active stocks.",
    ),
):
    """
    Pull daily OHLCV candles from the Upstox v2 API and store them in
    `technical.ohlcv_daily`.

    **Upstox URL called:**
    ```
    GET https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}
    ```

    **Query parameters:**
    - `from_date` — start of the date range to fetch (inclusive).
    - `to_date` — end of the date range to fetch (inclusive).
      Pass the same value as `from_date` to fetch a single trading day.
    - `ticker_id` *(optional)* — if provided, fetches only that stock.
      If omitted, fetches all stocks where `is_screener_active = true`, ordered by `id`.

    **Behaviour:**
    - Runs asynchronously in the background. Returns HTTP 202 immediately.
    - Existing rows for the same `(ticker_id, trade_date)` are skipped (`ON CONFLICT DO NOTHING`).
    - Poll the returned `job_id` at `GET /screener/recalculate/{job_id}` for status.

    **Errors:**
    - HTTP 400 if `from_date` is after `to_date`.
    - HTTP 404 if the provided `ticker_id` does not exist.
    - HTTP 500 if the Upstox token is not configured.

    **Note:** this endpoint only fetches and stores raw OHLCV data.
    To recompute indicators after fetching, call `POST /screener/recalculate`.
    """
    if from_date > to_date:
        raise HTTPException(
            status_code=400,
            detail="from_date cannot be after to_date.",
        )

    token = os.getenv(config.UPSTOX_ACCESS_TOKEN_ENV)
    if not token:
        raise HTTPException(
            status_code=500,
            detail=f"Upstox token not configured ({config.UPSTOX_ACCESS_TOKEN_ENV} missing)",
        )

    if ticker_id is not None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT instrument_key FROM classification.ticker_symbol WHERE id = %s;",
                    (ticker_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail=f"ticker_id {ticker_id} not found.")

    job_id = str(uuid.uuid4())
    _create_job(job_id)
    background_tasks.add_task(
        _run_fetch_ohlcv, job_id, token,
        from_date.isoformat(), to_date.isoformat(),
        ticker_id,
    )
    scope = f"ticker_id={ticker_id}" if ticker_id else "all active stocks"
    return {
        "job_id":    job_id,
        "status":    "pending",
        "from_date": from_date.isoformat(),
        "to_date":   to_date.isoformat(),
        "scope":     scope,
    }


def _run_fetch_ohlcv(
    job_id: str,
    token: str,
    from_date: str,
    to_date: str,
    ticker_id: int | None,
) -> None:
    """Background task: fetch OHLCV from Upstox and store in ohlcv_daily."""
    _update_job(job_id, status="running")

    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                if ticker_id is not None:
                    cur.execute(
                        "SELECT id, instrument_key FROM classification.ticker_symbol "
                        "WHERE id = %s;",
                        (ticker_id,),
                    )
                else:
                    cur.execute(
                        "SELECT id, instrument_key FROM classification.ticker_symbol "
                        "WHERE is_screener_active = TRUE ORDER BY id ASC;"
                    )
                stocks = [{"id": r["id"], "instrument_key": r["instrument_key"]}
                          for r in cur.fetchall()]
        finally:
            conn.close()

        log.info(
            "fetch-ohlcv job %s: %d stock(s) | %s → %s | "
            "GET %s/historical-candle/<key>/day/%s/%s",
            job_id, len(stocks), from_date, to_date,
            config.UPSTOX_BASE_URL, to_date, from_date,
        )

        client = UpstoxClient(token)
        writer = OHLCVWriter()
        updated = failed = 0

        for stock in stocks:
            try:
                records = client.get_historical_daily(
                    stock["instrument_key"], to_date, from_date
                )
                if records:
                    writer.upsert_batch(stock["id"], records)
                    updated += 1
                    log.debug(
                        "Stored %d records for %s (id=%d)",
                        len(records), stock["instrument_key"], stock["id"],
                    )
                else:
                    log.debug("No records for %s", stock["instrument_key"])
            except Exception as exc:
                log.error("Failed %s: %s", stock["instrument_key"], exc, exc_info=True)
                failed += 1

        log.info("fetch-ohlcv job %s done — updated=%d failed=%d", job_id, updated, failed)
        _update_job(job_id, status="completed", stocks_processed=updated)

    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# POST /screener/sync
# ---------------------------------------------------------------------------

@router.post(
    "/sync",
    status_code=202,
    summary="Fetch OHLCV then recompute indicators (full pipeline)",
    tags=["Data Pipeline"],
)
def trigger_sync(
    background_tasks: BackgroundTasks,
    days: int = Query(
        default=365, ge=1, le=365,
        description="Number of calendar days of history to fetch from Upstox (max 365 ≈ 252 trading days).",
    ),
):
    """
    Run the full daily pipeline in one call:

    1. Fetch OHLCV candles from Upstox for all `is_screener_active = true` stocks.
    2. Upsert candles into `technical.ohlcv_daily`.
    3. Recompute all ~35 indicators and upsert into `technical.stock_indicators`.

    **Query parameters:**
    - `days` — how many calendar days back to fetch (default 365, max 365).
      365 calendar days ≈ 252 trading days (1 year of market data).

    **Upstox URL called per stock:**
    ```
    GET https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{today}/{today-days}
    ```

    **Behaviour:**
    - Runs asynchronously. Returns HTTP 202 immediately with a `job_id`.
    - Poll status at `GET /screener/recalculate/{job_id}`.
    - Equivalent to running `python sync_daily.py` from the command line.

    **Error:** HTTP 500 if the Upstox token is not configured.
    """
    token = os.getenv(config.UPSTOX_ACCESS_TOKEN_ENV)
    if not token:
        raise HTTPException(
            status_code=500,
            detail=f"Upstox token not configured ({config.UPSTOX_ACCESS_TOKEN_ENV} missing)",
        )

    job_id = str(uuid.uuid4())
    _create_job(job_id)
    background_tasks.add_task(_run_sync, job_id, token, days)
    return {
        "job_id":  job_id,
        "status":  "pending",
        "message": f"Fetching last {days} calendar days of OHLCV then recomputing indicators",
    }


def _run_sync(job_id: str, token: str, days: int) -> None:
    """Background task: fetch OHLCV from Upstox then run indicator engine."""
    _update_job(job_id, status="running")
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT instrument_key FROM classification.ticker_symbol "
                    "WHERE is_screener_active = TRUE ORDER BY id ASC;"
                )
                keys = [row["instrument_key"] for row in cur.fetchall()]
        finally:
            conn.close()

        to_date   = date.today().isoformat()
        from_date = (date.today() - timedelta(days=days)).isoformat()

        log.info(
            "Sync job %s: %d active stocks | "
            "GET %s/historical-candle/<key>/day/%s/%s",
            job_id, len(keys), config.UPSTOX_BASE_URL, to_date, from_date,
        )

        client = UpstoxClient(token)
        writer = OHLCVWriter()
        updated, failed = fetch_and_store(client, writer, keys, to_date, from_date)
        log.info("Sync job %s: OHLCV done — updated=%d failed=%d", job_id, updated, failed)

        engine = IndicatorEngine()
        processed, errors = engine.run_all_active()

        # Refresh stock classifications via stored procedure
        try:
            log.info("Job %s: calling analytics.sp_refresh_classifications()...", job_id)
            conn = get_connection()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CALL analytics.sp_refresh_classifications()")
            conn.close()
            log.info("Job %s: stock classifications refreshed.", job_id)
        except Exception as exc:
            log.error("Job %s: failed to refresh classifications: %s", job_id, exc, exc_info=True)
            # Non-fatal — indicators were still computed successfully

        _update_job(job_id, status="completed", stocks_processed=processed)
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# POST /screener/recalculate
# ---------------------------------------------------------------------------

@router.post(
    "/recalculate",
    status_code=202,
    summary="Recompute indicators from existing OHLCV data",
    tags=["Data Pipeline"],
)
def trigger_recalculate(background_tasks: BackgroundTasks):
    """
    Recompute all ~35 technical indicators for every `is_screener_active = true`
    stock using OHLCV data already stored in `technical.ohlcv_daily`.

    **Does NOT fetch new data from Upstox.** To fetch fresh candles first,
    use `POST /screener/fetch-ohlcv` or `POST /screener/sync`.

    **Indicators computed per stock:**
    - Price levels: close, 52w high/low, YTD high/low, % from 52w high/low
    - Moving averages: SMA 20/50/100/200, EMA 9/21/50/200
    - MACD: line, signal, histogram
    - Cross signals: golden cross / death cross event + state
    - Trend: ADX 14
    - Momentum: RSI 14, Stochastic %K/%D, CCI 20, Williams %R 14, ROC 10
    - Volatility: Bollinger Bands, ATR 14, StdDev 20, Historical Volatility 20
    - Volume: avg 1m/1y, volume ratio, OBV, VWAP
    - Pivot points: pivot, support 1, resistance 1

    **Behaviour:**
    - Runs asynchronously. Returns HTTP 202 immediately with a `job_id`.
    - Each stock's row in `stock_indicators` is updated in place (`ON CONFLICT DO UPDATE`).
    - Stocks are processed in ascending `id` order.
    - Poll status at `GET /screener/recalculate/{job_id}`.
    """
    job_id = str(uuid.uuid4())
    _create_job(job_id)
    background_tasks.add_task(_run_recalculate, job_id)
    return {"job_id": job_id, "status": "pending"}


def _run_recalculate(job_id: str) -> None:
    _update_job(job_id, status="running")
    try:
        engine = IndicatorEngine()
        processed, errors = engine.run_all_active()

        # Refresh stock classifications via stored procedure
        try:
            log.info("Job %s: calling analytics.sp_refresh_classifications()...", job_id)
            conn = get_connection()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("CALL analytics.sp_refresh_classifications()")
            conn.close()
            log.info("Job %s: stock classifications refreshed.", job_id)
        except Exception as exc:
            log.error("Job %s: failed to refresh classifications: %s", job_id, exc, exc_info=True)
            # Non-fatal — indicators were still computed successfully

        _update_job(job_id, status="completed", stocks_processed=processed)
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# GET /screener/recalculate/{job_id}
# ---------------------------------------------------------------------------

@router.get(
    "/recalculate/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll job status",
    tags=["Data Pipeline"],
)
def get_job_status(job_id: str):
    """
    Return the current status of any background job created by
    `/screener/fetch-ohlcv`, `/screener/sync`, or `/screener/recalculate`.

    **Path parameter:**
    - `job_id` — UUID returned by the job-creating endpoint.

    **Status values:**
    - `pending` — job queued, not yet started
    - `running` — job is actively processing
    - `completed` — job finished successfully; `stocks_processed` shows the count
    - `failed` — job encountered a fatal error; `error` contains the message

    **Error:** HTTP 404 if the `job_id` does not exist.
    """
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return JobStatusResponse(**job)
