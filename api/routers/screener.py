"""
Screener router — all /screener/* endpoints.
"""

import os
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg2.extras
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

import config
from db import get_connection
from indicator_engine import IndicatorEngine
from data_ingestion import UpstoxClient, OHLCVWriter, fetch_and_store
from api.schemas import (
    IndicatorSnapshot,
    JobStatusResponse,
    StockListItem,
    StocksListResponse,
)

router = APIRouter()


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

@router.get("/stocks", response_model=StocksListResponse)
def list_stocks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=config.API_DEFAULT_PAGE_SIZE, ge=1),
):
    """Return paginated list of active stocks with their latest indicator snapshot."""
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

@router.get("/indicators/{ticker_id}", response_model=IndicatorSnapshot)
def get_latest_indicators(ticker_id: int):
    """Return the most recent indicator snapshot for a stock."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM technical.stock_indicators
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

@router.get("/indicators/{ticker_id}/history", response_model=list[IndicatorSnapshot])
def get_indicator_history(
    ticker_id: int,
    from_date: Optional[date] = Query(default=None),
    to_date: Optional[date] = Query(default=None),
):
    """Return historical indicator values for a stock within a date range."""
    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=config.API_DEFAULT_HISTORY_DAYS)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM technical.stock_indicators
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
# POST /screener/sync
# Fetches latest OHLCV from Upstox for all active stocks, then recomputes
# indicators. Equivalent to running sync_daily.py via the API.
# ---------------------------------------------------------------------------

@router.post("/sync", status_code=202)
def trigger_sync(
    background_tasks: BackgroundTasks,
    days: int = Query(default=365, ge=1, le=365,
                      description="How many calendar days of history to fetch (max 365)"),
):
    """
    Fetch OHLCV from Upstox for all active stocks then recompute indicators.
    Returns a job_id to poll for status.
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
    return {"job_id": job_id, "status": "pending",
            "message": f"Fetching last {days} calendar days of OHLCV then recomputing indicators"}


def _run_sync(job_id: str, token: str, days: int) -> None:
    """Background task: fetch OHLCV from Upstox then run indicator engine."""
    _update_job(job_id, status="running")
    try:
        # Load active instrument keys
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT instrument_key FROM classification.ticker_symbol "
                    "WHERE is_screener_active = TRUE;"
                )
                keys = [row["instrument_key"] for row in cur.fetchall()]
        finally:
            conn.close()

        to_date   = date.today().isoformat()
        from_date = (date.today() - timedelta(days=days)).isoformat()

        import logging
        log = logging.getLogger("sync_job")
        log.info(
            "Sync job %s: fetching %d active stocks | URL pattern: "
            "GET %s/historical-candle/<key>/day/%s/%s",
            job_id, len(keys), config.UPSTOX_BASE_URL, to_date, from_date,
        )

        client = UpstoxClient(token)
        writer = OHLCVWriter()
        updated, failed = fetch_and_store(client, writer, keys, to_date, from_date)

        log.info("Sync job %s: OHLCV done — updated=%d failed=%d", job_id, updated, failed)

        # Recompute indicators
        engine = IndicatorEngine()
        processed, errors = engine.run_all_active()

        _update_job(job_id, status="completed", stocks_processed=processed)
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# POST /screener/recalculate
# ---------------------------------------------------------------------------

@router.post("/recalculate", status_code=202)
def trigger_recalculate(background_tasks: BackgroundTasks):
    """Trigger async recalculation of indicators for all active stocks."""
    job_id = str(uuid.uuid4())
    _create_job(job_id)
    background_tasks.add_task(_run_recalculate, job_id)
    return {"job_id": job_id, "status": "pending"}


def _run_recalculate(job_id: str) -> None:
    _update_job(job_id, status="running")
    try:
        engine = IndicatorEngine()
        processed, errors = engine.run_all_active()
        _update_job(job_id, status="completed", stocks_processed=processed)
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


# ---------------------------------------------------------------------------
# GET /screener/recalculate/{job_id}
# ---------------------------------------------------------------------------

@router.get("/recalculate/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    """Return the current status of a recalculation job."""
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return JobStatusResponse(**job)
