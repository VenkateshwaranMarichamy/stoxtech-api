"""
Daily Sync Script.

Fetches the latest OHLCV data for all active stocks and recomputes indicators.

Usage:
    python sync_daily.py

Cron (weekdays at 15:30 IST):
    30 15 * * 1-5  /path/to/venv/bin/python /path/to/stk_fund/sync_daily.py
"""

import os
import sys
import time
from datetime import date

import psycopg2.extras

import config
from db import get_connection
from data_ingestion import UpstoxClient, OHLCVWriter, AuthError, fetch_and_store
from indicator_engine import IndicatorEngine
from logging_setup import setup_logging

logger = setup_logging("sync_daily")


def main() -> int:
    start = time.time()
    logger.info("Daily sync job started.")

    # Fetch Upstox token
    token = os.getenv(config.UPSTOX_ACCESS_TOKEN_ENV)
    if not token:
        logger.error(
            "Environment variable %s is not set.", config.UPSTOX_ACCESS_TOKEN_ENV
        )
        return 1

    # Load active instrument keys ordered by id
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT instrument_key FROM classification.ticker_symbol "
                "WHERE is_screener_active = TRUE ORDER BY id ASC;"
            )
            keys = [row["instrument_key"] for row in cur.fetchall()]
    except Exception as exc:
        logger.error("Failed to load active instrument keys: %s", exc, exc_info=True)
        return 1
    finally:
        conn.close()

    logger.info("Fetching OHLCV for %d active stocks.", len(keys))

    client = UpstoxClient(token)
    writer = OHLCVWriter()
    to_date = date.today().isoformat()

    try:
        updated, failed = fetch_and_store(client, writer, keys, to_date)
    except AuthError as exc:
        logger.error("Authentication error: %s", exc)
        return 1

    logger.info(
        "OHLCV sync done: %d updated, 0 skipped, %d failed.", updated, failed
    )

    # Recompute indicators
    engine = IndicatorEngine()
    processed, errors = engine.run_all_active()
    logger.info("Indicators: %d processed, %d errors.", processed, errors)

    # Refresh stock classifications via stored procedure
    try:
        logger.info("Calling analytics.sp_refresh_classifications()...")
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("CALL analytics.sp_refresh_classifications()")
        conn.close()
        logger.info("Stock classifications refreshed.")
    except Exception as exc:
        logger.error("Failed to refresh classifications: %s", exc, exc_info=True)
        # Non-fatal — sync still succeeded

    elapsed = time.time() - start
    logger.info("Daily sync job completed in %.1fs.", elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
