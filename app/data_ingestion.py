"""
Data ingestion module.

Provides:
  - UpstoxClient       — wraps the Upstox v2 historical candle API
  - OHLCVWriter        — upserts OHLCV records into technical.ohlcv_daily (keyed by ticker_id)
  - ActiveStockUpdater — computes and sets is_screener_active flags
"""

import os
import time
import json
import logging
from datetime import date

import requests
import psycopg2.extras

import app.config as config
from app.db import get_connection
from app.logging_setup import setup_logging

logger = setup_logging("data_ingestion")


class AuthError(Exception):
    """Raised when the Upstox API returns HTTP 401."""


# ---------------------------------------------------------------------------
# Upstox API client
# ---------------------------------------------------------------------------

class UpstoxClient:
    """Thin wrapper around the Upstox v2 historical candle REST API."""

    def __init__(self, access_token: str):
        self._token = access_token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })

    def get_historical_daily(
        self,
        instrument_key: str,
        to_date: str,
        from_date: str | None = None,
    ) -> list[dict]:
        """
        Fetch daily OHLCV candles from the Upstox v2 API.

        Returns list of dicts with keys:
            trade_date, open, high, low, close, volume.
        Raises AuthError on HTTP 401.
        """
        encoded_key = instrument_key.replace("|", "%7C")
        url = f"{config.UPSTOX_BASE_URL}/historical-candle/{encoded_key}/day/{to_date}"
        if from_date:
            url += f"/{from_date}"

        for attempt in range(config.UPSTOX_MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, timeout=30)
            except requests.RequestException as exc:
                logger.warning(
                    "Network error for %s (attempt %d/%d): %s",
                    instrument_key, attempt + 1, config.UPSTOX_MAX_RETRIES, exc,
                )
                if attempt < config.UPSTOX_MAX_RETRIES:
                    time.sleep(config.UPSTOX_RETRY_DELAY_SECONDS * (2 ** attempt))
                    continue
                return []

            if resp.status_code == 200:
                return self._parse_candles(resp.json())

            if resp.status_code == 401:
                raise AuthError(f"Upstox API authentication failed for {instrument_key}")

            if resp.status_code == 404:
                logger.warning("Instrument not found: %s", instrument_key)
                return []

            if resp.status_code == 429:
                wait = config.UPSTOX_RETRY_DELAY_SECONDS * (2 ** attempt)
                logger.warning(
                    "Rate limited for %s. Waiting %.1fs (attempt %d/%d).",
                    instrument_key, wait, attempt + 1, config.UPSTOX_MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                logger.warning(
                    "Server error %d for %s (attempt %d/%d).",
                    resp.status_code, instrument_key, attempt + 1, config.UPSTOX_MAX_RETRIES,
                )
                if attempt < config.UPSTOX_MAX_RETRIES:
                    time.sleep(config.UPSTOX_RETRY_DELAY_SECONDS * (2 ** attempt))
                    continue
                return []

            logger.warning("Unexpected HTTP %d for %s.", resp.status_code, instrument_key)
            return []

        return []

    @staticmethod
    def _parse_candles(payload: dict) -> list[dict]:
        """Parse the Upstox candle array into plain dicts (no instrument_key)."""
        try:
            candles = payload["data"]["candles"]
        except (KeyError, TypeError):
            return []

        return [
            {
                "trade_date": c[0][:10],  # "2024-01-15T00:00:00+05:30" → "2024-01-15"
                "open":       c[1],
                "high":       c[2],
                "low":        c[3],
                "close":      c[4],
                "volume":     c[5],
            }
            for c in candles
        ]


# ---------------------------------------------------------------------------
# ticker_id lookup cache  (instrument_key → id)
# ---------------------------------------------------------------------------

_ticker_id_cache: dict[str, int] = {}


def get_ticker_id(instrument_key: str) -> int | None:
    """
    Return the classification.ticker_symbol.id for an instrument_key.
    Results are cached in-process to avoid repeated DB lookups.
    """
    if instrument_key in _ticker_id_cache:
        return _ticker_id_cache[instrument_key]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM classification.ticker_symbol WHERE instrument_key = %s;",
                (instrument_key,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        logger.warning("No ticker_id found for instrument_key: %s", instrument_key)
        return None

    _ticker_id_cache[instrument_key] = row[0]
    return row[0]


# ---------------------------------------------------------------------------
# OHLCV writer  (uses ticker_id FK)
# ---------------------------------------------------------------------------

class OHLCVWriter:
    """Upserts OHLCV records into technical.ohlcv_daily using ticker_id FK."""

    _UPSERT_SQL = """
        INSERT INTO technical.ohlcv_daily
            (ticker_id, trade_date, open, high, low, close, volume)
        VALUES
            (%(ticker_id)s, %(trade_date)s, %(open)s, %(high)s,
             %(low)s, %(close)s, %(volume)s)
        ON CONFLICT (ticker_id, trade_date) DO NOTHING;
    """

    def upsert_batch(self, ticker_id: int, records: list[dict]) -> tuple[int, int]:
        """
        Upsert a batch of OHLCV records for a single stock.

        Args:
            ticker_id: FK to classification.ticker_symbol.id
            records:   List of dicts with trade_date/open/high/low/close/volume.

        Returns:
            (inserted_count, skipped_count)
        """
        if not records:
            return 0, 0

        rows = [{**r, "ticker_id": ticker_id} for r in records]
        trade_dates = [r["trade_date"] for r in records]

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, self._UPSERT_SQL, rows)
                    cur.execute(
                        "SELECT COUNT(*) FROM technical.ohlcv_daily "
                        "WHERE ticker_id = %s AND trade_date = ANY(%s::date[])",
                        (ticker_id, trade_dates),
                    )
                    total_present = cur.fetchone()[0]
            skipped = len(records) - total_present
            return total_present, max(skipped, 0)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Active stock flag updater
# ---------------------------------------------------------------------------

class ActiveStockUpdater:
    """Computes average daily traded value and updates is_screener_active."""

    def update_active_flags(
        self, threshold: float = config.MIN_AVG_DAILY_TRADED_VALUE
    ) -> tuple[int, int]:
        """
        Set is_screener_active based on avg(close * volume) over the last
        ACTIVE_STOCK_LOOKBACK_DAYS trading days.

        Returns:
            (activated_count, deactivated_count)
        """
        sql = """
            WITH avg_traded AS (
                SELECT
                    ticker_id,
                    AVG(close * volume) AS avg_traded_value
                FROM technical.ohlcv_daily
                WHERE trade_date >= CURRENT_DATE - INTERVAL '{lookback} days'
                GROUP BY ticker_id
            )
            UPDATE classification.ticker_symbol ts
            SET is_screener_active = (
                COALESCE(at.avg_traded_value, 0) >= %(threshold)s
            )
            FROM avg_traded at
            WHERE ts.id = at.ticker_id
            RETURNING ts.id, ts.is_screener_active;
        """.format(lookback=config.ACTIVE_STOCK_LOOKBACK_DAYS)

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, {"threshold": threshold})
                    rows = cur.fetchall()
            activated   = sum(1 for _, active in rows if active)
            deactivated = sum(1 for _, active in rows if not active)
            logger.info(
                "Active flags updated: %d activated, %d deactivated.",
                activated, deactivated,
            )
            return activated, deactivated
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Simple sequential fetch  (daily sync — small lists)
# ---------------------------------------------------------------------------

def fetch_and_store(
    client: UpstoxClient,
    writer: OHLCVWriter,
    instrument_keys: list[str],
    to_date: str,
    from_date: str | None = None,
) -> tuple[int, int]:
    """Fetch and store OHLCV for a list of keys. Returns (updated, failed)."""
    updated = 0
    failed  = 0

    for i, key in enumerate(instrument_keys, start=1):
        try:
            ticker_id = get_ticker_id(key)
            if ticker_id is None:
                logger.warning("Skipping %s — not found in ticker_symbol.", key)
                continue

            records = client.get_historical_daily(key, to_date, from_date)
            if records:
                writer.upsert_batch(ticker_id, records)
                updated += 1
                logger.debug("Stored %d records for %s (id=%d).", len(records), key, ticker_id)
            else:
                logger.debug("No records returned for %s.", key)
        except AuthError:
            raise
        except Exception as exc:
            logger.error("Failed to process %s: %s", key, exc, exc_info=True)
            failed += 1

        time.sleep(config.UPSTOX_REQUEST_DELAY_SECONDS)

        if i % 100 == 0:
            logger.info("Progress: %d / %d stocks processed.", i, len(instrument_keys))

    return updated, failed


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> int:
    path = config.BOOTSTRAP_CHECKPOINT_FILE
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            idx = int(data.get("next_index", 0))
            logger.info("Checkpoint found — resuming from index %d.", idx)
            return idx
        except Exception as exc:
            logger.warning("Could not read checkpoint: %s. Starting from 0.", exc)
    return 0


def _save_checkpoint(next_index: int) -> None:
    os.makedirs(os.path.dirname(config.BOOTSTRAP_CHECKPOINT_FILE), exist_ok=True)
    with open(config.BOOTSTRAP_CHECKPOINT_FILE, "w") as f:
        json.dump({"next_index": next_index, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)


def _clear_checkpoint() -> None:
    if os.path.exists(config.BOOTSTRAP_CHECKPOINT_FILE):
        os.remove(config.BOOTSTRAP_CHECKPOINT_FILE)
        logger.info("Checkpoint cleared.")


# ---------------------------------------------------------------------------
# Chunked batch runner  (5000+ stocks, window-aware)
# ---------------------------------------------------------------------------

def fetch_and_store_chunked(
    client: UpstoxClient,
    writer: OHLCVWriter,
    instrument_keys: list[str],
    to_date: str,
    from_date: str | None = None,
    resume: bool = True,
) -> tuple[int, int]:
    """
    Fetch OHLCV for large universes respecting Upstox's 2,000 req/30-min limit.
    Saves a checkpoint after every stock so interrupted runs can be resumed.
    """
    total       = len(instrument_keys)
    batch_size  = config.UPSTOX_WINDOW_MAX_REQUESTS
    window_secs = config.UPSTOX_WINDOW_SECONDS

    start_index = _load_checkpoint() if resume else 0
    if start_index >= total:
        logger.info("All %d stocks already processed (checkpoint).", total)
        return 0, 0

    remaining = instrument_keys[start_index:]
    logger.info(
        "Chunked bootstrap: %d total, starting at index %d (%d remaining).",
        total, start_index, len(remaining),
    )

    updated      = 0
    failed       = 0
    global_index = start_index
    batches      = [remaining[i:i + batch_size] for i in range(0, len(remaining), batch_size)]
    total_batches = len(batches)

    for batch_num, batch in enumerate(batches, start=1):
        batch_start = time.time()
        logger.info(
            "--- Batch %d / %d | %d stocks ---",
            batch_num, total_batches, len(batch),
        )
        b_updated = b_failed = 0

        for i, key in enumerate(batch, start=1):
            try:
                ticker_id = get_ticker_id(key)
                if ticker_id is None:
                    logger.warning("Skipping %s — not in ticker_symbol.", key)
                else:
                    records = client.get_historical_daily(key, to_date, from_date)
                    if records:
                        writer.upsert_batch(ticker_id, records)
                        b_updated += 1
                        logger.debug("Stored %d records for %s.", len(records), key)
                    else:
                        logger.debug("No records for %s.", key)
            except AuthError:
                _save_checkpoint(global_index)
                raise
            except Exception as exc:
                logger.error("Failed %s: %s", key, exc, exc_info=True)
                b_failed += 1

            global_index += 1
            _save_checkpoint(global_index)
            time.sleep(config.UPSTOX_REQUEST_DELAY_SECONDS)

            if i % 100 == 0:
                elapsed = time.time() - batch_start
                rate    = i / elapsed if elapsed > 0 else 0
                logger.info(
                    "Batch %d: %d/%d | %.1f req/s | overall %d/%d",
                    batch_num, i, len(batch), rate, global_index, total,
                )

        updated += b_updated
        failed  += b_failed
        batch_elapsed = time.time() - batch_start
        logger.info(
            "Batch %d done in %.0fs — updated: %d, failed: %d.",
            batch_num, batch_elapsed, b_updated, b_failed,
        )

        if batch_num < total_batches:
            wait = window_secs - batch_elapsed + config.UPSTOX_INTER_BATCH_PAUSE_SECONDS
            if wait > 0:
                resume_at = time.strftime("%H:%M:%S", time.localtime(time.time() + wait))
                logger.info("Window pause: %.0fs. Next batch at ~%s.", wait, resume_at)
                slept = 0
                while slept < wait:
                    chunk = min(60, wait - slept)
                    time.sleep(chunk)
                    slept += chunk
                    if wait - slept > 0:
                        logger.info("Waiting... %.0fs remaining.", wait - slept)
            else:
                time.sleep(config.UPSTOX_INTER_BATCH_PAUSE_SECONDS)

    _clear_checkpoint()
    logger.info("Chunked fetch complete: %d updated, %d failed.", updated, failed)
    return updated, failed
