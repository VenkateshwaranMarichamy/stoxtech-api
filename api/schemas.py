"""Pydantic response models for the screener API."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class IndicatorSnapshot(BaseModel):
    ticker_id: int
    trade_date: date
    computed_at: datetime

    # Price levels
    close: Decimal | None = None
    high_52w: Decimal | None = None
    low_52w: Decimal | None = None
    high_ytd: Decimal | None = None
    low_ytd: Decimal | None = None
    pct_from_52w_high: Decimal | None = None
    pct_from_52w_low: Decimal | None = None

    # Moving averages
    sma_20: Decimal | None = None
    sma_50: Decimal | None = None
    sma_100: Decimal | None = None
    sma_200: Decimal | None = None
    ema_9: Decimal | None = None
    ema_21: Decimal | None = None
    ema_50: Decimal | None = None
    ema_200: Decimal | None = None

    # MACD
    macd_line: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_histogram: Decimal | None = None

    # Cross signals
    golden_cross_event: bool | None = None
    death_cross_event: bool | None = None
    golden_cross_state: str | None = None

    # Trend
    adx_14: Decimal | None = None

    # Momentum / oscillators
    rsi_14: Decimal | None = None
    stoch_k: Decimal | None = None
    stoch_d: Decimal | None = None
    cci_20: Decimal | None = None
    williams_r_14: Decimal | None = None
    roc_10: Decimal | None = None

    # Volatility
    bb_upper: Decimal | None = None
    bb_middle: Decimal | None = None
    bb_lower: Decimal | None = None
    atr_14: Decimal | None = None
    stddev_20: Decimal | None = None
    hist_volatility_20: Decimal | None = None

    # Volume
    avg_volume_1m: Decimal | None = None
    avg_volume_1y: Decimal | None = None
    volume_ratio: Decimal | None = None
    obv: int | None = None
    vwap: Decimal | None = None

    # Pivot points
    pivot_point: Decimal | None = None
    pivot_support_1: Decimal | None = None
    pivot_resistance_1: Decimal | None = None

    model_config = {"from_attributes": True}


class StockListItem(BaseModel):
    """Subset returned by GET /screener/stocks."""

    ticker_id: int
    trade_date: date
    close: Decimal | None = None
    rsi_14: Decimal | None = None
    sma_50: Decimal | None = None
    sma_200: Decimal | None = None
    golden_cross_state: str | None = None
    high_52w: Decimal | None = None
    low_52w: Decimal | None = None
    pct_from_52w_high: Decimal | None = None
    volume_ratio: Decimal | None = None

    model_config = {"from_attributes": True}


class StocksListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    stocks: list[StockListItem]


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    stocks_processed: int | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
