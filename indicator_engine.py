"""
Indicator Engine.

Computes ~35 technical indicators per stock using pandas-ta and upserts
results into the stock_indicators table.
"""

import logging
from datetime import date, datetime

import numpy as np
import pandas as pd
import pandas_ta as ta
import psycopg2.extras

from db import get_connection
from logging_setup import setup_logging

logger = setup_logging("indicator_engine")

# Minimum rows required for each indicator group
_MIN_ROWS_SMA200 = 200
_MIN_ROWS_SMA100 = 100
_MIN_ROWS_SMA50 = 50
_MIN_ROWS_SMA20 = 20
_MIN_ROWS_EMA200 = 200
_MIN_ROWS_EMA50 = 50
_MIN_ROWS_EMA21 = 21
_MIN_ROWS_EMA9 = 9
_MIN_ROWS_MACD = 35   # 26 + 9 signal
_MIN_ROWS_RSI = 14
_MIN_ROWS_ADX = 14
_MIN_ROWS_STOCH = 14
_MIN_ROWS_CCI = 20
_MIN_ROWS_WILLIAMS = 14
_MIN_ROWS_ROC = 10
_MIN_ROWS_BB = 20
_MIN_ROWS_ATR = 14
_MIN_ROWS_OBV = 2
_MIN_ROWS_52W = 252
_MIN_ROWS_1Y_VOL = 252
_MIN_ROWS_1M_VOL = 21


def _safe_float(val) -> float | None:
    """Convert a value to float, returning None for NaN/None."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if (np.isnan(f) or np.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int | None:
    """Convert a value to int, returning None for NaN/None."""
    f = _safe_float(val)
    return None if f is None else int(f)


def _clamp(val: float | None, lo: float, hi: float) -> float | None:
    """Clamp a value to [lo, hi]. Returns None if val is None."""
    if val is None:
        return None
    return max(lo, min(hi, val))


class IndicatorEngine:
    """Computes technical indicators from OHLCV data and stores them in the DB."""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def compute_for_stock(
        self,
        ticker_id: int,
        df: pd.DataFrame,
    ) -> dict:
        """
        Compute all indicators for the most recent trading day.

        Args:
            ticker_id: FK to classification.ticker_symbol.id
            df: OHLCV DataFrame sorted ascending by trade_date.

        Returns:
            Flat dict of indicator_name → value (None for insufficient data).
        """
        n = len(df)
        if n == 0:
            return {}

        result: dict = {
            "ticker_id":  ticker_id,
            "trade_date": df["trade_date"].iloc[-1],
        }

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        # ---- Price levels ------------------------------------------------
        result["close"] = _safe_float(close.iloc[-1])

        # 52-week high/low — use up to 252 rows, but compute from whatever is available
        lookback = min(n, _MIN_ROWS_52W)
        h52 = close.iloc[-lookback:].max()
        l52 = close.iloc[-lookback:].min()
        c   = close.iloc[-1]
        result["high_52w"] = _safe_float(h52)
        result["low_52w"]  = _safe_float(l52)
        result["pct_from_52w_high"] = _safe_float(((c - h52) / h52) * 100) if h52 else None
        result["pct_from_52w_low"]  = _safe_float(((c - l52) / l52) * 100) if l52 else None

        # YTD high/low
        today = pd.Timestamp.today()
        ytd_start = pd.Timestamp(today.year, 1, 1)
        ytd_mask = pd.to_datetime(df["trade_date"]) >= ytd_start
        ytd_close = close[ytd_mask]
        if len(ytd_close) > 0:
            result["high_ytd"] = _safe_float(ytd_close.max())
            result["low_ytd"] = _safe_float(ytd_close.min())
        else:
            result["high_ytd"] = None
            result["low_ytd"] = None

        # ---- Moving averages ---------------------------------------------
        # Compute from available data — if fewer rows than the period,
        # the rolling/ewm result will be NaN for early rows but the last
        # value will still be valid as a partial-window average.
        for length, col in [
            (_MIN_ROWS_SMA20,  "sma_20"),
            (_MIN_ROWS_SMA50,  "sma_50"),
            (_MIN_ROWS_SMA100, "sma_100"),
            (_MIN_ROWS_SMA200, "sma_200"),
        ]:
            val = close.rolling(min(n, length)).mean().iloc[-1]
            result[col] = _safe_float(val)

        for length, col in [
            (_MIN_ROWS_EMA9,   "ema_9"),
            (_MIN_ROWS_EMA21,  "ema_21"),
            (_MIN_ROWS_EMA50,  "ema_50"),
            (_MIN_ROWS_EMA200, "ema_200"),
        ]:
            val = close.ewm(span=min(n, length), adjust=False).mean().iloc[-1]
            result[col] = _safe_float(val)

        # ---- MACD --------------------------------------------------------
        if n >= _MIN_ROWS_MACD:
            macd_df = df.copy()
            macd_df.ta.macd(fast=12, slow=26, signal=9, append=True)
            result["macd_line"] = _clamp(
                _safe_float(macd_df.get("MACD_12_26_9", pd.Series([None])).iloc[-1]),
                -9999999.99, 9999999.99,
            )
            result["macd_signal"] = _clamp(
                _safe_float(macd_df.get("MACDs_12_26_9", pd.Series([None])).iloc[-1]),
                -9999999.99, 9999999.99,
            )
            result["macd_histogram"] = _clamp(
                _safe_float(macd_df.get("MACDh_12_26_9", pd.Series([None])).iloc[-1]),
                -9999999.99, 9999999.99,
            )
        else:
            result.update(macd_line=None, macd_signal=None, macd_histogram=None)

        # ---- Golden / Death cross ----------------------------------------
        sma50  = result.get("sma_50")
        sma200 = result.get("sma_200")
        if sma50 is not None and sma200 is not None:
            result["golden_cross_state"] = "above" if sma50 > sma200 else "below"
            # Cross event: need at least 2 rows to compare yesterday vs today
            if n >= 2:
                prev_sma50  = close.rolling(min(n, _MIN_ROWS_SMA50)).mean().iloc[-2]
                prev_sma200 = close.rolling(min(n, _MIN_ROWS_SMA200)).mean().iloc[-2]
                result["golden_cross_event"] = bool(
                    prev_sma50 <= prev_sma200 and sma50 > sma200
                )
                result["death_cross_event"] = bool(
                    prev_sma50 >= prev_sma200 and sma50 < sma200
                )
            else:
                result["golden_cross_event"] = False
                result["death_cross_event"]  = False
        else:
            result.update(
                golden_cross_state=None,
                golden_cross_event=None,
                death_cross_event=None,
            )

        # ---- ADX ---------------------------------------------------------
        if n >= _MIN_ROWS_ADX:
            adx_df = df.copy()
            adx_df.ta.adx(length=14, append=True)
            result["adx_14"] = _safe_float(
                adx_df.get("ADX_14", pd.Series([None])).iloc[-1]
            )
        else:
            result["adx_14"] = None

        # ---- RSI ---------------------------------------------------------
        if n >= _MIN_ROWS_RSI:
            rsi_df = df.copy()
            rsi_df.ta.rsi(length=14, append=True)
            result["rsi_14"] = _safe_float(
                rsi_df.get("RSI_14", pd.Series([None])).iloc[-1]
            )
        else:
            result["rsi_14"] = None

        # ---- Stochastic --------------------------------------------------
        if n >= _MIN_ROWS_STOCH:
            stoch_df = df.copy()
            stoch_df.ta.stoch(k=14, d=3, append=True)
            result["stoch_k"] = _safe_float(
                stoch_df.get("STOCHk_14_3_3", pd.Series([None])).iloc[-1]
            )
            result["stoch_d"] = _safe_float(
                stoch_df.get("STOCHd_14_3_3", pd.Series([None])).iloc[-1]
            )
        else:
            result.update(stoch_k=None, stoch_d=None)

        # ---- CCI ---------------------------------------------------------
        if n >= _MIN_ROWS_CCI:
            cci_df = df.copy()
            cci_df.ta.cci(length=20, append=True)
            raw_cci = _safe_float(
                cci_df.get("CCI_20_0.015", pd.Series([None])).iloc[-1]
            )
            # CCI is theoretically unbounded; clamp to ±9999 to fit NUMERIC(10,2)
            result["cci_20"] = _clamp(raw_cci, -9999.99, 9999.99)
        else:
            result["cci_20"] = None

        # ---- Williams %R -------------------------------------------------
        if n >= _MIN_ROWS_WILLIAMS:
            wr_df = df.copy()
            wr_df.ta.willr(length=14, append=True)
            result["williams_r_14"] = _safe_float(
                wr_df.get("WILLR_14", pd.Series([None])).iloc[-1]
            )
        else:
            result["williams_r_14"] = None

        # ---- ROC ---------------------------------------------------------
        if n >= _MIN_ROWS_ROC:
            roc_df = df.copy()
            roc_df.ta.roc(length=10, append=True)
            raw_roc = _safe_float(
                roc_df.get("ROC_10", pd.Series([None])).iloc[-1]
            )
            # ROC is a % change — clamp to ±9999 to fit NUMERIC(10,2)
            result["roc_10"] = _clamp(raw_roc, -9999.99, 9999.99)
        else:
            result["roc_10"] = None

        # ---- Bollinger Bands ---------------------------------------------
        if n >= _MIN_ROWS_BB:
            bb_df = df.copy()
            bb_df.ta.bbands(length=20, std=2, append=True)
            # Column names vary by pandas-ta version: BBU_20_2.0 or BBU_20_2.0_2.0
            bb_cols = {c.split("_")[0]: c for c in bb_df.columns if c.startswith("BB")}
            result["bb_upper"] = _safe_float(
                bb_df.get(bb_cols.get("BBU", "BBU_20_2.0"), pd.Series([None])).iloc[-1]
            )
            result["bb_middle"] = _safe_float(
                bb_df.get(bb_cols.get("BBM", "BBM_20_2.0"), pd.Series([None])).iloc[-1]
            )
            result["bb_lower"] = _safe_float(
                bb_df.get(bb_cols.get("BBL", "BBL_20_2.0"), pd.Series([None])).iloc[-1]
            )
        else:
            result.update(bb_upper=None, bb_middle=None, bb_lower=None)

        # ---- ATR ---------------------------------------------------------
        if n >= _MIN_ROWS_ATR:
            atr_df = df.copy()
            atr_df.ta.atr(length=14, append=True)
            result["atr_14"] = _safe_float(
                atr_df.get("ATRr_14", pd.Series([None])).iloc[-1]
            )
        else:
            result["atr_14"] = None

        # ---- Std dev & historical volatility -----------------------------
        if n >= _MIN_ROWS_BB:
            result["stddev_20"] = _safe_float(close.rolling(20).std().iloc[-1])
        else:
            result["stddev_20"] = None

        if n >= _MIN_ROWS_BB:
            log_returns = np.log(close / close.shift(1)).dropna()
            if len(log_returns) >= 20:
                result["hist_volatility_20"] = _safe_float(
                    log_returns.rolling(20).std().iloc[-1] * np.sqrt(252)
                )
            else:
                result["hist_volatility_20"] = None
        else:
            result["hist_volatility_20"] = None

        # ---- Volume indicators -------------------------------------------
        # avg_volume_1m — last 21 days or all available
        lookback_1m = min(n, _MIN_ROWS_1M_VOL)
        result["avg_volume_1m"] = _safe_float(volume.iloc[-lookback_1m:].mean())

        # avg_volume_1y — last 252 days or all available
        lookback_1y = min(n, _MIN_ROWS_1Y_VOL)
        result["avg_volume_1y"] = _safe_float(volume.iloc[-lookback_1y:].mean())

        avg_1m = result.get("avg_volume_1m")
        if avg_1m and avg_1m > 0:
            result["volume_ratio"] = _safe_float(volume.iloc[-1] / avg_1m)
        else:
            result["volume_ratio"] = None

        # OBV
        if n >= _MIN_ROWS_OBV:
            obv_df = df.copy()
            obv_df.ta.obv(append=True)
            result["obv"] = _safe_int(
                obv_df.get("OBV", pd.Series([None])).iloc[-1]
            )
        else:
            result["obv"] = None

        # VWAP (single-day approximation: sum(typical_price * volume) / sum(volume))
        typical = (high + low + close) / 3
        if volume.iloc[-1] > 0:
            result["vwap"] = _safe_float(
                (typical * volume).sum() / volume.sum()
            )
        else:
            result["vwap"] = None

        # ---- Pivot points (using prior day's H/L/C) ----------------------
        if n >= 2:
            ph = float(high.iloc[-2])
            pl = float(low.iloc[-2])
            pc = float(close.iloc[-2])
            pivot = (ph + pl + pc) / 3
            result["pivot_point"] = _safe_float(pivot)
            result["pivot_support_1"] = _safe_float(2 * pivot - ph)
            result["pivot_resistance_1"] = _safe_float(2 * pivot - pl)
        else:
            result.update(pivot_point=None, pivot_support_1=None, pivot_resistance_1=None)

        return result

    def run_all_active(self) -> tuple[int, int]:
        """
        Compute indicators for all active stocks and upsert into stock_indicators.

        Returns:
            ``(processed_count, error_count)`` tuple.
        """
        logger.info("Fetching active stocks from DB.")
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT id, instrument_key FROM classification.ticker_symbol "
                    "WHERE is_screener_active = TRUE ORDER BY id ASC;"
                )
                stocks = [{"ticker_id": row["id"], "instrument_key": row["instrument_key"]}
                          for row in cur.fetchall()]
        finally:
            conn.close()

        logger.info("Computing indicators for %d active stocks.", len(stocks))
        processed = 0
        errors = 0

        for stock in stocks:
            ticker_id     = stock["ticker_id"]
            instrument_key = stock["instrument_key"]
            try:
                df = self._load_ohlcv(ticker_id)
                if df.empty:
                    logger.debug("No OHLCV data for %s (id=%d), skipping.", instrument_key, ticker_id)
                    continue
                indicators = self.compute_for_stock(ticker_id, df)
                if indicators:
                    self._upsert_indicators(indicators)
                    processed += 1
            except Exception as exc:
                logger.error(
                    "Error computing indicators for %s: %s", instrument_key, exc, exc_info=True
                )
                errors += 1

        logger.info(
            "Indicator computation done: %d processed, %d errors.", processed, errors
        )
        return processed, errors

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_ohlcv(self, ticker_id: int) -> pd.DataFrame:
        """Load all OHLCV rows for a stock from the DB, sorted ascending."""
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    """
                    SELECT trade_date, open, high, low, close, volume
                    FROM technical.ohlcv_daily
                    WHERE ticker_id = %s
                    ORDER BY trade_date ASC;
                    """,
                    (ticker_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df

    def _upsert_indicators(self, data: dict) -> None:
        """Upsert a single indicator row into stock_indicators."""
        skip = {"ticker_id", "trade_date"}
        cols = [k for k in data if k not in skip]

        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
        col_list   = ", ".join(["ticker_id", "trade_date"] + cols)
        val_list   = ", ".join(
            ["%(ticker_id)s", "%(trade_date)s"] + [f"%({c})s" for c in cols]
        )

        sql = f"""
            INSERT INTO technical.stock_indicators ({col_list}, computed_at)
            VALUES ({val_list}, NOW())
            ON CONFLICT (ticker_id, trade_date)
            DO UPDATE SET {set_clause}, computed_at = NOW();
        """

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, data)
        finally:
            conn.close()
