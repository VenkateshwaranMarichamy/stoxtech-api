"""
Screener router — all /screener/* endpoints.
"""

import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg2.extras
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

import config
from db import get_connection
from indicator_engine import IndicatorEngine
from api.schemas import (
    IndicatorSnapshot,
    JobStatusResponse,
    StockListItem,
    StocksListResponse,
)

router = APIRouter()

# In-memory job store (sufficient for single-user local deployment)
_jobs: dict[str, dict] = {}


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
            # Total count
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM ticker_symbol ts
                JOIN stock_indicators si ON ts.instrument_key = si.instrument_key
                WHERE ts.is_screener_active = TRUE
                  AND si.trade_date = (
                      SELECT MAX(trade_date) FROM stock_indicators si2
                      WHERE si2.instrument_key = ts.instrument_key
                  );
                """
            )
            total = cur.fetchone()["total"]

            # Paginated rows
            cur.execute(
                """
                SELECT
                    si.instrument_key,
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
                FROM ticker_symbol ts
                JOIN stock_indicators si ON ts.instrument_key = si.instrument_key
                WHERE ts.is_screener_active = TRUE
                  AND si.trade_date = (
                      SELECT MAX(trade_date) FROM stock_indicators si2
                      WHERE si2.instrument_key = ts.instrument_key
                  )
                ORDER BY si.instrument_key
                LIMIT %s OFFSET %s;
                """,
                (page_size, offset),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    stocks = [StockListItem(**dict(row)) for row in rows]
    return StocksListResponse(
        page=page,
        page_size=page_size,
        total=total,
        stocks=stocks,
    )


# ---------------------------------------------------------------------------
# GET /screener/indicators/{instrument_key}
# ---------------------------------------------------------------------------

@router.get("/indicators/{instrument_key}", response_model=IndicatorSnapshot)
def get_latest_indicators(instrument_key: str):
    """Return the most recent indicator snapshot for a stock."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM stock_indicators
                WHERE instrument_key = %s
                ORDER BY trade_date DESC
                LIMIT 1;
                """,
                (instrument_key,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No indicator data found for instrument_key: {instrument_key}",
        )

    return IndicatorSnapshot(**dict(row))


# ---------------------------------------------------------------------------
# GET /screener/indicators/{instrument_key}/history
# ---------------------------------------------------------------------------

@router.get("/indicators/{instrument_key}/history", response_model=list[IndicatorSnapshot])
def get_indicator_history(
    instrument_key: str,
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
                SELECT * FROM stock_indicators
                WHERE instrument_key = %s
                  AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date DESC;
                """,
                (instrument_key, from_date, to_date),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [IndicatorSnapshot(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /screener/recalculate
# ---------------------------------------------------------------------------

@router.post("/recalculate", status_code=202)
def trigger_recalculate(background_tasks: BackgroundTasks):
    """Trigger async recalculation of indicators for all active stocks."""
    job_id = str(uuid.uuid4())
    now = datetime.utcnow()
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "stocks_processed": None,
        "error": None,
        "created_at": now,
        "completed_at": None,
    }
    background_tasks.add_task(_run_recalculate, job_id)
    return {"job_id": job_id, "status": "pending", "created_at": now}


def _run_recalculate(job_id: str) -> None:
    """Background task: run the indicator engine and update job state."""
    _jobs[job_id]["status"] = "running"
    try:
        engine = IndicatorEngine()
        processed, errors = engine.run_all_active()
        _jobs[job_id].update(
            status="completed",
            stocks_processed=processed,
            completed_at=datetime.utcnow(),
        )
    except Exception as exc:
        _jobs[job_id].update(
            status="failed",
            error=str(exc),
            completed_at=datetime.utcnow(),
        )


# ---------------------------------------------------------------------------
# GET /screener/recalculate/{job_id}
# ---------------------------------------------------------------------------

@router.get("/recalculate/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str):
    """Return the current status of a recalculation job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return JobStatusResponse(**job)
